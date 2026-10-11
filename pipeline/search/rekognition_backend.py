"""Backend V3: Trace face search and SerpApi web search, checked by Amazon Rekognition (spec 1.1, 1.7; live only).

V1 (SEARCH_BACKEND=serpapi) returns what Yandex finds visually similar, which for a selfie
is mostly lookalikes. V2 (facecheck) trusts FaceCheck's own face index. V3 gathers candidates
from Trace's face index (trace_face.py) and from SerpApi's Yandex and Google Lens, and checks
the face in every candidate itself with Rekognition. For each image:

1. DetectFaces runs on an upright JPEG copy (at most 1,600 px, EXIF stripped). The largest
   face is the participant's. An image with no face fails as no_face before any search.
2. A crop of that face, padded by half the face's width and height on each side, is searched
   once with each engine in REKOGNITION_ENGINES (default trace, yandex_images, google_lens).
   Trace gets the crop as an upload (the upright copy if the crop is under 200 px); the SerpApi
   engines get it hosted the way V1 hosts images (hosting.py: tunnel or s3). Only the face
   leaves this machine. The engines run at the same time. If one fails, the others' results are
   used and failed_engines names it; if all fail, the search fails and a retry searches again.
3. Results are merged by page URL: all of Trace's matches first, then the SerpApi engines
   alternating by rank. With REKOGNITION_PLATFORMS set (V3_1 is linkedin,facebook), results on other
   sites are dropped here, before any download or comparison, and count as out_of_scope.
   Every Trace match left is compared, plus the first MAX_CANDIDATES of the rest. For a Trace match the image is
   Trace's preview of the matched face; for a SerpApi result it is downloaded with the same
   public-address guard as page fetches (the full-size image first, the engine's thumbnail if
   that fails). Each is shrunk to at most 1,600 px and compared with the crop by CompareFaces.
   An image that won't load or shows no face moves on to the result's next image. face_score
   is the highest similarity among the faces in the first image with a face, rounded down.
4. Results under REKOGNITION_MIN_SIMILARITY (default 80, CompareFaces' own default) and images
   without a face are dropped. The rest are ranked by similarity and converted to the
   Yandex-shaped payload parse_image_results() reads. search_information counts what happened
   to every result (matched, below_threshold, no_face, image_unavailable, ...).

Comparing stops after COMPARE_TIMEOUT_S; results not compared by then count as not_compared.
The image's Trace scan, with the uploaded crop and the previews, is deleted at Trace once its
search and comparisons are over (release); a scan that timed out is left running, and a rerun
within 24 hours resumes it through the Idempotency-Key derived from the image hash. Each SerpApi engine's sanitized reply is cached for
this run under runs/<run_id>/cache/rekognition/, keyed by image hash and engine, so a rerun makes
no new SerpApi search. Trace replies aren't cached, because their previews are gone after
release: a rerun that compares again pays one Trace credit per image. The final payload is
cached too, keyed by image hash, engines and threshold, but only when every engine answered
and every result was compared. --refresh skips both caches.

AWS credentials come from the standard chain (`python -m pipeline set-key aws`) and the region
from AWS_REGION; the IAM user needs rekognition:DetectFaces and rekognition:CompareFaces.
Rekognition charges about $0.001 per image analyzed: one DetectFaces per capture image, and one
CompareFaces per result compared, plus one more for each of its images that shows no face. Unless the AWS account opts out (an AWS Organizations AI
services opt-out policy), AWS may keep the analyzed images and use them to improve its services.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from pipeline import trace
from pipeline.config import DEFAULT_MIN_SIMILARITY, ConfigError, Settings
from pipeline.contracts import CaptureArtifact, CaptureImage
from pipeline.platforms import normalize_url, platform_for
from pipeline.search.base import NO_RESULTS, BackendError, _thumbnail, status_error
from pipeline.search.serpapi_backend import (
    MAX_REDIRECTS, SEARCH_TIMEOUT_S, BlockedDestination, SerpApiBackend, _write_json, check_url, open_host,
    page_client,
)
from pipeline.search.trace_face import MIN_SIDE as TRACE_MIN_SIDE, SCAN_TIMEOUT_S as TRACE_TIMEOUT_S
from pipeline.search.trace_face import TraceFaceSearch, is_preview_url

# SerpApi engine -> (extra request parameters, the result lists in its reply)
ENGINES = {
    "yandex_images": ({"tab": "about"}, ("image_results",)),
    "google_lens": ({}, ("visual_matches", "exact_matches")),
}
TRACE = "trace"  # Trace's face index (trace_face.py); its matches are merged and compared first
ALL_ENGINES = (TRACE, *ENGINES)
SOURCE_MAX_SIDE = 1600  # the capture copy DetectFaces sees and the crop is cut from
TARGET_MAX_SIDE = 1600  # result images are shrunk to this before CompareFaces (its limit is 5 MB)
FACE_MARGIN = 0.5  # crop padding on each side, as a fraction of the face's width and height
CROP_QUALITY = 90
MAX_CANDIDATES = 40
COMPARE_CONCURRENCY = 8
COMPARE_TIMEOUT_S = 45.0
DOWNLOAD_TIMEOUT_S = 8.0
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = ("image/", "application/octet-stream")  # Pillow decides whether it really is an image
# Face detection, the searches, and comparing, on top of the usual per-image deadline.
EXTRA_DEADLINE_S = 10.0 + SEARCH_TIMEOUT_S + COMPARE_TIMEOUT_S
TRACE_EXTRA_DEADLINE_S = 10.0 + TRACE_TIMEOUT_S + COMPARE_TIMEOUT_S  # Trace takes 25-45 s, more if queued
RESULT_FIELDS = ("title", "source", "snippet")
SCORE_BANDS = ((99, "strong match"), (95, "likely match"), (90, "possible match"), (80, "weak match"),
               (0, "unlikely match"))
# Rekognition error codes -> BackendError reasons; any other code is provider_error.
AWS_REASONS = {
    "InvalidParameterException": "no_face",  # CompareFaces found no face in an image
    "InvalidImageFormatException": "unsupported_image",
    "ImageTooLargeException": "image_too_large",
    "AccessDeniedException": "unauthorized",
    "UnrecognizedClientException": "unauthorized",
    "InvalidSignatureException": "unauthorized",
    "ExpiredTokenException": "unauthorized",
    "ThrottlingException": "rate_limited",
    "ProvisionedThroughputExceededException": "rate_limited",
    "LimitExceededException": "rate_limited",
    "InternalServerError": "server_error",
}

FACE_SCORE_NOTE = """\
Each hit has a face_score from 0 to 100: Amazon Rekognition's similarity between the participant's face
and the closest face in that hit's image. Hits under {minimum:g} were already dropped. 99+ is a strong
match, 95-98 likely, 90-94 possible, lower is weak. Prefer high scores, and remember that even 99 is not
proof: different people look alike, and a page can show the participant's photo without being about them."""


def score_band(score: int) -> str:
    return next(label for floor, label in SCORE_BANDS if score >= floor)


def rekognition_client(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("rekognition", region_name=region, config=Config(
        connect_timeout=5, read_timeout=15, max_pool_connections=32, retries={"max_attempts": 3, "mode": "standard"}))


def require_rekognition(settings: Settings) -> None:
    """Fail before any search when Rekognition can't be called: no AWS_REGION or no AWS credentials.
    Only looks the credentials up; makes no AWS request."""
    hint = ("save an access key with `python -m pipeline set-key aws --region us-east-1` (or set AWS_PROFILE), "
            "set SEARCH_BACKEND=facecheck or serpapi, or pass --mock")
    if not settings.aws_region:
        raise ConfigError(f"backend V3 compares faces with Amazon Rekognition and needs AWS_REGION; {hint}")
    import boto3
    from botocore.exceptions import BotoCoreError

    try:
        credentials = boto3.session.Session().get_credentials()
    except BotoCoreError as exc:
        raise ConfigError(f"AWS credentials for Amazon Rekognition couldn't be loaded ({type(exc).__name__}); "
                          f"{hint}") from None
    if credentials is None:
        raise ConfigError("backend V3 compares faces with Amazon Rekognition, but no AWS credentials were found; "
                          + hint)


def aws_error(exc: Exception) -> BackendError | None:
    """The BackendError for a boto3 failure, or None if exc isn't one."""
    from botocore.exceptions import (
        BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError, NoCredentialsError,
        PartialCredentialsError, ReadTimeoutError,
    )

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return BackendError(AWS_REASONS.get(code, "provider_error"),
                            status_code=status if isinstance(status, int) else None)
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return BackendError("unauthorized")
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        return BackendError("timeout")
    if isinstance(exc, EndpointConnectionError):
        return BackendError("connection_error")
    if isinstance(exc, BotoCoreError):
        return BackendError("provider_error")
    return None


def face_crop(image: bytes, box: dict, margin: float = FACE_MARGIN) -> bytes:
    """The face in box (Rekognition's BoundingBox, as fractions of the image size), padded by margin
    times the face's width and height on each side and clipped to the image, as a JPEG."""
    from PIL import Image

    with Image.open(io.BytesIO(image)) as opened:
        width, height = opened.size
        left, top, w, h = (float(box[name]) for name in ("Left", "Top", "Width", "Height"))
        bounds = (max(0, round((left - margin * w) * width)), max(0, round((top - margin * h) * height)),
                  min(width, round((left + w + margin * w) * width)),
                  min(height, round((top + h + margin * h) * height)))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise BackendError("no_face")
        output = io.BytesIO()
        opened.crop(bounds).convert("RGB").save(output, "JPEG", quality=CROP_QUALITY)
    return output.getvalue()


def best_similarity(reply: dict) -> float | None:
    """The highest similarity among the faces CompareFaces found in the target image: 0 if it found
    faces but matched none, None if it found no face."""
    values = [match["Similarity"] for match in reply.get("FaceMatches") or []
              if isinstance(match, dict) and isinstance(match.get("Similarity"), (int, float))
              and not isinstance(match["Similarity"], bool)]
    if values:
        return max(0.0, min(100.0, float(max(values))))
    return 0.0 if reply.get("UnmatchedFaces") else None


def sanitize_engine(engine: str, payload: dict) -> dict:
    """Keep what V3 uses from one engine's reply: its id, status and error text, and for each result
    the page link, title, source, snippet, thumbnail and image URLs (full-size first). Drops
    search_parameters, which echoes the hosted image URL, and everything else SerpApi adds."""
    clean: dict = {}
    metadata = payload.get("search_metadata")
    if isinstance(metadata, dict):
        clean["search_metadata"] = {name: metadata[name] for name in ("id", "status")
                                    if isinstance(metadata.get(name), str)}
    if isinstance(payload.get("error"), str):
        clean["error"] = payload["error"][:300]
    results = []
    for key in ENGINES[engine][1]:
        rows = payload.get(key)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("link"), str):
                continue
            full = _thumbnail(row.get("original_image") if engine == "yandex_images" else row.get("image"))
            thumbnail = _thumbnail(row.get("thumbnail"))
            result = {name: row[name] for name in RESULT_FIELDS if isinstance(row.get(name), str)}
            result.update(link=row["link"], thumbnail=thumbnail,
                          images=[url for url in dict.fromkeys((full, thumbnail)) if url])
            results.append(result)
    clean["results"] = results
    return clean


def _failed(clean: dict) -> bool:
    """Whether a sanitized reply is a failure. SerpApi's "hasn't returned any results" is zero results."""
    status, error = clean.get("search_metadata", {}).get("status"), clean.get("error")
    if error and not (status in (None, "Success") and NO_RESULTS.search(error)):
        return True
    return status not in (None, "Success")


def trace_results(scan_id: str, scan: dict) -> dict:
    """A finished Trace scan in the sanitized engine shape: per match the page link, a title from the
    handle and platform, Trace's score in the snippet, and the preview URL (fetched with the key)."""
    results = []
    for index, match in enumerate(scan.get("matches") or []):
        if not isinstance(match, dict) or not isinstance(match.get("url"), str):
            continue
        score, tier = match.get("score"), match.get("tier")
        platform = match.get("platform") if isinstance(match.get("platform"), str) else ""
        handle = match.get("handle") if isinstance(match.get("handle"), str) else ""
        preview = match.get("preview_url")
        if not is_preview_url(preview):
            preview = f"https://traceaifacescan.app/api/v1/scans/{scan_id}/previews/{index}"
        results.append({
            "link": match["url"],
            "title": f"{handle} on {platform}" if handle and platform else handle or platform,
            "source": platform if platform and platform != "web" else _domain(match["url"]),
            "snippet": (f"Trace face match {score}/100" + (f" ({tier.replace('_', ' ')})" if isinstance(tier, str) else ""))
                       if isinstance(score, int) and not isinstance(score, bool) else "Trace face match",
            "thumbnail": None, "images": [], "previews": [preview],
            "trace_score": score if isinstance(score, int) and not isinstance(score, bool) else None,
        })
    results.sort(key=lambda row: -(row["trace_score"] or 0))
    return {"search_metadata": {"id": scan_id, "status": "Success"}, "results": results}


@dataclass
class Face:
    crop: bytes  # JPEG of the participant's padded face: what gets searched and compared
    faces: int  # how many faces DetectFaces found in the capture image
    copy: bytes = b""  # the upright copy the crop came from; Trace gets it when the crop is under 200 px
    crop_size: tuple[int, int] = (0, 0)
    digest: str = ""  # SHA-256 of the capture image file


@dataclass
class Candidate:
    url: str  # normalized page URL, the merge key
    row: dict  # the first engine's result for it
    full: list[str] = field(default_factory=list)  # full-size image URLs from every engine
    thumbnails: list[str] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)  # Trace previews; they need the Trace key
    engines: list[str] = field(default_factory=list)

    @property
    def images(self) -> list[str]:
        """Public image URLs to try in order: full-size images first, then thumbnails."""
        return self.full + self.thumbnails

    def sources(self) -> list[tuple[str, bool]]:
        """(url, is_trace_preview) in the order to try: Trace's preview of the matched face first."""
        return [(url, True) for url in self.previews] + [(url, False) for url in self.images]


def merge(results: dict[str, dict]) -> list[Candidate]:
    """One candidate per page URL: all of Trace's matches first, then each SerpApi engine's results
    in turn by rank."""
    candidates: dict[str, Candidate] = {}

    def add(engine: str, row: dict) -> None:
        url = normalize_url(row["link"])
        if url is None:
            return
        candidate = candidates.setdefault(url, Candidate(url=url, row=row))
        candidate.engines.append(engine)
        candidate.previews += [preview for preview in row.get("previews", []) if preview not in candidate.previews]
        for image in row.get("images", []):
            if image not in candidate.images:
                (candidate.thumbnails if image == row.get("thumbnail") else candidate.full).append(image)

    for row in results.get(TRACE, {}).get("results", []):
        add(TRACE, row)
    lists = [(engine, reply["results"]) for engine, reply in results.items() if engine != TRACE]
    for rank in range(max((len(rows) for _, rows in lists), default=0)):
        for engine, rows in lists:
            if rank < len(rows):
                add(engine, rows[rank])
    return list(candidates.values())


class RekognitionBackend(SerpApiBackend):
    """Trace and SerpApi searches of a face crop, filtered by Rekognition CompareFaces. Page fetches,
    image hosting, release() and aclose() build on SerpApi's (serpapi_backend.SerpApiBackend).
    api_key and host are needed only for the SerpApi engines, trace_search only for Trace."""

    name = "rekognition"

    def __init__(self, *, api_key: str | None, capture_dir: Path, host, rekognition, engines=tuple(ENGINES),
                 trace_search: TraceFaceSearch | None = None, min_similarity: float = DEFAULT_MIN_SIMILARITY,
                 platforms: tuple[str, ...] = (),
                 cache_dir: Path | None = None, refresh: bool = False,
                 search_client: httpx.AsyncClient | None = None, pages: httpx.AsyncClient | None = None,
                 images: httpx.AsyncClient | None = None, compare_timeout: float = COMPARE_TIMEOUT_S):
        unknown = [engine for engine in engines if engine not in ALL_ENGINES]
        if unknown or not engines:
            raise ValueError(f"unsupported engines {unknown}; expected some of {', '.join(ALL_ENGINES)}")
        if TRACE in engines and trace_search is None:
            raise ValueError("the trace engine needs a TraceFaceSearch")
        if any(engine in ENGINES for engine in engines) and (not api_key or host is None):
            raise ValueError("the SerpApi engines need a SerpApi key and an image host")
        super().__init__(api_key=api_key or "", capture_dir=capture_dir, host=host, cache_dir=cache_dir,
                         refresh=refresh, search_client=search_client, pages=pages)
        self.rekognition = rekognition  # a boto3 Rekognition client, or a test double with the same two methods
        self.trace_search = trace_search
        self.engines = tuple(engines)
        self.extra_deadline_s = TRACE_EXTRA_DEADLINE_S if TRACE in self.engines else EXTRA_DEADLINE_S
        self.min_similarity = float(min_similarity)
        self.platforms = tuple(platforms)  # platform_for() names to keep; empty keeps every site
        self.images = images or page_client(accept="image/*")
        self.compare_timeout = compare_timeout
        self.face_score_note = FACE_SCORE_NOTE.format(minimum=self.min_similarity)  # for the per-image agent
        self._faces: dict[str, Face] = {}
        self._results: dict[str, dict[str, dict]] = {}  # image_id -> engine -> sanitized reply
        self._locks: dict[str, asyncio.Lock] = {}

    # Search

    async def reverse_image(self, image: CaptureImage) -> dict:
        data = await asyncio.to_thread(self._image_bytes, image)
        digest = hashlib.sha256(data).hexdigest()
        final = self._final_path(digest)
        cached = await asyncio.to_thread(self._read_cache, final, image)
        if cached is not None:
            self.cache_hits.add(image.image_id)
            trace.event("search_cache_hit", image=image.image_id)
            return cached
        async with self._locks.setdefault(image.image_id, asyncio.Lock()):
            face = self._faces.get(image.image_id)
            if face is None:
                face = self._faces[image.image_id] = await self._find_face(image, data)
                face.digest = digest
            results, failed = await self._search_engines(image, digest, face)
            payload, complete = await self._match(image, results, failed, face.crop)
        if final is not None and complete:
            await asyncio.to_thread(_write_json, final, payload)
        return payload

    def _final_path(self, digest: str) -> Path | None:
        if self.cache_dir is None:
            return None
        scope = f"-{'+'.join(self.platforms)}" if self.platforms else ""
        return self.cache_dir / f"{digest}-v3-{'+'.join(self.engines)}-{self.min_similarity:g}{scope}.json"

    def _engine_path(self, digest: str, engine: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / f"{digest}-{engine}.json"

    def _read_cache(self, path: Path | None, image: CaptureImage) -> dict | None:
        if path is None or self.refresh or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            trace.error("search_cache_unreadable", exc, image=image.image_id)
            return None
        return payload if isinstance(payload, dict) else None

    async def _find_face(self, image: CaptureImage, data: bytes) -> Face:
        from pipeline.hosting import HostingError, search_copy

        try:
            copy = await asyncio.to_thread(search_copy, data, SOURCE_MAX_SIDE)
        except HostingError:
            raise BackendError("unsupported_image") from None
        reply = await self._aws("DetectFaces", image, Image={"Bytes": copy}, Attributes=["DEFAULT"])
        faces = [face for face in reply.get("FaceDetails") or []
                 if isinstance(face, dict) and isinstance(face.get("BoundingBox"), dict)]
        if not faces:
            trace.error("no_face_in_capture", image=image.image_id)
            raise BackendError("no_face")
        largest = max(faces, key=lambda face: _area(face["BoundingBox"]))
        try:
            crop = await asyncio.to_thread(face_crop, copy, largest["BoundingBox"])
        except (KeyError, TypeError, ValueError):
            raise BackendError("invalid_response") from None
        crop_size = await asyncio.to_thread(_size, crop)
        trace.event("capture_face", image=image.image_id, faces=len(faces), confidence=largest.get("Confidence"),
                    crop_bytes=len(crop), crop_size=list(crop_size))
        return Face(crop=crop, faces=len(faces), copy=copy, crop_size=crop_size)

    async def _search_engines(self, image: CaptureImage, digest: str, face: Face) -> tuple[dict[str, dict], list[str]]:
        """Each engine's sanitized reply, in REKOGNITION_ENGINES order, and the engines that failed.
        Engines that already answered for this image, in this run or (SerpApi only) its cache,
        aren't searched again."""
        answered = self._results.setdefault(image.image_id, {})
        for engine in self.engines:
            if engine not in answered and engine != TRACE:
                cached = await asyncio.to_thread(self._read_cache, self._engine_path(digest, engine), image)
                if cached is not None and isinstance(cached.get("results"), list):
                    answered[engine] = cached
                    trace.event("search_cache_hit", image=image.image_id, engine=engine)
        missing = [engine for engine in self.engines if engine not in answered]
        failures: dict[str, BackendError] = {}
        if missing:
            outcomes = await asyncio.gather(*(self._search_engine(image, engine, face) for engine in missing),
                                            return_exceptions=True)
            for engine, outcome in zip(missing, outcomes):
                if isinstance(outcome, BackendError):
                    failures[engine] = outcome
                elif isinstance(outcome, BaseException):
                    raise outcome
                else:
                    answered[engine] = outcome
                    path = self._engine_path(digest, engine) if engine != TRACE else None
                    if path is not None:
                        await asyncio.to_thread(_write_json, path, outcome)
            if failures:
                trace.error("engine_failed", image=image.image_id,
                            engines={engine: error.reason for engine, error in failures.items()})
        if not answered:
            errors = list(failures.values())
            raise next((error for error in errors if error.retryable), errors[0])
        return {engine: answered[engine] for engine in self.engines if engine in answered}, list(failures)

    async def _search_engine(self, image: CaptureImage, engine: str, face: Face) -> dict:
        if engine == TRACE:
            return await self._search_trace(image, face)
        url = await self._hosted_url(image, face.crop)  # once per image; a hosting failure fails only SerpApi
        extra, _ = ENGINES[engine]
        clean = sanitize_engine(engine, await self._get(image, {"engine": engine, "url": url, **extra}))
        if _failed(clean):
            trace.error("serpapi_error", image=image.image_id, engine=engine,
                        status=clean.get("search_metadata", {}).get("status"), message=clean.get("error"))
            raise BackendError("provider_error")
        if clean.get("error"):
            clean["results"] = []  # "hasn't returned any results"
        trace.event("serpapi_search", image=image.image_id, engine=engine,
                    request_id=clean.get("search_metadata", {}).get("id"), results=len(clean["results"]))
        return clean

    async def _search_trace(self, image: CaptureImage, face: Face) -> dict:
        upload = face.crop if min(face.crop_size) >= TRACE_MIN_SIDE else face.copy
        # The same photo within 24 hours gets the same scan: a rerun after a timeout resumes it unpaid.
        key = hashlib.sha256(f"hackmidwest-v3-trace:{face.digest}".encode()).hexdigest()[:32]
        scan_id, scan = await self.trace_search.search(image.image_id, upload, key=key)
        clean = trace_results(scan_id, scan)
        trace.event("trace_search", image=image.image_id, scan=scan_id, results=len(clean["results"]),
                    best_score=max((row["trace_score"] or 0 for row in clean["results"]), default=None))
        return clean

    async def release(self, image: CaptureImage) -> None:
        await super().release(image)
        if self.trace_search is not None:
            await self.trace_search.delete(image.image_id)  # the uploaded crop and the previews

    # Face matching

    async def _match(self, image: CaptureImage, results: dict[str, dict], failed: list[str],
                     crop: bytes) -> tuple[dict, bool]:
        """The Yandex-shaped payload of the results whose face matches, and whether it's complete
        enough to cache (every engine answered, every result compared)."""
        candidates = merge(results)
        outcomes: Counter[str] = Counter()
        in_scope = [candidate for candidate in candidates
                    if not self.platforms or platform_for(candidate.url) in self.platforms]
        if len(candidates) > len(in_scope):
            outcomes["out_of_scope"] = len(candidates) - len(in_scope)
        # Every Trace match is compared (Trace returns at most 50); the cap limits the SerpApi results.
        from_trace = [candidate for candidate in in_scope if TRACE in candidate.engines]
        compared = from_trace + [candidate for candidate in in_scope if TRACE not in candidate.engines][:MAX_CANDIDATES]
        if len(in_scope) > len(compared):
            outcomes["over_limit"] = len(in_scope) - len(compared)
        matches = []
        for candidate, (outcome, similarity) in zip(compared, await self._compare_all(image, crop, compared)):
            outcomes[outcome] += 1
            trace.event("face_compare", image=image.image_id, url=candidate.url, engines=candidate.engines,
                        outcome=outcome, similarity=None if similarity is None else round(similarity, 2))
            if outcome == "matched":
                matches.append((similarity, candidate))
        matches.sort(key=lambda match: -match[0])  # stable: merge order breaks ties

        rows = []
        for position, (similarity, candidate) in enumerate(matches, 1):
            row = candidate.row
            rows.append({"position": position, "link": row["link"], "title": row.get("title", ""),
                         "source": row.get("source") or _domain(row["link"]), "snippet": row.get("snippet", ""),
                         **({"thumbnail": row["thumbnail"]} if row.get("thumbnail") else {}),
                         "face_score": min(100, int(similarity))})
        ids = [reply.get("search_metadata", {}).get("id") for reply in results.values()]
        ids = [request_id for request_id in ids if isinstance(request_id, str)]
        metadata = {"status": "Success", **({"id": ",".join(ids)} if ids else {})}
        info = {"total_results": len(rows), "engines": list(results), "failed_engines": failed,
                "min_similarity": self.min_similarity, "candidates": len(candidates), "outcomes": dict(outcomes),
                **({"platforms": list(self.platforms)} if self.platforms else {})}
        trace.event("face_matching", image=image.image_id, **info)
        complete = not failed and not outcomes["not_compared"] and not outcomes["compare_error"]
        return {"search_metadata": metadata, "search_information": info, "image_results": rows}, complete

    async def _compare_all(self, image: CaptureImage, crop: bytes,
                           candidates: list[Candidate]) -> list[tuple[str, float | None]]:
        """(outcome, similarity) for each candidate, in order, within compare_timeout. Raises at once
        if Rekognition refuses the credentials, since every other comparison would fail the same way."""
        if not candidates:
            return []
        slots = asyncio.Semaphore(COMPARE_CONCURRENCY)

        async def one(candidate: Candidate) -> tuple[str, float | None]:
            async with slots:
                return await self._score(image, crop, candidate)

        tasks = [asyncio.ensure_future(one(candidate)) for candidate in candidates]
        try:
            await asyncio.wait(tasks, timeout=self.compare_timeout, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for task in tasks:
                task.cancel()  # no effect on finished tasks
            await asyncio.gather(*tasks, return_exceptions=True)
        scored = []
        for task in tasks:
            if task.cancelled():
                scored.append(("not_compared", None))
            elif task.exception() is not None:
                raise task.exception()
            else:
                scored.append(task.result())
        return scored

    async def _score(self, image: CaptureImage, crop: bytes, candidate: Candidate) -> tuple[str, float | None]:
        """Compare the crop with the candidate's images in order until one shows a face. An image
        that won't load or has no face moves on to the next, usually the engine's thumbnail."""
        if not candidate.sources():
            return "no_image", None
        outcome = "image_unavailable"  # becomes no_face once an image loads but shows no face
        for url, is_preview in candidate.sources():
            try:
                target = await (self._preview_image(url) if is_preview else self._download_image(url))
            except BackendError:
                continue
            try:
                reply = await self._aws("CompareFaces", image, SourceImage={"Bytes": crop},
                                        TargetImage={"Bytes": target}, SimilarityThreshold=0.0)
            except BackendError as error:
                if error.reason == "unauthorized":
                    raise
                if error.reason == "no_face":
                    outcome = "no_face"
                    continue
                if error.reason in ("unsupported_image", "image_too_large"):
                    continue
                return "compare_error", None
            similarity = best_similarity(reply)
            if similarity is None:
                outcome = "no_face"
                continue
            return ("matched" if similarity >= self.min_similarity else "below_threshold"), similarity
        return outcome, None

    async def _aws(self, operation: str, image: CaptureImage, **params) -> dict:
        methods = {"DetectFaces": self.rekognition.detect_faces, "CompareFaces": self.rekognition.compare_faces}
        method = methods[operation]
        try:
            reply = await asyncio.to_thread(method, **params)
        except Exception as exc:
            error = aws_error(exc)
            if error is None:
                raise
            if error.reason != "no_face":
                trace.error("rekognition_error", exc, image=image.image_id, operation=operation, reason=error.reason)
            raise error from None
        if not isinstance(reply, dict):
            raise BackendError("invalid_response")
        return reply

    # Result images

    async def _download_image(self, url: str) -> bytes:
        """One result image as a JPEG CompareFaces accepts; raises BackendError."""
        try:
            data = await asyncio.wait_for(self._download(url), timeout=DOWNLOAD_TIMEOUT_S)
        except TimeoutError:
            raise BackendError("timeout") from None
        return await _comparable(data)

    async def _preview_image(self, url: str) -> bytes:
        """Trace's preview of a matched face, as a JPEG CompareFaces accepts; raises BackendError."""
        if self.trace_search is None:
            raise BackendError("unsupported_content")
        try:
            data = await asyncio.wait_for(self.trace_search.preview(url), timeout=DOWNLOAD_TIMEOUT_S)
        except TimeoutError:
            raise BackendError("timeout") from None
        return await _comparable(data)

    async def _download(self, url: str) -> bytes:
        """GET a public image, checking every redirect hop like page fetches; at most MAX_IMAGE_BYTES."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            try:
                async with self.images.stream("GET", current) as response:
                    if response.is_redirect and "location" in response.headers:
                        current = urldefrag(urljoin(current, response.headers["location"]))[0]
                        continue
                    if response.status_code >= 400:
                        raise status_error(response.status_code, response.headers)
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not content_type.startswith(IMAGE_TYPES):
                        raise BackendError("unsupported_content", status_code=response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_IMAGE_BYTES:
                            raise BackendError("image_too_large")
                    return bytes(body)
            except httpx.TimeoutException:
                raise BackendError("timeout") from None
            except httpx.HTTPError as error:
                blocked = isinstance(error.__cause__, BlockedDestination)
                raise BackendError("blocked_destination" if blocked else "connection_error") from None
        raise BackendError("too_many_redirects")

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self.images.aclose()
            if self.trace_search is not None:
                await self.trace_search.aclose()  # deletes any scan still at Trace


async def _comparable(data: bytes) -> bytes:
    """Untrusted image bytes as a JPEG of at most TARGET_MAX_SIDE px for CompareFaces."""
    from pipeline.hosting import search_copy

    try:
        return await asyncio.to_thread(search_copy, data, TARGET_MAX_SIDE)
    except Exception:  # untrusted bytes: any decoder failure (HostingError or worse) costs this result only
        raise BackendError("unsupported_image") from None


def _size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        return opened.size


def _area(box: dict) -> float:
    width, height = box.get("Width"), box.get("Height")
    return width * height if isinstance(width, (int, float)) and isinstance(height, (int, float)) else 0.0


def _domain(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def build_rekognition_backend(settings: Settings, capture: CaptureArtifact, run_dir: Path, *,
                              refresh: bool) -> RekognitionBackend:
    """Backend V3 for a run: AWS_REGION and AWS credentials, TRACE_API_KEY for the trace engine, and
    SERPAPI_KEY plus an image host for the SerpApi engines. Credentials are looked up before the
    tunnel opens, so a run without them stops before any search."""
    engines = settings.rekognition_engines
    if settings.serpapi_engines and not settings.serpapi_key:
        raise ConfigError("SERPAPI_KEY is missing; save it with `python -m pipeline set-key serpapi`, or pass --mock")
    if TRACE in engines and not settings.trace_api_key:
        raise ConfigError("TRACE_API_KEY is missing; save it with `python -m pipeline set-key trace`, "
                          "or drop trace from REKOGNITION_ENGINES")
    require_rekognition(settings)
    client = rekognition_client(settings.aws_region)
    return RekognitionBackend(api_key=settings.serpapi_key, capture_dir=Path(capture.capture_dir),
                              host=open_host(settings, run_dir) if settings.needs_hosting else None,
                              rekognition=client, engines=engines,
                              trace_search=TraceFaceSearch(settings.trace_api_key) if TRACE in engines else None,
                              min_similarity=settings.rekognition_min_similarity,
                              platforms=settings.rekognition_platforms,
                              cache_dir=Path(run_dir) / "cache" / "rekognition", refresh=refresh)
