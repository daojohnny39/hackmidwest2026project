"""SerpApi Yandex reverse-image search and bounded live page fetches (spec 1.1, 1.2, 1.7).

Search: GET https://serpapi.com/search.json with engine=yandex_images, the hosted
image url, tab=about and api_key. The image is hosted once per image, reused
across retries, and deleted by release() when its search is over. Successful
responses are sanitized (no echoed image URL, key, or endpoint links) and cached
for this run only under runs/<run_id>/cache/serpapi/, keyed by image hash, engine
and tab. --refresh skips reading that cache.

Fetch: public http(s) destinations only. The URL is checked at every redirect
hop (at most three), and the connection itself dials only addresses that passed
the check, so DNS rebinding can't reach a private network. No cookies, no proxy
or netrc from the environment, an identifiable User-Agent, 10 s per fetch, HTML
capped at 1 MB. Login walls and non-HTML responses are errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import httpcore
import httpx

from pipeline import trace
from pipeline.config import ConfigError, Settings
from pipeline.contracts import CaptureArtifact, CaptureImage, FetchedPage, UploadRecord
from pipeline.platforms import normalize_url
from pipeline.search.base import BackendError, status_error

SERPAPI_URL = "https://serpapi.com/search.json"
ENGINE, TAB = "yandex_images", "about"
# SerpApi searches can take several seconds, and a timed-out search may still use a credit.
SEARCH_TIMEOUT_S = 20.0
FETCH_TIMEOUT_S = 10.0
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 1024 * 1024
HTML_TYPES = ("text/html", "application/xhtml+xml")
USER_AGENT = ("HackMidwest2026-ConsentedProfileDemo/0.1 "
              "(+https://github.com/daojohnny39/hackmidwest2026project)")
LOGIN_PATHS = ("/login", "/accounts/login", "/authwall", "/checkpoint", "/signin", "/sign-in", "/i/flow/login",
               "/uas/login", "/session")

# Response fields kept in the cache and handed to the parser; everything else is dropped.
METADATA_FIELDS = ("id", "status", "total_time_taken")
RESULT_FIELDS = ("position", "title", "link", "source", "snippet", "thumbnail")


class BlockedDestination(httpcore.ConnectError):
    pass


def _public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """URL-level destination rule; the connection-level check below backs it up."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise BackendError("blocked_destination") from None
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise BackendError("blocked_destination")
    if port not in (None, 80, 443) or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise BackendError("blocked_destination")
    try:
        if not _public_ip(host):
            raise BackendError("blocked_destination")
    except ValueError:
        pass  # a host name; resolved and checked at connection time


class PublicOnlyNetwork(httpcore.AsyncNetworkBackend):
    """Resolves the host itself and connects only to public addresses (all of them must be public)."""

    def __init__(self, resolver=None):
        self._inner = httpcore.AnyIOBackend()
        self._resolve = resolver or self._getaddrinfo

    @staticmethod
    async def _getaddrinfo(host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(info[4][0] for info in infos))

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await self._resolve(host, port)
        except OSError as error:
            raise httpcore.ConnectError(f"cannot resolve host: {type(error).__name__}") from None
        if not addresses or not all(_public_ip(address) for address in addresses):
            raise BlockedDestination("destination is not a public address")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                                      socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        raise last_error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedDestination("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool dials through PublicOnlyNetwork. TLS still verifies the
    certificate against the URL's host name (httpcore uses the origin host for SNI)."""

    def __init__(self, resolver=None):
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False), max_connections=20,
            max_keepalive_connections=10, keepalive_expiry=5.0, network_backend=PublicOnlyNetwork(resolver))


def page_client(resolver=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PublicOnlyTransport(resolver), follow_redirects=False, trust_env=False,
        timeout=httpx.Timeout(FETCH_TIMEOUT_S), headers={"User-Agent": USER_AGENT, "Accept": ", ".join(HTML_TYPES)},
        cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])),  # accepts no cookies
    )


def sanitize(payload: dict) -> dict:
    """Keep only what the parser uses. Drops search_parameters (it echoes the image URL),
    endpoint links, similar_images, and anything else SerpApi adds."""
    clean: dict = {}
    metadata = payload.get("search_metadata")
    if isinstance(metadata, dict):
        clean["search_metadata"] = {name: metadata[name] for name in METADATA_FIELDS if name in metadata}
    info = payload.get("search_information")
    if isinstance(info, dict) and isinstance(info.get("total_results"), int):
        clean["search_information"] = {"total_results": info["total_results"]}
    if isinstance(payload.get("error"), str):
        clean["error"] = payload["error"][:300]
    rows = payload.get("image_results")
    if isinstance(rows, list):
        clean["image_results"] = [{name: row[name] for name in RESULT_FIELDS if name in row}
                                  for row in rows if isinstance(row, dict)]
    return clean


class SerpApiBackend:
    name = "serpapi"
    origin = None
    search_endpoint = "/search.json"
    fetch_endpoint = "page-fetch"  # live page URLs are evidence, so the log never names them

    def __init__(self, *, api_key: str, capture_dir: Path, host, cache_dir: Path | None = None, refresh: bool = False,
                 search_client: httpx.AsyncClient | None = None, pages: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.capture_dir = Path(capture_dir)
        self.host = host  # a hosting.py host, or a test double with upload(data) / delete(record) and optional close()
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.search_client = search_client or httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, trust_env=False,
                                                                headers={"User-Agent": USER_AGENT})
        self.pages = pages or page_client()
        self.cache_hits: set[str] = set()
        self._hosted: dict[str, tuple[str, UploadRecord]] = {}
        self._hosting_locks: dict[str, asyncio.Lock] = {}

    # Search

    def _image_bytes(self, image: CaptureImage) -> bytes:
        if not image.file:
            raise BackendError("invalid_request")
        # capture.py already checked the path stays inside the folder with no symlinks.
        return (self.capture_dir / image.file).read_bytes()

    def _cache_path(self, data: bytes) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{hashlib.sha256(data).hexdigest()}-{ENGINE}-{TAB}.json"

    async def reverse_image(self, image: CaptureImage) -> dict:
        data = await asyncio.to_thread(self._image_bytes, image)
        cache = self._cache_path(data)
        if cache is not None and not self.refresh and cache.is_file():
            try:
                self.cache_hits.add(image.image_id)
                payload = json.loads(cache.read_text(encoding="utf-8"))
                trace.event("search_cache_hit", image=image.image_id)
                return payload
            except (OSError, ValueError) as exc:
                self.cache_hits.discard(image.image_id)
                trace.error("search_cache_unreadable", exc, image=image.image_id)
        url = await self._hosted_url(image, data)
        params = {"engine": ENGINE, "url": url, "tab": TAB, "api_key": self.api_key}
        try:
            response = await self.search_client.get(SERPAPI_URL, params=params)
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError:  # never show the exception: its text can include the request URL and key
            raise BackendError("connection_error") from None
        if response.status_code != 200:
            trace.error("serpapi_http_error", image=image.image_id, status=response.status_code,
                        body=response.text[:2000])
            raise status_error(response.status_code, response.headers)
        try:
            payload = response.json()
        except ValueError:
            raise BackendError("invalid_response") from None
        if not isinstance(payload, dict):
            raise BackendError("invalid_response")
        clean = sanitize(payload)
        metadata = clean.get("search_metadata", {})
        if cache is not None and metadata.get("status") == "Success" and "error" not in clean:
            await asyncio.to_thread(_write_json, cache, clean)
        return clean

    async def _hosted_url(self, image: CaptureImage, data: bytes) -> str:
        lock = self._hosting_locks.setdefault(image.image_id, asyncio.Lock())
        async with lock:
            if image.image_id not in self._hosted:
                try:
                    self._hosted[image.image_id] = await asyncio.to_thread(self.host.upload, data)
                except BackendError:
                    raise
                except Exception:
                    raise BackendError("hosting_error") from None
            return self._hosted[image.image_id][0]

    async def release(self, image: CaptureImage) -> None:
        hosted = self._hosted.pop(image.image_id, None)
        if hosted is None:
            return
        try:
            await asyncio.to_thread(self.host.delete, hosted[1])
        except Exception as exc:
            # uploads.json still lists it as not deleted; `purge` retries the deletion
            trace.error("image_release_failed", exc, image=image.image_id)

    async def aclose(self) -> None:
        for image_id in list(self._hosted):
            hosted = self._hosted.pop(image_id)
            try:
                await asyncio.to_thread(self.host.delete, hosted[1])
            except Exception as exc:
                trace.error("image_release_failed", exc, image=image_id)
        close = getattr(self.host, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
        await self.search_client.aclose()
        await self.pages.aclose()

    # Fetch

    async def fetch(self, url: str) -> FetchedPage:
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=FETCH_TIMEOUT_S)
        except TimeoutError:
            raise BackendError("timeout") from None

    async def _fetch(self, url: str) -> FetchedPage:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            try:
                async with self.pages.stream("GET", current) as response:
                    if response.is_redirect and "location" in response.headers:
                        current = urldefrag(urljoin(current, response.headers["location"]))[0]
                        continue
                    final = str(response.url)
                    if _login_wall(final):
                        raise BackendError("login_wall", status_code=response.status_code)
                    if response.status_code >= 400:
                        raise status_error(response.status_code, response.headers)
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type not in HTML_TYPES:
                        raise BackendError("unsupported_content", status_code=response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_HTML_BYTES:
                            break  # bounded: keep the first 1 MB
                    html = bytes(body[:MAX_HTML_BYTES]).decode(response.encoding or "utf-8", errors="replace")
                    return FetchedPage(requested_url=url, final_url=normalize_url(final) or url,
                                       fetched_url=normalize_url(final) or url, status_code=response.status_code,
                                       content_type=content_type, html=html)
            except httpx.TimeoutException:
                raise BackendError("timeout") from None
            except httpx.HTTPError as error:
                # httpx re-raises connection failures as ConnectError, keeping ours as the cause.
                blocked = isinstance(error.__cause__, BlockedDestination)
                raise BackendError("blocked_destination" if blocked else "connection_error") from None
        raise BackendError("too_many_redirects")


def _login_wall(url: str) -> bool:
    path = urlsplit(url).path.lower().rstrip("/")
    return any(path == login or path.startswith(login + "/") for login in LOGIN_PATHS)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def build_serpapi_backend(settings: Settings, capture: CaptureArtifact, run_dir: Path, *, refresh: bool) -> SerpApiBackend:
    """The live backend for a run: needs SERPAPI_KEY and an image host (IMAGE_HOST, hosting.py).
    A tunnel host opens here, before any image's deadline starts; if it can't, no search is made."""
    from pipeline.hosting import HostingError, host_for

    if not settings.serpapi_key:
        raise ConfigError("SERPAPI_KEY is missing; save it with `python -m pipeline set-key serpapi`, or pass --mock")
    host = host_for(settings, run_dir)
    if hasattr(host, "start"):
        try:
            host.start()
        except HostingError as error:
            raise ConfigError(f"couldn't open the image tunnel ({error}); check the network and retry with "
                              "`stage 1 --run <run>`, set IMAGE_HOST=s3, or pass --mock") from None
    return SerpApiBackend(api_key=settings.serpapi_key, capture_dir=Path(capture.capture_dir), host=host,
                          cache_dir=Path(run_dir) / "cache" / "serpapi", refresh=refresh)
