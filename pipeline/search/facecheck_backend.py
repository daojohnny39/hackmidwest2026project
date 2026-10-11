"""FaceCheck.ID face search (spec 1.1, 1.7; live only).

FaceCheck matches faces by identity against its own index of faces from public web
pages. Yandex (serpapi_backend.py) returns visually similar images instead, so it
mostly finds lookalikes. One search per image:

1. POST /api/upload_pic with the image (multipart field "images") returns id_search.
2. POST /api/search {id_search, with_progress, status_only: false, demo} is polled
   until output arrives or error is set. Each item has a 0-100 face match score, the
   page URL (url.value), and a base64 thumbnail.

A search costs 3 credits ($0.30) unless FACECHECK_DEMO=true, which scans only the
first 100,000 faces for free; demo results aren't meaningful. The upload is a JPEG
copy at most 1,600 px on its longest side with EXIF stripped. A retry polls the
image's existing id_search instead of uploading again, so it never starts a second
paid search. Results are converted to the Yandex-shaped payload parse_image_results()
reads, with face_score on each row, and cached for this run under
runs/<run_id>/cache/facecheck/, keyed by image hash and demo mode. Thumbnails are
dropped: they show faces of people other than the participant.

Errors arrive on HTTP 200 with error and code fields. The token goes only in the
Authorization header, never in a URL, and is replaced in any provider message
before it's traced.

Score bands, from FaceCheck: 90-100 certain, 83-89 confident, 70-82 uncertain,
50-69 weak. Page fetches are SerpApi's guarded live fetches (serpapi_backend.LivePages).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pipeline import trace
from pipeline.config import ConfigError, Settings
from pipeline.contracts import CaptureArtifact, CaptureImage
from pipeline.search.base import BackendError, status_error
from pipeline.search.serpapi_backend import USER_AGENT, LivePages, _write_json, page_client

FACECHECK_URL = "https://facecheck.id"
UPLOAD_PATH, SEARCH_PATH, INFO_PATH = "/api/upload_pic", "/api/search", "/api/info"
CREDITS_PER_SEARCH = 3
CREDIT_USD = 0.10
REQUEST_TIMEOUT_S = 30.0
INFO_TIMEOUT_S = 10.0
SEARCH_TIMEOUT_S = 180.0  # queue plus search, per image; stage 1 adds it to the image deadline
POLL_INTERVAL_S = 2.0
UPLOAD_MAX_SIDE = 1600
SCORE_BANDS = ((90, "certain match"), (83, "confident match"), (70, "uncertain match"), (50, "weak match"),
               (0, "unlikely match"))
INFO_FIELDS = ("remaining_credits", "has_credits_to_search", "is_online", "faces")


class TokenRejected(Exception):
    """FaceCheck refused the API token."""


class InfoCheckFailed(Exception):
    """FaceCheck couldn't be reached or answered unexpectedly; the token may still be valid."""


def score_band(score: int) -> str:
    return next(label for floor, label in SCORE_BANDS if score >= floor)


class FaceCheckBackend(LivePages):
    name = "facecheck"
    origin = None
    search_endpoint = SEARCH_PATH
    extra_deadline_s = SEARCH_TIMEOUT_S

    def __init__(self, *, api_key: str, capture_dir: Path, demo: bool = False, cache_dir: Path | None = None,
                 refresh: bool = False, client: httpx.AsyncClient | None = None,
                 pages: httpx.AsyncClient | None = None, poll_interval: float = POLL_INTERVAL_S,
                 search_timeout: float = SEARCH_TIMEOUT_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep):
        self.api_key = api_key
        self.capture_dir = Path(capture_dir)
        self.demo = demo
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, trust_env=False,
                                                  headers={"User-Agent": USER_AGENT})
        self.pages = pages or page_client()
        self.poll_interval, self.search_timeout = poll_interval, search_timeout
        self.clock, self.sleep = clock, sleep
        self.cache_hits: set[str] = set()
        self._searches: dict[str, str] = {}  # image_id -> id_search, so a retry resumes instead of paying again
        self._locks: dict[str, asyncio.Lock] = {}

    # Search

    def _image_bytes(self, image: CaptureImage) -> bytes:
        if not image.file:
            raise BackendError("invalid_request")
        # capture.py already checked the path stays inside the folder with no symlinks.
        return (self.capture_dir / image.file).read_bytes()

    def _cache_path(self, data: bytes) -> Path | None:
        if self.cache_dir is None:
            return None
        mode = "facecheck-demo" if self.demo else "facecheck"
        return self.cache_dir / f"{hashlib.sha256(data).hexdigest()}-{mode}.json"

    def needs_search(self, images: Iterable[CaptureImage]) -> bool:
        """Whether any image would make a new (paid) search instead of reading this run's cache."""
        if self.refresh or self.cache_dir is None:
            return True
        return any(not self._cache_path(self._image_bytes(image)).is_file() for image in images)

    async def reverse_image(self, image: CaptureImage) -> dict:
        data = await asyncio.to_thread(self._image_bytes, image)
        cache = self._cache_path(data)
        if cache is not None and not self.refresh and cache.is_file():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                self.cache_hits.add(image.image_id)
                trace.event("search_cache_hit", image=image.image_id)
                return payload
            except (OSError, ValueError) as exc:
                trace.error("search_cache_unreadable", exc, image=image.image_id)
        async with self._locks.setdefault(image.image_id, asyncio.Lock()):
            id_search = self._searches.get(image.image_id)
            if id_search is None:
                id_search = await self._upload(image, data)
                self._searches[image.image_id] = id_search
            else:
                trace.event("facecheck_resume", image=image.image_id)
            output = await self._poll(image, id_search)
        payload = to_payload(id_search, output, demo=self.demo)
        if cache is not None:
            await asyncio.to_thread(_write_json, cache, payload)
        return payload

    async def _upload(self, image: CaptureImage, data: bytes) -> str:
        from pipeline.hosting import HostingError, search_copy

        try:
            copy = await asyncio.to_thread(search_copy, data, UPLOAD_MAX_SIDE)
        except HostingError:
            raise BackendError("unsupported_image") from None
        reply = self._reply(await self._post(UPLOAD_PATH, files={"images": ("image.jpg", copy, "image/jpeg")}),
                            image, "upload")
        id_search = reply.get("id_search")
        if not isinstance(id_search, str) or not id_search:
            raise BackendError("invalid_response")
        trace.event("facecheck_upload", image=image.image_id, bytes=len(copy), message=reply.get("message"))
        return id_search

    async def _poll(self, image: CaptureImage, id_search: str) -> dict:
        started = self.clock()
        body = {"id_search": id_search, "with_progress": True, "status_only": False, "demo": self.demo}
        last = None
        while True:
            reply = self._reply(await self._post(SEARCH_PATH, json=body), image, "search")
            output = reply.get("output")
            if output:
                if not isinstance(output, dict) or not isinstance(output.get("items") or [], list):
                    raise BackendError("invalid_response")
                trace.event("facecheck_done", image=image.image_id, items=len(output.get("items") or []),
                            max_score=output.get("max_score"), took_s=output.get("tookSeconds"),
                            queue_s=output.get("tookSecondsQueue"), searched_faces=output.get("searchedFaces"),
                            demo=output.get("demo"))
                return output
            progress = reply.get("progress")
            if progress != last:
                trace.event("facecheck_progress", image=image.image_id, progress=progress, message=reply.get("message"))
                last = progress
            if self.clock() - started >= self.search_timeout:
                raise BackendError("timeout")
            await self.sleep(self.poll_interval)

    async def _post(self, path: str, **kwargs) -> httpx.Response:
        headers = {"accept": "application/json", "Authorization": self.api_key}
        try:
            response = await self.client.post(FACECHECK_URL + path, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError:
            raise BackendError("connection_error") from None
        if response.status_code != 200:
            trace.error("facecheck_http_error", path=path, status=response.status_code,
                        body=response.text[:2000].replace(self.api_key, "***"))
            raise status_error(response.status_code, response.headers)
        return response

    def _reply(self, response: httpx.Response, image: CaptureImage, step: str) -> dict:
        try:
            reply = response.json()
        except ValueError:
            raise BackendError("invalid_response") from None
        if not isinstance(reply, dict):
            raise BackendError("invalid_response")
        if reply.get("error"):
            code, message = str(reply.get("code") or ""), str(reply["error"]).replace(self.api_key, "***")
            error = BackendError(_reason(code, message))
            trace.error("facecheck_error", image=image.image_id, step=step, reason=error.reason, code=code[:40],
                        message=message[:300])
            raise error
        return reply

    async def release(self, image: CaptureImage) -> None:
        return None  # nothing is hosted; FaceCheck deletes search history within 24 hours

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.pages.aclose()


def _reason(code: str, message: str) -> str:
    text = f"{code} {message}".lower()
    if "token" in text:
        return "unauthorized"
    if "credit" in text:
        return "no_credits"
    return "provider_error"


def to_payload(id_search: str, output: dict, *, demo: bool) -> dict:
    """FaceCheck output -> the Yandex-shaped payload parse_image_results() reads, best match first.
    Keeps the page URL and score of each item and drops its thumbnail."""
    rows = []
    for item in output.get("items") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        url = url.get("value") if isinstance(url, dict) else url
        score = item.get("score")
        if not isinstance(url, str) or not isinstance(score, int) or isinstance(score, bool):
            continue
        score = max(0, min(100, score))
        rows.append({"link": url, "title": "", "source": _domain(url), "face_score": score,
                     "snippet": f"FaceCheck face match {score}/100 ({score_band(score)})"})
    rows.sort(key=lambda row: -row["face_score"])
    for position, row in enumerate(rows, 1):
        row["position"] = position
    information: dict = {"total_results": len(rows), "demo": bool(output.get("demo", demo))}
    if isinstance(output.get("searchedFaces"), int):
        information["searched_faces"] = output["searchedFaces"]
    return {"search_metadata": {"id": id_search, "status": "Success"}, "search_information": information,
            "image_results": rows}


def _domain(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def account_info(api_key: str) -> dict:
    """Remaining credits and engine status for this token. /api/info is free and runs no search."""
    try:
        response = httpx.post(FACECHECK_URL + INFO_PATH, timeout=INFO_TIMEOUT_S,
                              headers={"accept": "application/json", "Authorization": api_key})
    except httpx.HTTPError as exc:
        raise InfoCheckFailed(type(exc).__name__) from None
    if response.status_code in (401, 403):
        raise TokenRejected(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise InfoCheckFailed(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise InfoCheckFailed("response was not JSON") from None
    if not isinstance(data, dict):
        raise InfoCheckFailed("unexpected response")
    if data.get("error"):
        code = str(data.get("code") or "error")[:40]
        if _reason(code, str(data["error"])) == "unauthorized":
            raise TokenRejected(code)
        raise InfoCheckFailed(code)
    return {name: data[name] for name in INFO_FIELDS if name in data}


def describe_info(info: dict) -> str:
    parts = []
    credits = info.get("remaining_credits")
    if isinstance(credits, int):
        parts.append(f"{credits} credits left ({credits // CREDITS_PER_SEARCH} searches at {CREDITS_PER_SEARCH} "
                     f"credits, ${CREDITS_PER_SEARCH * CREDIT_USD:.2f} each)")
    if info.get("is_online") is False:
        parts.append("search engine offline")
    if isinstance(info.get("faces"), int):
        parts.append(f"{info['faces']:,} faces indexed")
    return ", ".join(parts) or "no account details returned"


def build_facecheck_backend(settings: Settings, capture: CaptureArtifact, run_dir: Path, *,
                            refresh: bool) -> FaceCheckBackend:
    """The FaceCheck backend for a run. When a paid search is needed (not demo mode, not all cached),
    it checks the account first with the free /api/info call, so a run with no credits stops before
    any upload."""
    if not settings.facecheck_api_key:
        raise ConfigError("FACECHECK_API_KEY is missing; save it with `python -m pipeline set-key facecheck`, "
                          "or pass --mock")
    backend = FaceCheckBackend(api_key=settings.facecheck_api_key, capture_dir=Path(capture.capture_dir),
                               demo=settings.facecheck_demo, cache_dir=Path(run_dir) / "cache" / "facecheck",
                               refresh=refresh)
    if settings.facecheck_demo or not backend.needs_search(capture.capture.images):
        return backend
    try:
        info = account_info(settings.facecheck_api_key)
    except TokenRejected as exc:
        raise ConfigError(f"FaceCheck rejected FACECHECK_API_KEY ({exc}); save a new token with "
                          "`python -m pipeline set-key facecheck`") from None
    except InfoCheckFailed as exc:
        trace.error("facecheck_info_failed", reason=str(exc))  # the search reports any real problem itself
        return backend
    trace.event("facecheck_account", **info)
    if info.get("has_credits_to_search") is False:
        raise ConfigError(f"the FaceCheck account has no search credits (each search costs {CREDITS_PER_SEARCH}); "
                          "buy credits at https://facecheck.id/en/buy, or set FACECHECK_DEMO=true for free demo "
                          "searches")
    return backend
