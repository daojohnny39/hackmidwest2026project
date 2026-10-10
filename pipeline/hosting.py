"""Short-lived image hosting for SerpApi's Yandex engine (spec 1.2; live only; Proposal).

Yandex needs a public image URL and SerpApi documents no upload parameter. Each
image goes to a private S3 bucket under a random key and is shared through a
presigned GET URL valid for 10 minutes. The key is recorded in uploads.json
before the upload, so `purge` can retry cleanup after a crash. The object is
deleted once the search and its retries are over. Presigned URLs are never
logged or stored.

Unverified integration: whether Yandex can fetch this bucket's presigned URLs.
Check it early with smoke-search. Use a dedicated bucket or prefix with public
listing off and a lifecycle rule that expires leftovers; signed-URL expiry
doesn't delete anything. boto3 is imported lazily.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path

from pipeline.artifacts import read_uploads, write_uploads
from pipeline.config import ConfigError
from pipeline.contracts import UploadRecord, UploadsManifest

PRESIGN_SECONDS = 600
KEY_PREFIX = "hackmidwest-faces/"
IMAGE_TYPES = {b"\xff\xd8\xff": ("image/jpeg", ".jpg"), b"\x89PNG\r\n\x1a\n": ("image/png", ".png")}


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


def require_hosting(settings) -> None:
    """Fail before any upload when image hosting isn't configured."""
    if not settings.image_bucket or not settings.aws_region:
        raise ConfigError("SerpApi search hosts each image briefly in S3 so Yandex can fetch it; "
                          "set IMAGE_BUCKET and AWS_REGION (and AWS credentials) in .env, or pass --mock")


def host_for(settings, run_dir: Path) -> S3ImageHost:
    require_hosting(settings)
    return S3ImageHost(bucket=settings.image_bucket, region=settings.aws_region, run_dir=run_dir)
