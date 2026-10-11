"""Trace reverse face search (https://traceaifacescan.app), backend V3's face-index engine.

Yandex and Google Lens find images that look like the participant; Trace, like FaceCheck,
matches the face against faces it has indexed from public pages and social profiles, and it
takes card payments. One search per image (API v1, openapi.json checked 2026-10-10):

1. POST /api/v1/scans with the face image (multipart field "image": JPEG, PNG or WebP, at most
   8 MB, at least 200x200 px) returns 202 and the scan. The Idempotency-Key header makes a
   repeated upload within 24 hours return the same scan instead of paying again. Backend V3
   derives the key from the capture image's hash, so a rerun after a timeout picks up the
   scan it left running.
2. GET /scans/{id} is polled until status is done or failed, for at most SCAN_TIMEOUT_S. Trace
   says 25-45 s; the first live scan (2026-10-10) was still at 87% after 90 s. A scan started while the
   account has no credits comes back locked, with no matches; POST /scans/{id}/reveal spends a
   credit and runs it, or answers 402 insufficient_credits.
3. Each match has a 50-100 score, a tier (possible from 70, strong from 80, near_certain from 90),
   the platform, a handle for account URLs, the page URL, and a preview of the matched face at
   /scans/{id}/previews/{index}, which needs the API key. Backend V3 compares that preview with
   the participant's face in Rekognition.
4. DELETE /scans/{id} deletes the uploaded image and the previews once the scan has finished.
   A scan that is still running is left for a rerun to pick up; Trace deletes it after 30 days.

A search costs one credit (packs of 5 for $4, 40 for $22, 100 for $50). Errors are RFC 9457
problem documents whose code maps to a BackendError reason. The key goes only in the
Authorization header, and only to Trace's API: previews are fetched only from its scans path.
GET /account is free and is how `set-key trace` checks a key.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from pipeline import trace
from pipeline.search.base import BackendError, status_error
from pipeline.search.serpapi_backend import USER_AGENT

TRACE_API = "https://traceaifacescan.app/api/v1"
TRACE_HOST = "traceaifacescan.app"
REQUEST_TIMEOUT_S = 30.0
ACCOUNT_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 3.0
SCAN_TIMEOUT_S = 300.0  # Trace says 25-45 s, but one took over 90 s; stage 1 adds this to the image deadline
MIN_SIDE = 200
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
CREDIT_PACKS = "5 for $4, 40 for $22, 100 for $50"
# Problem-document codes -> BackendError reasons.
PROBLEM_REASONS = {"unauthenticated": "unauthorized", "insufficient_credits": "no_credits",
                   "invalid_image": "unsupported_image", "rate_limited": "rate_limited", "not_found": "not_found"}
# A failed scan's error_code -> BackendError reason. Trace returns the credit of a failed search.
FAILURE_REASONS = {"no_face": "no_face", "timeout": "timeout", "rate_limited": "rate_limited",
                   "provider_unavailable": "server_error", "network": "server_error", "upload_failed": "server_error",
                   "not_authorized": "provider_error", "unexpected": "provider_error"}


class KeyRejected(Exception):
    """Trace refused the API key."""


class CheckFailed(Exception):
    """Trace couldn't be reached or answered unexpectedly; the key may still be valid."""


def is_preview_url(url: object) -> bool:
    """Whether url is a preview on Trace's own API, the only place the key may be sent."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == TRACE_HOST and parts.port is None
            and parts.path.startswith("/api/v1/scans/") and "/previews/" in parts.path)


class TraceFaceSearch:
    """Async client for one stage-1 run. A retry of an image resumes its scan instead of paying twice."""

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None,
                 poll_interval: float = POLL_INTERVAL_S, scan_timeout: float = SCAN_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep):
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, trust_env=False,
                                                  follow_redirects=False, headers={"User-Agent": USER_AGENT})
        self.poll_interval, self.scan_timeout = poll_interval, scan_timeout
        self.clock, self.sleep = clock, sleep
        self._scans: dict[str, str] = {}  # image_id -> scan id
        self._keys: dict[str, str] = {}  # image_id -> Idempotency-Key, so a lost 202 can't start a second scan
        self._finished: set[str] = set()  # scan ids that reached done or failed, and may be deleted

    async def search(self, image_id: str, image: bytes, *, key: str | None = None) -> tuple[str, dict]:
        """Run or resume the image's scan; returns (scan id, the finished scan with its matches).
        key is the Idempotency-Key: the same key within 24 hours returns the same scan."""
        scan_id = self._scans.get(image_id)
        if scan_id is None:
            scan_id = await self._upload(image_id, image, self._keys.setdefault(image_id, key or secrets.token_hex(16)))
        else:
            trace.event("trace_resume", image=image_id, scan=scan_id)
        try:
            scan = await self._wait(image_id, scan_id)
        except BackendError as error:
            if error.reason != "not_found" or key is None or self._keys[image_id] != key:
                raise
            # The key's earlier scan was deleted after it finished; this search is a new, paid one.
            trace.event("trace_scan_gone", image=image_id, scan=scan_id)
            scan_id = await self._upload(image_id, image, self._new_key(image_id))
            scan = await self._wait(image_id, scan_id)
        if scan.get("locked"):
            # No credit was spent when the scan was made; reveal spends one, or answers 402.
            await self._call("POST", f"/scans/{scan_id}/reveal", image_id)
            trace.event("trace_reveal", image=image_id, scan=scan_id)
            scan = await self._wait(image_id, scan_id, unlocked=True)
        trace.event("trace_done", image=image_id, scan=scan_id, matches=len(scan.get("matches") or []))
        return scan_id, scan

    def _new_key(self, image_id: str) -> str:
        self._keys[image_id] = secrets.token_hex(16)
        return self._keys[image_id]

    async def _upload(self, image_id: str, image: bytes, key: str) -> str:
        reply = await self._call("POST", "/scans", image_id, headers={"Idempotency-Key": key},
                                 files={"image": ("face.jpg", image, "image/jpeg")})
        scan_id = reply.get("id")
        if not isinstance(scan_id, str) or not scan_id:
            raise BackendError("invalid_response")
        self._scans[image_id] = scan_id
        trace.event("trace_upload", image=image_id, scan=scan_id, bytes=len(image), status=reply.get("status"),
                    locked=reply.get("locked"))
        return scan_id

    async def _wait(self, image_id: str, scan_id: str, *, unlocked: bool = False) -> dict:
        started, last = self.clock(), None
        while True:
            scan = await self._call("GET", f"/scans/{scan_id}", image_id)
            status = scan.get("status")
            if status in ("done", "failed"):
                self._finished.add(scan_id)
            if status == "failed":
                code = str(scan.get("error_code") or "unexpected")
                trace.error("trace_scan_failed", image=image_id, scan=scan_id, code=code)
                raise BackendError(FAILURE_REASONS.get(code, "provider_error"))
            if status == "done" and not (unlocked and scan.get("locked")):
                if not isinstance(scan.get("matches") or [], list):
                    raise BackendError("invalid_response")
                return scan
            if (status, scan.get("progress")) != last:
                last = (status, scan.get("progress"))
                trace.event("trace_progress", image=image_id, scan=scan_id, status=status, progress=scan.get("progress"))
            if self.clock() - started >= self.scan_timeout:
                raise BackendError("timeout")
            await self.sleep(self.poll_interval)

    async def preview(self, url: str) -> bytes:
        """The preview image of one match. Refuses any URL outside Trace's scans path."""
        if not is_preview_url(url):
            raise BackendError("blocked_destination")
        response = await self._send("GET", url, headers={"Accept": "image/*"})
        if response.status_code != 200:
            raise status_error(response.status_code, response.headers)
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            raise BackendError("unsupported_content")
        if len(response.content) > MAX_PREVIEW_BYTES:
            raise BackendError("image_too_large")
        return response.content

    async def delete(self, image_id: str) -> None:
        """Delete the image's finished scan (its uploaded image and previews) at Trace. A scan still
        running is left for a rerun to pick up with the same Idempotency-Key. Never raises."""
        scan_id = self._scans.pop(image_id, None)
        if scan_id is None:
            return
        if scan_id not in self._finished:
            trace.event("trace_left_running", image=image_id, scan=scan_id)
            return
        try:
            await self._call("DELETE", f"/scans/{scan_id}", image_id)
            trace.event("trace_deleted", image=image_id, scan=scan_id)
        except BackendError as error:
            if error.reason != "not_found":
                trace.error("trace_delete_failed", image=image_id, scan=scan_id, reason=error.reason)

    async def aclose(self) -> None:
        for image_id in list(self._scans):
            await self.delete(image_id)
        await self.client.aclose()

    async def _call(self, method: str, path: str, image_id: str, **kwargs) -> dict:
        response = await self._send(method, TRACE_API + path, **kwargs)
        if not 200 <= response.status_code < 300:
            problem = _json(response)
            code = str(problem.get("code") or "") if isinstance(problem, dict) else ""
            trace.error("trace_http_error", image=image_id, method=method, path=path.split("/previews/")[0],
                        status=response.status_code, code=code[:40],
                        detail=str(problem.get("detail") or "")[:300] if isinstance(problem, dict) else None)
            error = status_error(response.status_code, response.headers)
            if code in PROBLEM_REASONS:
                error = BackendError(PROBLEM_REASONS[code], status_code=response.status_code,
                                     retry_after=error.retry_after)
            raise error
        if method == "DELETE":
            return {}
        reply = _json(response)
        if not isinstance(reply, dict):
            raise BackendError("invalid_response")
        return reply

    async def _send(self, method: str, url: str, *, headers: dict | None = None, **kwargs) -> httpx.Response:
        headers = {"Accept": "application/json", **(headers or {}), "Authorization": f"Bearer {self.api_key}"}
        try:
            return await self.client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError:  # never show the exception: it could carry the request
            raise BackendError("connection_error") from None


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def account_info(api_key: str) -> dict:
    """The account behind a key, from the free GET /account. Raises KeyRejected or CheckFailed."""
    try:
        response = httpx.get(TRACE_API + "/account", timeout=ACCOUNT_TIMEOUT_S, trust_env=False,
                             headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}",
                                      "User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise CheckFailed(type(exc).__name__) from None
    if response.status_code == 401:
        raise KeyRejected("unauthenticated")
    if response.status_code != 200:
        raise CheckFailed(f"HTTP {response.status_code}")
    data = _json(response)
    if not isinstance(data, dict):
        raise CheckFailed("unexpected response")
    return data


def describe_account(info: dict) -> str:
    """Only the credit counts from GET /account, wherever they sit; never the key or other details."""
    credits = {}
    for scope in (info, info.get("account"), info.get("data")):
        if isinstance(scope, dict):
            credits.update({name: value for name, value in scope.items()
                            if "credit" in name.lower() and isinstance(value, int) and not isinstance(value, bool)})
    if not credits:
        return "no credit balance in the reply"
    return ", ".join(f"{name.replace('_', ' ')}: {value}" for name, value in sorted(credits.items()))
