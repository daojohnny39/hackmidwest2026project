"""Exercise local asset lifecycle, access boundaries, quota and durability."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from candidate_processing.storage import (
    ASSET_TTL_SECONDS, MAX_IMAGE_BYTES, MAX_STORE_BYTES,
    AssetIntegrityError, AssetNotFound, LocalAssetStore, StorageFull,
)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "assets"
        self.now = 1_000.0
        self.store = LocalAssetStore(self.root, clock=lambda: self.now)

    def test_defaults_and_round_trip_preserve_exact_bytes_and_digest(self):
        data = b"\x89PNG\r\n\x1a\nsynthetic undecoded bytes\x00\xff"
        original = self.store.put(data, scope="session-a", mime_type="image/png")
        metadata, restored = self.store.get(original.id, scope="session-a")
        self.assertEqual(restored, data)
        self.assertEqual(metadata, original)
        self.assertRegex(original.id, r"^[0-9a-f]{32}$")
        self.assertEqual(original.size, len(data))
        self.assertEqual(original.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(original.mime_type, "image/png")
        self.assertEqual(original.expires_at, self.now + ASSET_TTL_SECONDS)
        self.assertEqual(self.store.max_bytes, MAX_STORE_BYTES)

    def test_reopening_store_retains_assets(self):
        asset = self.store.put(b"original", scope="session-a")
        reopened = LocalAssetStore(self.root, clock=lambda: self.now)
        self.assertEqual(reopened.get(asset.id, scope="session-a"), (asset, b"original"))

    def test_environment_selects_default_storage_directory(self):
        root = Path(self.directory.name) / "configured"
        with patch.dict(os.environ, {"FACETRACE_DATA_DIR": str(root)}):
            self.assertEqual(LocalAssetStore().root, root.resolve())

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem permissions")
    def test_private_directory_and_database_permissions(self):
        self.store.put(b"private", scope="session-a")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.database.stat().st_mode), 0o600)

    def test_wrong_scope_cannot_read_or_delete_and_does_not_disclose_existence(self):
        asset = self.store.put(b"private", scope="session-a")
        for operation in (self.store.get, self.store.delete):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(AssetNotFound) as denied:
                    operation(asset.id, scope="session-b")
                with self.assertRaises(AssetNotFound) as missing:
                    operation("0" * 32, scope="session-b")
                self.assertEqual(str(denied.exception), str(missing.exception))
        self.assertEqual(self.store.get(asset.id, scope="session-a")[1], b"private")

    def test_trusted_scope_is_required_for_each_operation(self):
        asset = self.store.put(b"private", scope="session-a")
        for scope in ("", " ", None, "x" * 129):
            for operation, value in ((self.store.put, b"x"), (self.store.get, asset.id), (self.store.delete, asset.id)):
                with self.subTest(scope=scope, operation=operation.__name__), self.assertRaises(ValueError):
                    operation(value, scope=scope)

    def test_path_and_url_inputs_cannot_resolve_as_assets(self):
        for asset_id in ("../assets.sqlite3", "/etc/passwd", "https://example.com/a.jpg", "A" * 32, None):
            for operation in (self.store.get, self.store.delete):
                with self.subTest(asset_id=asset_id), self.assertRaises(AssetNotFound):
                    operation(asset_id, scope="session-a")

    def test_empty_oversized_or_mutable_input_is_rejected(self):
        for data in (b"", b"x" * (MAX_IMAGE_BYTES + 1), bytearray(b"x"), "text"):
            with self.subTest(type=type(data).__name__, size=len(data)), self.assertRaises(ValueError):
                self.store.put(data, scope="session-a")

    def test_maximum_asset_size_is_accepted(self):
        data = b"x" * MAX_IMAGE_BYTES
        asset = self.store.put(data, scope="session-a")
        self.assertEqual(self.store.get(asset.id, scope="session-a")[1], data)

    def test_mime_is_only_a_hint_and_unsupported_hints_fail(self):
        asset = self.store.put(b"not a decoded image", scope="session-a")
        self.assertEqual(asset.mime_type, "application/octet-stream")
        with self.assertRaises(ValueError):
            self.store.put(b"content", scope="session-a", mime_type="text/html")

    def test_separate_crop_and_duplicate_uploads_never_overwrite_original(self):
        original = self.store.put(b"original", scope="session-a")
        repeated = self.store.put(b"original", scope="session-a")
        crop = self.store.put(b"crop", scope="session-a", mime_type="image/jpeg")
        self.assertEqual(len({original.id, repeated.id, crop.id}), 3)
        self.assertEqual(original.sha256, repeated.sha256)
        self.store.delete(crop.id, scope="session-a")
        self.assertEqual(self.store.get(original.id, scope="session-a")[1], b"original")

    def test_delete_removes_asset_and_releases_quota(self):
        store = LocalAssetStore(self.root, max_bytes=4, clock=lambda: self.now)
        asset = store.put(b"1234", scope="session-a")
        store.delete(asset.id, scope="session-a")
        with self.assertRaises(AssetNotFound):
            store.get(asset.id, scope="session-a")
        with self.assertRaises(AssetNotFound):
            store.delete(asset.id, scope="session-a")
        self.assertEqual(store.put(b"1234", scope="session-a").size, 4)

    def test_expiry_boundary_blocks_reads_and_cleanup_preserves_live_assets(self):
        expired = self.store.put(b"old", scope="session-a")
        self.now += 1
        live = self.store.put(b"new", scope="session-b")
        self.now = expired.expires_at - 0.001
        self.assertEqual(self.store.get(expired.id, scope="session-a")[1], b"old")
        self.now = expired.expires_at
        with self.assertRaises(AssetNotFound):
            self.store.get(expired.id, scope="session-a")
        self.assertEqual(self.store.cleanup(), 1)
        self.assertEqual(self.store.cleanup(), 0)
        self.assertEqual(self.store.get(live.id, scope="session-b")[1], b"new")

    def test_owner_can_delete_expired_asset_before_cleanup(self):
        asset = self.store.put(b"old", scope="session-a")
        self.now = asset.expires_at
        self.store.delete(asset.id, scope="session-a")
        self.assertEqual(self.store.cleanup(), 0)

    def test_put_and_startup_reclaim_expired_assets(self):
        store = LocalAssetStore(self.root, max_bytes=3, clock=lambda: self.now)
        first = store.put(b"old", scope="session-a")
        self.now = first.expires_at
        second = store.put(b"new", scope="session-a")
        self.assertEqual(store.get(second.id, scope="session-a")[1], b"new")
        self.now = second.expires_at
        reopened = LocalAssetStore(self.root, max_bytes=3, clock=lambda: self.now)
        self.assertEqual(reopened.cleanup(), 0)
        self.assertEqual(reopened.put(b"new", scope="session-b").size, 3)

    def test_quota_is_shared_between_scopes_and_failed_put_preserves_existing(self):
        store = LocalAssetStore(self.root, max_bytes=5, clock=lambda: self.now)
        first = store.put(b"123", scope="session-a")
        with self.assertRaises(StorageFull):
            store.put(b"456", scope="session-b")
        self.assertEqual(store.get(first.id, scope="session-a")[1], b"123")
        self.assertEqual(store.put(b"45", scope="session-b").size, 2)

    def test_concurrent_stores_cannot_exceed_quota(self):
        stores = [LocalAssetStore(self.root, max_bytes=5, clock=lambda: self.now) for _ in range(2)]
        start = threading.Barrier(2)

        def upload(store):
            start.wait(timeout=5)
            try:
                return store.put(b"1234", scope="session-a")
            except StorageFull:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(upload, stores))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get(winners[0].id, scope="session-a")[1], b"1234")

    def test_database_write_failure_rolls_back_bytes_and_quota(self):
        store = LocalAssetStore(self.root, max_bytes=4, clock=lambda: self.now)
        with sqlite3.connect(store.database) as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON assets BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.put(b"1234", scope="session-a")
        with sqlite3.connect(store.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_insert")
        asset = store.put(b"1234", scope="session-a")
        self.assertEqual(store.get(asset.id, scope="session-a")[1], b"1234")

    def test_modified_stored_bytes_fail_integrity_check(self):
        asset = self.store.put(b"original", scope="session-a")
        with sqlite3.connect(self.store.database) as db:
            db.execute("UPDATE assets SET data=? WHERE id=?", (b"tampered", asset.id))
        with self.assertRaises(AssetIntegrityError):
            self.store.get(asset.id, scope="session-a")

    def test_invalid_configuration_is_rejected(self):
        for ttl in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                LocalAssetStore(self.root, ttl=ttl)
        for quota in (0, -1, True, 1.5):
            with self.subTest(quota=quota), self.assertRaises(ValueError):
                LocalAssetStore(self.root, max_bytes=quota)

    def test_symlinked_directory_or_database_is_rejected(self):
        link = Path(self.directory.name) / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            LocalAssetStore(link)
        other = Path(self.directory.name) / "other"
        other.mkdir()
        (other / "assets.sqlite3").symlink_to(self.store.database)
        with self.assertRaises(ValueError):
            LocalAssetStore(other)


if __name__ == "__main__":
    unittest.main()
