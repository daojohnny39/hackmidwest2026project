# Section 2: private candidate image storage

Implemented and verified October 11, 2026. This section supports your IMG-02 byte
limits and IMG-03 original-image preservation requirements, plus the INT-01 asset
handoff. It does not complete image validation or face detection. Those checks
belong to Section 3.

Storage was included in the combined [Stage A checkpoint](member-3-stage-a.md).
Current Jev work and commit instructions are in the [Stage B guide](member-3-stage-b.md).

## Using the store

`candidate_processing.storage.LocalAssetStore` exposes `put`, `get`, `delete`
and `cleanup`. Construct it with a dedicated private directory, or let it use
`FACETRACE_DATA_DIR` (default `.facetrace-data/`). The store creates its directory
and SQLite database when instantiated; importing the module performs no I/O.

```python
from candidate_processing.storage import LocalAssetStore

store = LocalAssetStore()

# Both values come from the backend: retrieved bytes from Member 2, and the
# authenticated caller/session scope. Neither scope nor a local file path
# should be accepted from candidate JSON.
asset = store.put(retrieved_bytes, scope=trusted_scope, mime_type="image/jpeg")
candidate_image = {"status": "available", "assetId": asset.id}

metadata, original_bytes = store.get(asset.id, scope=trusted_scope)
# A later image processor saves each generated crop through a separate put().
store.delete(asset.id, scope=trusted_scope)
```

Returned `Asset` metadata contains `id`, `size` (bytes), `sha256`, `mime_type`
and `expires_at` (Unix timestamp). Candidate JSON uses only the opaque ID; the
pipeline will report verified image measurements separately. The supplied MIME
type is a hint, not proof that the bytes decode as an image. The store permits
JPEG, PNG and generic binary MIME hints, and does not decode images or fetch URLs.

## Behavior and limits

| Concern | Implemented behavior |
| --- | --- |
| Original preservation | Store exact immutable bytes; each upload/crop gets a fresh generated ID. There is no overwrite operation. |
| Caller access | Every put/read/delete requires a nonblank trusted scope, up to 128 characters. Reads/deletes for another scope return the same `AssetNotFound` as an unknown ID. |
| Path handling | Only generated 32-character lowercase hexadecimal IDs resolve. Candidate URLs and local paths cannot resolve as IDs. |
| Byte limits | Reject empty inputs and assets over 10 MiB. The 512 MiB store quota counts all asset payloads across scopes, including crops. |
| Lifetime | 24 hours by default. At expiry, reads fail immediately. Startup, uploads and explicit `cleanup()` remove expired rows and bytes. |
| Integrity | Read verifies the stored byte length and SHA-256 before returning bytes. |
| Concurrent writes | SQLite serializes quota checks and writes, so multiple store instances sharing a database cannot independently consume the same remaining quota. |
| Failed writes | Asset bytes and metadata commit together or roll back together. Existing assets survive failed uploads. |
| Local permissions | POSIX directory mode `0700`, database mode `0600`; reject a symlink for the configured root or database. |

Bytes and metadata live in `.facetrace-data/assets.sqlite3`. The quota applies to
asset payload bytes; SQLite metadata and temporary transaction journals require
additional disk space. Deletes clear payload cells and release reusable database
space. The database file need not shrink. These are local persistence controls,
not encryption or a guarantee of erasure from backups or filesystem snapshots.

There is no background service in this section. An idle application must call
`cleanup()` periodically if it needs expired bytes physically removed on a
timetable; expired assets are inaccessible even before cleanup. Member 1's
backend adapter will own that lifecycle and supply the trusted caller scope.
Use the same quota configuration for every process sharing a database.

`AssetNotFound` covers unknown, expired or unauthorized reads and unknown or
unauthorized deletes. An owner may delete its expired asset before cleanup.
`StorageFull` reports a quota failure. `AssetIntegrityError` reports mismatched
stored bytes. Invalid arguments raise `ValueError`; disk/SQLite failures propagate
for the future pipeline to record as storage-stage errors. No HTTP routes or
Strands wrapper are added in this section.

## Verification and commit

21 storage tests pass. They cover exact-byte round trips, persistence after
reopening, permissions, cross-scope access, malformed IDs, maximum byte size,
separate crops, quota reclamation, expiry boundaries, two concurrent store
instances, a forced database write failure, and corrupted stored bytes. All
41 storage and contract tests pass together. Tests use synthetic bytes and
temporary directories; no photos, credentials or network access are needed.

```bash
.venv/bin/python -m unittest discover -s tests/member3 -v
```

Storage is included in the completed [Stage A checkpoint](member-3-stage-a.md),
alongside validation, MediaPipe/OpenCV reuse, face crops and quality flags.
The [Stage B guide](member-3-stage-b.md) contains the subsequent Jev implementation
and its current verification and commit instructions.
