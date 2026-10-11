# Stage C: integration and validation

Stage C adds the combined callable, authenticated local HTTP routes, a Strands
tool factory and an evaluation runner. It retains the version 1 contract.
Camera code and capture defaults are unchanged. No agent, discovery workflow,
face comparison, reputation lookup or deployment is added.

## Direct integration

Member 2 retrieves image bytes and stores them under a backend-assigned scope.
Member 1 calls the function in its async workflow:

```python
from candidate_processing.pipeline import analyze_candidates
from candidate_processing.storage import LocalAssetStore

store = LocalAssetStore()

async def process(candidate_batch, authenticated_session_id):
    return await analyze_candidates(
        candidate_batch, store=store, scope=authenticated_session_id,
    )
```

`candidate_batch` is a dictionary matching `contracts/member3-input.schema.json`.
The return value is a JSON-compatible dictionary matching the output schema.
Malformed batches raise Pydantic `ValidationError` before any processing.
Every accepted candidate retains its original IDs, asset reference and source
URLs, including duplicates and operational failures. Member 1 supplies the
trusted scope; subject names and payload fields never choose it.

The optional `analyzer` and `provider` arguments accept backend-owned injected
dependencies. Without a provider, the function opens and closes one `JevClient`.
An injected provider belongs to its caller and can be reused within that caller's
async lifecycle. A shared `IsolatedImageAnalyzer` serializes inference. The
default HTTP app shares one analyzer across requests.

The combined 120-second deadline includes authorized asset reads, image work,
crop storage, and classification. Vision runs first, then Stage B receives the
same absolute deadline. Completed image/classification results survive expiry;
unfinished stages receive explicit timeout errors and review flags. Worker
termination and pending storage cleanup may add time after the deadline. External
cancellation propagates. Storage retention still applies to completed crop assets.

Byte hashes are recomputed after each authorized read. Successful image analysis
and its crop references are reused only within the current batch and scope.
Operational failures can be retried by later candidates. Different source pages
or changed metadata receive separate contextual decisions. Stage B groups truly
equivalent records, retains every source, and preserves stable relevance ordering.

## Strands integration

```python
from candidate_processing.strands_adapter import create_candidate_tool

candidate_tool = create_candidate_tool(
    store=store, scope=authenticated_session_id,
)
# Member 1 adds candidate_tool to the tools of its existing Strands agent.
# It can also invoke the tool directly without a model:
result = await candidate_tool(request=candidate_batch)
```

The factory uses the installed `strands-agents==1.59.0` decorator and full input
schema. The model sees only `request`; storage, scope, credentials and runtime
limits are bound by backend code. Success returns Strands content containing the
JSON BatchResult; invalid batches return a sanitized error. Per-candidate failures
remain within a successful batch response and require review. Tests invoke the
actual decorated tool through the SDK's `stream` dispatcher, without creating an
agent or calling AWS. Member 1 still needs to register it in their workflow and
confirm the handoff with Member 2.

Reference: [official Strands custom-tool documentation](https://strandsagents.com/docs/user-guide/sdk/tools/custom-tools/).

## Local HTTP service

Install `requirements-member3.txt`. Export a random `FACETRACE_API_TOKEN` of at
least 32 characters, a backend-assigned `FACETRACE_ASSET_SCOPE`, and optionally
`FACETRACE_DATA_DIR` and `TYPESAFE_API_KEY`. This module does not load `.env`.
Missing authentication settings fail before storage creation.

```bash
.venv/bin/python -m uvicorn candidate_processing.http_adapter:create_app_from_env \
  --factory --host 127.0.0.1 --port 8000
```

| Route | Behavior |
| --- | --- |
| `POST /api/v1/assets` | One multipart `file`; isolated content validation; returns `assetId`, `byteLength`, `contentSha256`, `mimeType`, `expiresAt` |
| `GET /api/v1/assets/{assetId}` | Authorized original/crop bytes as a private attachment |
| `DELETE /api/v1/assets/{assetId}` | Authorized deletion; 204 on success |
| `POST /api/v1/candidates/analyze` | Version 1 batch in; version 1 result out |

All routes require the bearer token. This is a single-principal local adapter;
Member 1 owns production authentication, multi-user authorization and deployment.
Uploads are authenticated before parsing or temporary-file spooling. Streaming
bodies are capped at 10 MiB plus 64 KiB of multipart overhead, with an independent
10 MiB file limit. JSON is capped at 2 MiB. Body receipt has a 30-second timeout,
separate from the processing deadline. Image filenames and MIME headers cannot
override content validation. Asset responses use `no-store`, `nosniff` and
attachment disposition. The Stage A worker's documented OS-isolation limits
still apply; see [Stage A](member-3-stage-a.md).

Reference: [FastAPI file handling](https://fastapi.tiangolo.com/tutorial/request-files/).

## Evaluation and verification

See [evaluation findings and commands](../evals/member3/README.md). The 40 fixtures
are synthetic, with **agent-authored provisional labels**, split 24 development
and 16 held-out cases with distinct subjects/pages. They include ten name
collisions and ten sparse cases. They are ready for human label review; they are
not claimed to be manually labeled by a person. The offline report uses a fixed
abstaining mock and establishes no Jev accuracy.

```bash
.venv/bin/python -m unittest discover -s tests/member3 -q
.venv/bin/python -m unittest tests.test_opencv tests.test_face_landmarker tests.test_photo_checkpoint -q
.venv/bin/python -m candidate_processing.evaluation --output evals/member3/offline-report.json
.venv/bin/python -m pip check
```

New tests cover callable JSON round trips, hash reuse, distinct-page decisions,
same-name conflict, corrupt/no-face images, scope isolation, shared deadlines,
cancellation, real Strands dispatch, HTTP parsing/authentication/upload limits,
quota failures, and metric arithmetic. The local blank-image case exercises
real MediaPipe when its model is present; Jev responses in automated tests are
mocked. Live Jev verification, human-reviewed labels, actual model quality,
teammate workflow acceptance and real-photo calibration remain outstanding.

Verified October 11, 2026: the full repository Python suite passed **860 tests**
after installing its pinned dependencies and allowing the tests' localhost mock
servers. Two additional evaluation edge cases were then added and the affected
HTTP/evaluation suites passed again, bringing new Stage C coverage to **34 tests**.
`pip check`, compilation and whitespace checks passed. The synthetic live-smoke
command returned `jev_credentials_missing`; no live provider success is claimed.
