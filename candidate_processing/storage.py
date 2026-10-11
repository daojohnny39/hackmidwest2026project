"""Private local image-byte storage; format/decoder validation belongs to Section 3.

Bytes and metadata share a SQLite transaction so interrupted writes cannot leave
orphan image files. Scope must come from trusted backend authentication/session
configuration, never from a subject name or a candidate payload.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Callable, Iterator
from uuid import uuid4

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ASSET_TTL_SECONDS = 24 * 60 * 60
MAX_STORE_BYTES = 512 * 1024 * 1024
_ASSET_ID = re.compile(r"[0-9a-f]{32}")
_MIME_TYPES = {"application/octet-stream", "image/jpeg", "image/png"}


@dataclass(frozen=True)
class Asset:
    id: str
    size: int
    sha256: str
    mime_type: str
    expires_at: float


class AssetNotFound(Exception):
    """An asset is missing, expired, or outside the caller's scope."""


class StorageFull(Exception):
    """Adding this asset would exceed the configured stored-byte quota."""


class AssetIntegrityError(ValueError):
    """Stored bytes no longer match their recorded length or SHA-256."""


class LocalAssetStore:
    """Store immutable originals and separately created crops by opaque ID.

    The quota covers asset payload bytes, excluding SQLite metadata/journals.
    SQLite reuses space after deletion; the database need not shrink. An idle
    service must schedule cleanup() if it needs physical expiry on a timetable.
    Expired assets are always inaccessible, even before cleanup runs.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        ttl: float = ASSET_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        max_bytes: int = MAX_STORE_BYTES,
    ):
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Asset lifetime must be a finite positive number")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Storage quota must be a positive integer")
        configured_root = Path(root if root is not None else os.environ.get("FACETRACE_DATA_DIR", ".facetrace-data"))
        if configured_root.is_symlink():
            raise ValueError("Use a dedicated storage directory, not a symbolic link")
        self.root = configured_root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.ttl, self.clock, self.max_bytes = ttl, clock, max_bytes
        self.database = self.root / "assets.sqlite3"

        # Create with private permissions from the outset. The configured root
        # is trusted and must not be writable by other users.
        if self.database.is_symlink():
            raise ValueError("The asset database cannot be a symbolic link")
        descriptor = os.open(self.database, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("The asset database must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self._transaction() as db:
            db.execute(f"""
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size BETWEEN 1 AND {MAX_IMAGE_BYTES}),
                    sha256 TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    expires REAL NOT NULL,
                    data BLOB NOT NULL CHECK(length(data) = size)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS assets_expiry ON assets(expires)")
        self.cleanup()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        try:
            # Clear deleted payloads and serialize quota checks with writes.
            # No WAL sidecar holds an older copy after a completed deletion.
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self) -> float:
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("Storage clock must return a finite timestamp")
        return now

    @staticmethod
    def _validate_scope(scope: str):
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 128:
            raise ValueError("A trusted nonblank owner/session scope of at most 128 characters is required")

    @staticmethod
    def _validate_id(asset_id: str):
        if not isinstance(asset_id, str) or _ASSET_ID.fullmatch(asset_id) is None:
            raise AssetNotFound("Asset not found")

    def put(self, data: bytes, *, scope: str, mime_type: str = "application/octet-stream") -> Asset:
        """Retain byte-exact input; MIME is a hint, not proof of image validity."""
        self._validate_scope(scope)
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_IMAGE_BYTES:
            raise ValueError("Asset must be immutable bytes containing 1 byte through 10 MiB")
        if mime_type not in _MIME_TYPES:
            raise ValueError("MIME hint must be JPEG, PNG or application/octet-stream")
        now = self._now()
        expires_at = now + self.ttl
        if not math.isfinite(expires_at):
            raise ValueError("Asset expiry must be a finite timestamp")
        asset = Asset(uuid4().hex, len(data), hashlib.sha256(data).hexdigest(), mime_type, expires_at)
        full = False
        with self._transaction() as db:
            db.execute("DELETE FROM assets WHERE expires<=?", (now,))
            used = db.execute("SELECT COALESCE(SUM(size), 0) FROM assets").fetchone()[0]
            if used + asset.size > self.max_bytes:
                full = True
            else:
                db.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    asset.id, scope, asset.size, asset.sha256, asset.mime_type, asset.expires_at, data,
                ))
        # Let expiry cleanup commit even when the incoming asset cannot fit.
        if full:
            raise StorageFull("Asset storage quota reached")
        return asset

    def get(self, asset_id: str, *, scope: str) -> tuple[Asset, bytes]:
        self._validate_scope(scope)
        self._validate_id(asset_id)
        with self._transaction() as db:
            row = db.execute("""
                SELECT id, size, sha256, mime, expires, data FROM assets
                WHERE id=? AND scope=? AND expires>?
            """, (asset_id, scope, self._now())).fetchone()
        if row is None:
            raise AssetNotFound("Asset not found")
        asset, data = Asset(*row[:5]), row[5]
        if not isinstance(data, bytes) or len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
            raise AssetIntegrityError("Stored asset failed integrity validation")
        return asset, data

    def delete(self, asset_id: str, *, scope: str) -> None:
        """Delete an owned asset, including an expired asset awaiting cleanup."""
        self._validate_scope(scope)
        self._validate_id(asset_id)
        with self._transaction() as db:
            deleted = db.execute("DELETE FROM assets WHERE id=? AND scope=?", (asset_id, scope)).rowcount
        if not deleted:
            raise AssetNotFound("Asset not found")

    def cleanup(self) -> int:
        """Remove expired bytes and metadata; return the number removed."""
        with self._transaction() as db:
            return db.execute("DELETE FROM assets WHERE expires<=?", (self._now(),)).rowcount
