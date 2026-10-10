"""Search backend interface, reverse-image result parsing, and bounded retries (spec 1.1, 1.7).

A backend makes exactly one attempt per call and raises BackendError on failure;
with_retries() is the only retry layer, so retries never multiply. Stage code
never checks which backend it holds.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol, TypeVar

from pipeline.contracts import CaptureImage, FetchedPage, ImageHit
from pipeline.platforms import normalize_url

T = TypeVar("T")

MAX_RETRIES = 2  # spec 1.7: at most two retries per request
BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 4.0
MAX_HITS = 50
TITLE_LIMIT = 300
SNIPPET_LIMIT = 500
RETRYABLE = frozenset({"rate_limited", "server_error"})
# SerpApi reports an empty result set as an "error" alongside a successful status.
NO_RESULTS = re.compile(r"hasn'?t returned any results", re.IGNORECASE)


class BackendError(Exception):
    """One failed backend call.

    reason is a sanitized code from spec 1.7 (not_found, forbidden, rate_limited,
    server_error, timeout, login_wall, invalid_request, unauthorized,
    invalid_response, provider_error, hosting_error, blocked_destination, ...).
    The message is the reason only, so it can never carry a key or signed URL.
    """

    def __init__(self, reason: str, *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code if status_code is not None and 100 <= status_code <= 599 else None
        self.retry_after = retry_after
        self.retries = 0

    @property
    def retryable(self) -> bool:
        return self.reason in RETRYABLE


class SearchBackend(Protocol):
    name: str  # "mock" or "serpapi"
    origin: str | None  # mock server origin for normalize_url; None when live
    search_endpoint: str  # endpoint paths for run_log.jsonl; never a page URL
    fetch_endpoint: str

    async def reverse_image(self, image: CaptureImage) -> dict:
        """One reverse-image search attempt; returns the provider's JSON."""

    async def fetch(self, url: str) -> FetchedPage:
        """One fetch attempt of a normalized page URL; non-2xx responses raise BackendError."""

    async def release(self, image: CaptureImage) -> None:
        """Delete any hosted copy of the image; called once its search and retries are over."""

    async def aclose(self) -> None:
        """Release clients and clean up anything still hosted."""


@dataclass
class SearchResult:
    request_id: str | None
    hits: list[ImageHit]
    extras: dict = field(default_factory=dict)  # allowlisted metadata only


def status_error(status: int, headers: Mapping[str, str] | None = None, *, now: datetime | None = None) -> BackendError:
    """Map an HTTP error status to a BackendError, carrying Retry-After when given."""
    if status == 429:
        reason = "rate_limited"
    elif status >= 500:
        reason = "server_error"
    else:
        reason = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                  410: "not_found", 451: "forbidden"}.get(status, "http_error")
    retry_after = None
    for name, value in (headers or {}).items():
        if name.lower() == "retry-after":
            retry_after = parse_retry_after(value, now=now)
    return BackendError(reason, status_code=status, retry_after=retry_after)


def parse_retry_after(value: str, *, now: datetime | None = None) -> float | None:
    """Retry-After as seconds from now; accepts delta-seconds or an HTTP date."""
    value = str(value).strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


async def with_retries(
    call: Callable[[], Awaitable[T]],
    *,
    deadline: float,
    max_retries: int = MAX_RETRIES,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> tuple[T, int]:
    """Run call, retrying 429 and 5xx up to max_retries times within the deadline.

    Honors Retry-After; without it, backs off 0.5 s, 1 s, ... capped at 4 s. If the
    wait would pass the deadline, stops and re-raises. Returns (result, retries);
    a final BackendError carries .retries.
    """
    retries = 0
    while True:
        try:
            return await call(), retries
        except BackendError as error:
            error.retries = retries
            if not error.retryable or retries >= max_retries:
                raise
            wait = error.retry_after if error.retry_after is not None else min(BACKOFF_BASE_S * 2 ** retries, BACKOFF_MAX_S)
            if clock() + wait >= deadline:
                raise
            await sleep(wait)
            retries += 1


def parse_image_results(payload: object, *, mock_origin: str | None = None) -> SearchResult:
    """Normalize a Yandex-shaped reverse-image response (SerpApi or the mock) into ImageHits.

    A provider error or a non-success status is a failure even on HTTP 200, except
    SerpApi's "hasn't returned any results", which is zero hits. Missing
    image_results is zero hits. similar_images are never treated as matching pages.
    """
    if not isinstance(payload, dict):
        raise BackendError("invalid_response")
    metadata = payload.get("search_metadata") if isinstance(payload.get("search_metadata"), dict) else {}
    status = metadata.get("status")
    error = payload.get("error")
    if error and not (status in (None, "Success") and NO_RESULTS.search(str(error))):
        raise BackendError("provider_error")
    if status not in (None, "Success"):
        raise BackendError("provider_error")
    rows = [] if error else payload.get("image_results", [])
    if not isinstance(rows, list):
        raise BackendError("invalid_response")
    rows = [row for row in rows if isinstance(row, dict)][:MAX_HITS]

    positions = [row.get("position") for row in rows]
    use_provider = all(isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in positions) \
        and len(set(positions)) == len(positions)
    hits, skipped = [], 0
    for index, row in enumerate(rows, 1):
        link = row.get("link") if isinstance(row.get("link"), str) else None
        original = row.get("mock_original_url") if mock_origin is not None else None
        url = normalize_url(original if isinstance(original, str) else link, mock_origin=mock_origin)
        if url is None:
            skipped += 1
            continue
        hits.append(ImageHit(
            position=positions[index - 1] if use_provider else index,
            title=_text(row.get("title"), TITLE_LIMIT),
            url=url,
            fetch_url=link or url,
            source=_text(row.get("source"), TITLE_LIMIT),
            snippet=_text(row.get("snippet"), SNIPPET_LIMIT),
            thumbnail_url=_thumbnail(row.get("thumbnail")),
        ))
    hits.sort(key=lambda hit: hit.position)

    extras: dict = {"status": status or "Success"}
    info = payload.get("search_information")
    if isinstance(info, dict) and isinstance(info.get("total_results"), int):
        extras["total_results"] = info["total_results"]
    if skipped:
        extras["skipped_hits"] = skipped
    request_id = metadata.get("id")
    return SearchResult(request_id=request_id if isinstance(request_id, str) else None, hits=hits, extras=extras)


def _text(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _thumbnail(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("link")
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return None
    # Never keep anything that looks like a signed URL (our own hosted image).
    return None if "x-amz-" in value.lower() or len(value) > 2048 else value
