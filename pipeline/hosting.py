"""Short-lived image hosting for SerpApi's Yandex engine (spec 1.2; live only).

Yandex needs a public image URL and SerpApi documents no upload parameter.
IMAGE_HOST picks how each image gets one:

- tunnel (default): a loopback-only HTTP server on this machine, reached through a
  localhost.run SSH tunnel (`ssh -R`, no account or key needed). The tunnel opens
  once per stage-1 run, before any image's deadline starts. Each image gets a random
  path that's served only until its search is over, and the tunnel closes when the
  run's searches end. Nothing is stored off this machine, so uploads.json stays
  empty and `purge` has nothing remote to delete; localhost.run relays the bytes.
  The free tunnel slows to about 25 KB/s after a short burst, so it serves a copy at
  most 800 px on its longest side, re-encoded as JPEG without EXIF (no GPS or camera
  data leaves the machine).
  Yandex refuses Cloudflare quick-tunnel URLs (*.trycloudflare.com) without fetching
  them, but fetched an lhr.life URL (checked 2026-10-10).
- s3: a private S3 bucket under a random key, shared through a presigned GET URL
  valid for 10 minutes. The key is recorded in uploads.json before the upload, so
  `purge` can retry cleanup after a crash. The object is deleted once the search
  and its retries are over. Use a dedicated bucket or prefix with public listing
  off and a lifecycle rule that expires leftovers; signed-URL expiry doesn't
  delete anything. boto3 is imported lazily.

Hosted URLs are never logged or stored.
"""

from __future__ import annotations

import atexit
import io
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.artifacts import read_uploads, write_uploads
from pipeline.config import ConfigError
from pipeline.contracts import UploadRecord, UploadsManifest

PRESIGN_SECONDS = 600
KEY_PREFIX = "hackmidwest-faces/"
IMAGE_TYPES = {b"\xff\xd8\xff": ("image/jpeg", ".jpg"), b"\x89PNG\r\n\x1a\n": ("image/png", ".png")}
# The first end-to-end probe through a new tunnel took about 13 s (2026-10-10).
TUNNEL_START_SECONDS = 20.0
TUNNEL_PROBE_SECONDS = 25.0
TUNNEL_SERVER = "nokey@localhost.run"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
TUNNEL_LABEL = "localhost-run-tunnel"  # UploadRecord.bucket for tunnel-hosted images; never written to uploads.json
# A 270 KB photo took 10-13 s through the tunnel and Yandex gave up on it; an 800 px copy is about 60 KB.
SEARCH_IMAGE_MAX_SIDE = 800
SEARCH_IMAGE_QUALITY = 85


class HostingError(Exception):
    """Upload or presign failed. The message is a code, never a URL or key."""


def s3_client(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("s3", region_name=region, config=Config(
        signature_version="s3v4", connect_timeout=10, read_timeout=20,
        retries={"max_attempts": 2, "mode": "standard"}))


def image_type(data: bytes) -> tuple[str, str]:
    """(content type, file suffix) from the file's magic bytes; capture.py already allows only JPEG and PNG."""
    for magic, kind in IMAGE_TYPES.items():
        if data.startswith(magic):
            return kind
    raise HostingError("unsupported_image")


class S3ImageHost:
    """Uploads images to a private bucket and tracks every object in the run's uploads.json."""

    def __init__(self, *, bucket: str, region: str, run_dir: Path, client=None, prefix: str = KEY_PREFIX,
                 expires: int = PRESIGN_SECONDS):
        self.bucket, self.run_dir, self.prefix, self.expires = bucket, Path(run_dir), prefix, expires
        self.client = client if client is not None else s3_client(region)
        self._lock = threading.Lock()  # uploads.json is read-modify-write; images run in parallel

    def upload(self, data: bytes) -> tuple[str, UploadRecord]:
        """Upload one image; returns (presigned GET URL, record). Blocking: call from a worker thread."""
        content_type, suffix = image_type(data)
        record = UploadRecord(bucket=self.bucket, key=f"{self.prefix}{secrets.token_hex(16)}{suffix}")
        self._save(record)  # before the upload, so a crash mid-upload is still cleaned up
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=record.key, Body=data, ContentType=content_type)
            if response.get("VersionId"):
                record.version_id = response["VersionId"]
                self._save(record)
            url = self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": record.key}, ExpiresIn=self.expires)
        except Exception:
            raise HostingError("hosting_error") from None
        return url, record

    def delete(self, record: UploadRecord) -> None:
        """Delete one uploaded object (and its version, if versioned) and mark it deleted."""
        delete_object(record, client=self.client)
        record.deleted = True
        self._save(record)

    def _save(self, record: UploadRecord) -> None:
        with self._lock:
            manifest = read_uploads(self.run_dir)
            objects = [item for item in manifest.objects if (item.bucket, item.key) != (record.bucket, record.key)]
            write_uploads(self.run_dir, UploadsManifest(run_id=manifest.run_id, objects=objects + [record]))


def delete_object(record: UploadRecord, *, client) -> None:
    params = {"Bucket": record.bucket, "Key": record.key}
    if record.version_id:
        params["VersionId"] = record.version_id
    client.delete_object(**params)


def remote_deleter(region: str | None):
    """A purge callback that deletes one UploadRecord, or None when no region is configured.
    The S3 client is created on first use, so a run with nothing uploaded never loads boto3."""
    if not region:
        return None
    clients = []

    def delete(record: UploadRecord) -> None:
        if not clients:
            clients.append(s3_client(region))
        delete_object(record, client=clients[0])

    return delete


class TunnelImageHost:
    """Serves images from 127.0.0.1 through a localhost.run SSH tunnel, one random path per image.

    start() opens the tunnel and checks it end to end; upload() starts it if needed.
    If it can't start, later uploads fail at once instead of waiting again.
    `probe(url, timeout)` returns the body at a public URL or None; tests replace it.
    """

    def __init__(self, *, ssh: str, probe=None, start_timeout: float = TUNNEL_START_SECONDS,
                 probe_timeout: float = TUNNEL_PROBE_SECONDS):
        self.ssh, self.start_timeout, self.probe_timeout = ssh, start_timeout, probe_timeout
        self.probe = probe or _probe
        self._files: dict[str, tuple[str, bytes]] = {}  # "/<token>.jpg" -> (content type, bytes)
        self._files_lock = threading.Lock()
        self._start_lock = threading.RLock()
        self._base: str | None = None
        self._failed = False
        self._server: ThreadingHTTPServer | None = None
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        """Open the tunnel now (blocking, up to about 45 s); raises HostingError if it can't."""
        self._started()

    def upload(self, data: bytes) -> tuple[str, UploadRecord]:
        """Serve a small copy of one image; returns (public URL, record). Blocking: call from a worker thread."""
        image_type(data)
        copy = search_copy(data)
        base = self._started()
        record = UploadRecord(bucket=TUNNEL_LABEL, key=f"{secrets.token_hex(16)}.jpg")
        with self._files_lock:
            self._files[f"/{record.key}"] = ("image/jpeg", copy)
        return f"{base}/{record.key}", record

    def delete(self, record: UploadRecord) -> None:
        with self._files_lock:
            self._files.pop(f"/{record.key}", None)
        record.deleted = True

    def close(self) -> None:
        """Stop serving, then stop the tunnel and the local server. Safe to call twice."""
        with self._files_lock:
            self._files.clear()
        with self._start_lock:  # never while another thread is still starting the tunnel
            process, server, reader = self._process, self._server, self._reader
            self._process = self._server = self._reader = None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if reader is not None:
                reader.join(timeout=2)  # it ends at EOF, once ssh has exited
            process.stdout.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        atexit.unregister(self.close)

    def _started(self) -> str:
        with self._start_lock:
            if self._base is None:
                if self._failed:
                    raise HostingError("tunnel_error")
                try:
                    self._base = self._start()
                except BaseException:
                    self._failed = True
                    self.close()
                    raise
            return self._base

    def _start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self._files, self._files_lock))
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="image-host", daemon=True).start()
        port = self._server.server_address[1]
        atexit.register(self.close)  # don't leave the tunnel open if the run dies
        # BatchMode: never prompt. No key is offered, so the tunnel isn't tied to this machine's keys.
        # accept-new pins localhost.run's host key on first use, which protects the plain-HTTP leg.
        options = ["BatchMode=yes", "PubkeyAuthentication=no", "StrictHostKeyChecking=accept-new",
                   "ConnectTimeout=10", "ServerAliveInterval=30", "ExitOnForwardFailure=yes"]
        try:
            self._process = subprocess.Popen(
                [self.ssh, "-T", *(arg for option in options for arg in ("-o", option)),
                 "-R", f"80:127.0.0.1:{port}", TUNNEL_SERVER],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace")
        except OSError:
            raise HostingError("tunnel_error") from None

        # localhost.run prints a banner, then "<name>.lhr.life tunneled with tls termination, https://<name>.lhr.life".
        found: dict[str, str] = {}
        ready = threading.Event()

        def read_log(stream) -> None:
            try:
                for line in stream:  # keep draining so ssh never blocks on a full pipe
                    match = TUNNEL_URL.search(line)
                    if match and "url" not in found:
                        found["url"] = match.group(0)
                        ready.set()
            except (OSError, ValueError):
                pass  # the pipe was closed under us
            ready.set()  # ssh exited

        self._reader = threading.Thread(target=read_log, args=(self._process.stdout,), name="tunnel-log", daemon=True)
        self._reader.start()
        if not ready.wait(self.start_timeout) or "url" not in found or self._process.poll() is not None:
            raise HostingError("tunnel_error")

        # The tunnel's DNS name can take a few seconds to resolve; check a throwaway file end to end.
        token, body = f"/{secrets.token_hex(16)}.txt", secrets.token_bytes(16)
        with self._files_lock:
            self._files[token] = ("application/octet-stream", body)
        try:
            deadline = time.monotonic() + self.probe_timeout
            while self.probe(found["url"] + token, 5.0) != body:
                if time.monotonic() >= deadline or self._process.poll() is not None:
                    raise HostingError("tunnel_unreachable")
                time.sleep(1.0)
        finally:
            with self._files_lock:
                self._files.pop(token, None)
        return found["url"]


def search_copy(data: bytes) -> bytes:
    """A JPEG at most SEARCH_IMAGE_MAX_SIDE px on its longest side, upright, with no EXIF or other metadata."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)  # apply the orientation tag before it's dropped
            image.thumbnail((SEARCH_IMAGE_MAX_SIDE, SEARCH_IMAGE_MAX_SIDE))
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=SEARCH_IMAGE_QUALITY)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HostingError("unsupported_image") from None
    return output.getvalue()


def _handler(files: dict[str, tuple[str, bytes]], lock: threading.Lock):
    class ImageHandler(BaseHTTPRequestHandler):
        server_version, sys_version = "image-host", ""

        def do_GET(self) -> None:
            self._send(head=False)

        def do_HEAD(self) -> None:
            self._send(head=True)

        def _send(self, *, head: bool) -> None:
            with lock:
                hosted = files.get(urlsplit(self.path).path)
            if hosted is None:
                self.send_error(404)
                return
            content_type, data = hosted
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def log_message(self, format, *args) -> None:
            pass  # request paths are hosted URLs; never log them

    return ImageHandler


def _probe(url: str, timeout: float) -> bytes | None:
    import httpx

    try:
        response = httpx.get(url, timeout=timeout, trust_env=False, headers={"Cache-Control": "no-cache"})
    except httpx.HTTPError:
        return None
    return response.content if response.status_code == 200 else None


def find_ssh() -> str | None:
    return shutil.which("ssh")


def require_hosting(settings) -> None:
    """Fail before any upload when image hosting isn't configured."""
    if settings.image_host == "s3":
        if not settings.image_bucket or not settings.aws_region:
            raise ConfigError("IMAGE_HOST=s3 hosts each image briefly in S3 so Yandex can fetch it; "
                              "set IMAGE_BUCKET and AWS_REGION (and AWS credentials) in .env, "
                              "use IMAGE_HOST=tunnel, or pass --mock")
    elif find_ssh() is None:
        raise ConfigError("IMAGE_HOST=tunnel serves each image briefly through a localhost.run SSH tunnel so "
                          "Yandex can fetch it, and needs the ssh program (sudo apt install openssh-client); "
                          "or set IMAGE_HOST=s3, or pass --mock")


def host_for(settings, run_dir: Path) -> S3ImageHost | TunnelImageHost:
    require_hosting(settings)
    if settings.image_host == "s3":
        return S3ImageHost(bucket=settings.image_bucket, region=settings.aws_region, run_dir=run_dir)
    return TunnelImageHost(ssh=find_ssh())
