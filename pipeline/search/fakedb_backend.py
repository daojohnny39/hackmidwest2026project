"""Dev mode: search the team's fake image database instead of the internet (SEARCH_BACKEND=fakedb,
`python -m pipeline serve --fake-db`, or `npm run dev:fake` in frontend/).

The database is Zach's image browser at FAKE_DB_URL (default: his server on the team tailnet). It
holds the 202,599 CelebA photos, each with a made-up name and the site it was "found on", plus photos
the team uploads through its Settings > Upload image dialog with a JSON file such as
{"first_name": "Mark", "last_name": "Smith", "foundOn": ["anonfacez.icu", "instagram.com"]}.
Its API (GET /api/images, GET /img/<file>) has no face search, and comparing every CelebA photo would
cost about $200 in Rekognition calls, so the candidates are the uploaded photos only: rows in the
first and last PAGE_SIZE of the list whose file name isn't a CelebA one (six digits). An upload lands
there as long as its file name starts with a letter. The name typed into the website's form is never
sent to the database.

Each uploaded row becomes one result per site in its foundOn, at a made-up URL on that site
(source_url): an account URL on the sites platforms.py knows accounts for, a member profile on
FRAUD_SITE, and /photos/<file> elsewhere.
Then backend V3 takes over (rekognition_backend.py): DetectFaces finds the user's face in the capture,
CompareFaces scores each row's photo against it, and rows under REKOGNITION_MIN_SIMILARITY are
dropped. So the photo still goes to Amazon Rekognition, and runs need external_processing consent
like live search.

The fake sites don't exist, so fetch() serves a small made-up page for each result URL, built from its
row. On FRAUD_SITE that page is a fake profile pretending to be the person. FakeDbThreats marks
FRAUD_SITE as a malicious fraud site (high) and every other site as benign (low), to show the threat
model on fake data.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from pipeline import trace
from pipeline.config import ConfigError, Settings
from pipeline.contracts import CaptureArtifact, CaptureImage, FetchedPage
from pipeline.findings import Findings
from pipeline.platforms import normalize_url
from pipeline.search.base import BackendError, status_error
from pipeline.search.rekognition_backend import (
    COMPARE_TIMEOUT_S, MAX_IMAGE_BYTES, Face, RekognitionBackend, rekognition_client, require_rekognition,
)

FAKEDB = "fakedb"  # the backend's one engine
PAGE_SIZE = 500  # the database's largest page
REQUEST_TIMEOUT_S = 10.0
CHECK_TIMEOUT_S = 5.0
CELEBA_FILE = re.compile(r"\d{6}\.(?:png|jpe?g)", re.IGNORECASE)
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")  # the names the database's upload accepts
SITE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
FRAUD_SITE = "evilassociation.hakr"  # the one malicious site in dev mode's threat model
NAME_LIMIT = 100
MAX_SITES = 10  # per row
# Sites where platforms.py recognizes account URLs, so the made-up page there is an account.
ACCOUNT_PATHS = {"linkedin.com": "/in/{handle}", "x.com": "/{handle}", "instagram.com": "/{handle}",
                 "facebook.com": "/{handle}", "github.com": "/{handle}"}
# The threat FakeDbThreats gives each source, by whether its site is FRAUD_SITE.
FRAUD_THREAT = {"score": 100, "level": "high", "indicators": [
    f"Dev mode marks {FRAUD_SITE} as a malicious fraud site.",
    "The page is a fake profile that uses this photo to pass as you."]}
BENIGN_THREAT = {"score": 0, "level": "low", "indicators": [
    f"Dev mode marks every site except {FRAUD_SITE} as benign."]}


@dataclass(frozen=True)
class Row:
    """One photo in the database."""

    file_name: str
    name: str  # first, middle and last name; "" when it has none
    sites: tuple[str, ...]  # its foundOn hosts, without "www."


def parse_rows(payload: dict) -> list[Row]:
    """The usable rows of one /api/images page. Rows with an unsafe file name or no valid site are skipped."""
    items = payload.get("items")
    if not isinstance(items, list):
        raise BackendError("invalid_response")
    rows = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("file_name"), str):
            continue
        if not FILE_NAME.fullmatch(item["file_name"]):
            continue
        found = item.get("foundOn")
        found = [found] if isinstance(found, str) else found if isinstance(found, list) else []
        sites = [site.strip().lower().removeprefix("www.") for site in found if isinstance(site, str)]
        sites = list(dict.fromkeys(site for site in sites if SITE.fullmatch(site)))[:MAX_SITES]
        if not sites:
            continue
        parts = [item.get(name) for name in ("first_name", "middle_name", "last_name")]
        name = " ".join(" ".join(part.split()) for part in parts if isinstance(part, str) and part.strip())
        rows.append(Row(file_name=item["file_name"], name=name[:NAME_LIMIT], sites=tuple(sites)))
    return rows


def source_url(row: Row, site: str) -> str:
    """The made-up page URL for a row on a site."""
    stem = row.file_name.rsplit(".", 1)[0]
    handle = f"{re.sub(r'[^a-z0-9]', '', row.name.lower()) or 'user'}_{stem}"
    if site == FRAUD_SITE:
        return f"https://{site}/members/{handle}"
    path = ACCOUNT_PATHS.get(site)
    if path:
        return f"https://{site}{path.format(handle=handle)}"
    return f"https://{site}/photos/{stem}"


def page_html(row: Row, site: str, image_url: str) -> str:
    """The made-up page fetch() serves for a row on a site: its name, the site, and the photo. On
    FRAUD_SITE it's a member profile (a JSON-LD Person) with a scam pitch."""
    head = ""
    if site == FRAUD_SITE:
        name = row.name or "Verified member"
        title = f"{name} | Member profile"
        text = ("Verified investment advisor. Guaranteed 300% returns in 30 days. Send a deposit in gift cards "
                "or crypto to reserve your spot, and message me privately for details.")
        person = json.dumps({"@context": "https://schema.org", "@type": "Person", "name": name, "image": image_url})
        head = '<script type="application/ld+json">' + person.replace("</", "<\\/") + "</script>"
    elif row.name:
        title, text = f"{row.name} on {site}", f"Photo posted by {row.name} on {site}."
    else:
        title, text = f"Photo on {site}", f"Photo posted on {site} without a name."
    title, text, image = html.escape(title), html.escape(text), html.escape(image_url)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
            f'<meta property="og:title" content="{title}"><meta property="og:image" content="{image}">'
            f'<meta name="description" content="{text}">{head}</head>'
            f'<body><h1>{title}</h1><img src="{image}" alt=""><p>{text}</p></body></html>')


class FakeDbBackend(RekognitionBackend):
    """Backend V3 with the fake database as its one engine. Search results, photo downloads and page
    fetches all come from the database or from its rows; nothing is fetched from the internet."""

    name = "fakedb"
    search_endpoint = "/api/images"
    fetch_endpoint = "fake-page"
    supported_engines = (FAKEDB,)

    def __init__(self, *, db_url: str, capture_dir: Path, rekognition, min_similarity: float,
                 client: httpx.AsyncClient | None = None, compare_timeout: float = COMPARE_TIMEOUT_S):
        self.db_url = db_url.rstrip("/")
        # One client for the database's API and its photos. It can reach a private address (a tailnet
        # IP), which the live backends' public-only clients refuse, so it only gets URLs under db_url.
        client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, trust_env=False, follow_redirects=False)
        super().__init__(api_key=None, capture_dir=capture_dir, host=None, rekognition=rekognition,
                         engines=(FAKEDB,), min_similarity=min_similarity, images=client,
                         compare_timeout=compare_timeout)
        self._rows: list[Row] | None = None
        self._rows_lock = asyncio.Lock()
        self._pages: dict[str, tuple[Row, str]] = {}  # normalized result URL -> (row, site)

    def image_url(self, file_name: str) -> str:
        return f"{self.db_url}/img/{quote(file_name)}"

    # Search

    async def _search_engine(self, image: CaptureImage, engine: str, face: Face) -> dict:
        """The uploaded photos as one result per (row, site), in the sanitized engine shape merge() reads."""
        rows = await self._uploads()
        results = []
        for row in rows:
            photo = self.image_url(row.file_name)
            for site in row.sites:
                url = source_url(row, site)
                key = normalize_url(url)
                if key is None:
                    continue
                self._pages[key] = (row, site)
                results.append({"link": url, "title": f"{row.name} on {site}" if row.name else f"Photo on {site}",
                                "source": site, "snippet": f"Photo found on {site}.", "thumbnail": photo,
                                "images": [photo]})
        trace.event("fakedb_search", image=image.image_id, uploads=len(rows), results=len(results))
        return {"search_metadata": {"status": "Success"}, "results": results}

    async def _uploads(self) -> list[Row]:
        """The database's uploaded photos, read once per run: the first and last page of its list
        without the CelebA files."""
        async with self._rows_lock:
            if self._rows is None:
                first = await self._get_page(0)
                total = first.get("all_total", first.get("total"))
                if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    raise BackendError("invalid_response")
                rows = parse_rows(first)
                if total > PAGE_SIZE:
                    rows += parse_rows(await self._get_page(max(PAGE_SIZE, total - PAGE_SIZE)))
                uploads: dict[str, Row] = {}
                for row in rows:
                    if not CELEBA_FILE.fullmatch(row.file_name):
                        uploads.setdefault(row.file_name, row)
                self._rows = list(uploads.values())
                trace.event("fakedb_rows", total=total, uploads=len(self._rows))
            return self._rows

    async def _get_page(self, offset: int) -> dict:
        try:
            response = await self.images.get(f"{self.db_url}/api/images", params={"offset": offset, "limit": PAGE_SIZE})
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError:
            raise BackendError("connection_error") from None
        if response.status_code != 200:
            raise status_error(response.status_code, response.headers)
        try:
            payload = response.json()
        except ValueError:
            raise BackendError("invalid_response") from None
        if not isinstance(payload, dict):
            raise BackendError("invalid_response")
        return payload

    # Photos and pages

    async def _download(self, url: str) -> bytes:
        """GET one of the database's photos, at most MAX_IMAGE_BYTES; any other URL is refused."""
        if not url.startswith(f"{self.db_url}/img/"):
            raise BackendError("blocked_destination")
        try:
            async with self.images.stream("GET", url) as response:
                if response.status_code != 200:
                    raise status_error(response.status_code, response.headers)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_IMAGE_BYTES:
                        raise BackendError("image_too_large")
                return bytes(body)
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError:
            raise BackendError("connection_error") from None

    async def fetch(self, url: str) -> FetchedPage:
        """The made-up page for one of this run's result URLs; any other URL is a 404."""
        key = normalize_url(url)
        entry = self._pages.get(key) if key else None
        if entry is None:
            raise BackendError("not_found", status_code=404)
        row, site = entry
        return FetchedPage(requested_url=url, final_url=key, fetched_url=key, status_code=200,
                           content_type="text/html; charset=utf-8",
                           html=page_html(row, site, self.image_url(row.file_name)))


class FakeDbThreats:
    """Dev mode's threat check, server.ThreatCheck: FRAUD_SITE is high, every other site low. Makes no
    request."""

    def check(self, findings: Findings) -> None:
        for candidate in findings.candidates:
            candidate["threat"] = fake_threat(candidate.get("sourceUrl", ""))
        trace.event("fakedb_threats", sources=len(findings.candidates))


def fake_threat(url: str) -> dict:
    try:
        site = (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        site = ""
    threat = FRAUD_THREAT if site == FRAUD_SITE else BENIGN_THREAT
    return {**threat, "indicators": list(threat["indicators"])}


def check_database(db_url: str) -> int:
    """How many photos the database lists, from one free GET; raises ConfigError if it doesn't answer."""
    try:
        response = httpx.get(f"{db_url}/api/images", params={"offset": 0, "limit": 1}, timeout=CHECK_TIMEOUT_S,
                             trust_env=False, follow_redirects=False)
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        payload = None
    total = payload.get("all_total") if isinstance(payload, dict) else None
    if not isinstance(total, int) or isinstance(total, bool):
        raise ConfigError(f"the fake image database at {db_url} isn't answering. Check that this machine is on the "
                          "team tailnet and that Zach's server is running, or set FAKE_DB_URL")
    return total


def build_fakedb_backend(settings: Settings, capture: CaptureArtifact) -> FakeDbBackend:
    """Dev mode's backend for a run: FAKE_DB_URL, AWS_REGION and AWS credentials."""
    if not settings.fake_db_url:
        raise ConfigError("dev mode needs FAKE_DB_URL; load settings with the fakedb backend")
    require_rekognition(settings)
    return FakeDbBackend(db_url=settings.fake_db_url, capture_dir=Path(capture.capture_dir),
                         rekognition=rekognition_client(settings.aws_region),
                         min_similarity=settings.rekognition_min_similarity)
