# Member 3 integration contract, version 1

The shared contracts, [private image-byte storage](../docs/member-3-storage.md),
and [Stage A image processing](../docs/member-3-stage-a.md) are implemented.
[Stage B](../docs/member-3-stage-b.md) adds Jev classification and prioritization;
live provider acceptance is pending credentials. [Stage C](../docs/member-3-stage-c.md)
implements the combined callable, HTTP endpoints, Strands tool and evaluation runner.
The defaults below are the implementation baseline for integration, pending
teammate integration. They do not change the existing front-camera contract.

## Payloads

- [Input example](examples/member3-input.json) and [input schema](member3-input.schema.json)
- [Output example](examples/member3-output.json) and [output schema](member3-output.schema.json)

Both examples are synthetic contract fixtures. The asset ID is a placeholder;
the output illustrates failure handling, not a completed processing run.

| Input field | Contract |
| --- | --- |
| `schemaVersion` | Required integer `1` |
| `requestId` | Required correlation ID |
| `captureId` | Optional correlation with the separate camera capture |
| `candidates` | Zero to 20 entries with unique `id` values |
| `id`, optional `sourceId` | Stable candidate and upstream source identifiers |
| `imageUrl`, `sourceUrl` | Required original HTTP(S) provenance URLs without embedded credentials; never fetched by this module |
| `subject` | A nonblank `name` or at least one nonblank `usernames` entry; optional `context` |
| `pageTitle`, `description` | Optional source text; maximum 500 and 4,000 characters |
| `profileMetadata` | Optional `username`, `organization`, `role`, `biography` |
| `image` | Retrieval state: `available` requires an `assetId`; `failed` requires a retrieval `error`; `unavailable` supports missing image bytes |

IDs contain 1–128 letters, digits, underscores or hyphens. Schemas list all
field limits. Unknown fields and incorrect JSON types are rejected. Validation
rejects a malformed batch before processing; operational failures within an
accepted batch become per-candidate results.

The runtime source of truth is `candidate_processing/contracts.py`. JSON Schemas
describe the structural contract. Cross-field rules also require runtime
validation: unique IDs, consistent image states, valid crop bounds, and complete
ranking/review lists. Use camelCase for serialized JSON; Python callers may use
snake_case attributes.

```python
from candidate_processing.contracts import CandidateBatch

batch = CandidateBatch.model_validate_json(request_bytes)
payload = batch.model_dump(mode="json", by_alias=True)
```

## Result semantics

Every accepted candidate retains its original IDs and provenance, including
duplicates and processing failures. `rankedCandidateIds` includes every
`disposition: candidate` result once, ordered by contextual relevance in Stage B.
Duplicate results reference a canonical candidate through
`duplicateOf`. `reviewCandidateIds` includes every result with
`reviewRequired: true`, including duplicates or invalid records when flagged.

Image status and classification status are independent. `faceCount: 0` means
successful inference detected no faces; a failed or unavailable analysis reports
`null`. Reaching the two-face capacity sets `atDetectorLimit: true`; it does not
establish the actual total. Crops use `[left, top, right, bottom]` pixel boxes in
the EXIF-oriented image and reference separate assets. Faces are not assigned to
the subject.

Classification reports contextual `relevance` (`high`, `medium`, `low`, `unknown`)
and `sourceType` (professional profile, social media, news article, personal
website, forum, or unknown). Failed/skipped classification uses `unknown` for
both decisions. Evidence records contain a reason code, explanation, optional
source field/excerpt, and a `rule`, `model`, or `operational` method. These
decisions do not establish facial identity or source trustworthiness. Stage B
records the returned model and rubric version plus optional `sdkVersion`.

## Image transfer and storage defaults

Member 2 retrieves external images. The backend can now use `LocalAssetStore.put`
to store bytes and pass the returned opaque `assetId` to Member 3, or use the
authenticated HTTP upload route below. Candidate JSON
contains no base64 image, local path or download instruction. Hashes used for
deduplication are computed from stored bytes, not accepted from caller metadata.

| Setting | Default |
| --- | --- |
| Private local storage | `FACETRACE_DATA_DIR`, default `.facetrace-data/`, excluded from Git |
| Access | Backend-assigned caller/session scope; original images and crops share that scope |
| Lifetime and quota | 24 hours; 512 MiB total asset payload quota (SQLite overhead is additional) |
| Image limits | Still JPEG/PNG, up to 10 MiB, 20 million decoded pixels, 10,000 px per side |
| Crops | Up to two padded 256 × 256 JPEG assets; originals remain immutable |
| Batch execution | At most 20 candidates, sequential vision work; Stage C enforces a combined 120-second processing deadline; safe cancellation cleanup can add latency |
| Jev execution | 10-second total deadline per candidate, at most one retry |

The initial store is for local integration. Member 1 supplies production storage
and authentication bindings. Asset IDs do not themselves grant access. Request
payloads cannot select another caller's scope. Jev receives bounded text context,
not image pixels, crops or asset access tokens.

## HTTP adapter

These routes are implemented by the optional `candidate_processing.http_adapter`.
Member 1 can also call `candidate_processing.pipeline.analyze_candidates` directly.

| Route | Request / response |
| --- | --- |
| `POST /api/v1/assets` | Multipart `file`; returns `assetId`, verified byte length, SHA-256 and expiry |
| `GET /api/v1/assets/{assetId}` | Returns authorized image bytes |
| `DELETE /api/v1/assets/{assetId}` | Deletes an authorized asset |
| `POST /api/v1/candidates/analyze` | JSON `CandidateBatch`; returns JSON `BatchResult` within the request |

The local HTTP adapter requires `Authorization: Bearer <FACETRACE_API_TOKEN>`
(at least 32 characters) and obtains its single caller's scope from
`FACETRACE_ASSET_SCOPE` or explicit backend configuration. Scope is never taken
from candidate JSON or query parameters. Upload MIME is verified by the isolated
decoder. Unauthorized bodies are rejected before parsing; multipart and JSON
bodies are bounded even without Content-Length. Upload `expiresAt` is a Unix
timestamp in seconds. Missing/expired/unauthorized asset reads return 404;
invalid inputs return 422, oversized requests 413, and quota failures 507.
Investigation creation, camera uploads, job polling, discovery and threat
assessment remain Member 1/2/4 integration work.

## Configuration and checks

`requirements-member3.txt` pins the Python dependencies for the module;
it includes the existing camera/checkpoint requirements. `.env.example` documents
server-side configuration. This module does not load dotenv files;
export variables in the server process when using Jev or the HTTP service. Contract checks need no
credentials, camera, downloaded model or external requests.

From the repository root:

```bash
.venv/bin/python -m pip install -r requirements-member3.txt
.venv/bin/python -m candidate_processing.export_contracts
.venv/bin/python -m unittest discover -s tests/member3 -v
.venv/bin/python -m pip check
```

Regenerate both schemas whenever runtime models change. The contract test suite
checks that the committed schemas match the exporter.
