# Stage B: TypeSafe Jev classification

Implemented October 11, 2026. Automated verification passes. **A successful live
Jev call is still pending:** the smoke command reports `jev_credentials_missing`
because `TYPESAFE_API_KEY` is not configured in the checked environment.

## Task coverage and files

| Task | Implementation | Acceptance status |
| --- | --- | --- |
| AI-01: SDK integration | `jev_client.py`: installed `typesafe-sdk==0.7.4`, async lifecycle, bounded retries, sanitized failures and response validation | Real SDK exercised through mock HTTP; live response pending |
| AI-02: relevance | `classification.py`: constrained labels, contextual evidence excerpts and conservative rubric checks | Automated policy cases pass; model accuracy evaluation remains Stage C |
| AI-03: source categories | Same classification module; six allowed categories including `unknown` | All categories and invalid responses covered offline |
| AI-04: prioritization | `stage_b.py`: attach decisions to Stage A, group equivalent records, stable ordering and independent review | Failure, duplicate and deadline cases pass |

Three implementation modules, two test files and this guide cover Stage B.
The existing contracts add optional `classification.sdkVersion`; its committed
output schema is regenerated. No dependency, service, database, frontend or
camera-setting changes are needed.

Official [Python SDK documentation](https://docs.typesafe.ai/sdk/python),
[usage guide](https://docs.typesafe.ai/sdk/python/usage) and
[API reference](https://docs.typesafe.ai/api) were checked against the installed
SDK before implementation. The import is `typesafe_sdk`; the method is
`AsyncTypeSafeClient.system_one()` with typed `Choice` questions.

## Calling Stage B

For one already validated candidate and its Stage A result:

```python
from candidate_processing.classification import classify_candidate
from candidate_processing.jev_client import JevClient
from candidate_processing.stage_b import apply_classification

async def classify_one(candidate, stage_a_result):
    async with JevClient() as provider:
        outcome = await classify_candidate(candidate, provider)
    return apply_classification(stage_a_result, outcome).model_dump(
        mode="json", by_alias=True,
    )
```

For an existing `CandidateBatch` and backend-produced Stage A results:

```python
from candidate_processing.stage_b import classify_analyzed_batch

async def classify_batch(batch, stage_a_results):
    async with JevClient() as provider:
        result = await classify_analyzed_batch(batch, stage_a_results, provider)
    return result.model_dump(mode="json", by_alias=True)
```

Reuse a client across candidates or batches within the backend's async lifecycle.
The batch function verifies every candidate ID and its original provenance before
making requests. Stage A measurements/hashes must come from the backend analyzer,
never from client-supplied JSON. An invalid or unavailable image can still have
useful page context and a successful classification.

## Text, evidence and rubric

Only subject name/usernames/context and allowlisted page/profile text reach Jev.
Image URLs, asset IDs, pixels, crops, landmarks and capture identifiers are omitted.
HTTP URL credentials, query strings and fragments are removed from the text view;
original source URLs remain in the returned candidate provenance. This is not a
general secret detector: callers must supply appropriate public text metadata.

Each page/profile field and the source URL are limited to 400 sanitized characters,
so reasons can reference the exact excerpt assessed. Subject fields retain their
existing contract limits. Truncated page context forces `unknown` plus review
because unseen text may contradict the excerpt. A URL alone skips classification.

Names/usernames use Unicode NFKC, case folding and collapsed whitespace for
comparison; a username's leading `@` is optional. Matches require token boundaries,
and original inputs are preserved. These conservative rules do not resolve aliases
or establish identity. A name alone is `unknown`; a username alone is at most
`medium`. `high` requires an identifier match and model-assessed support for supplied
subject context. Supporting and conflicting details together force `unknown`;
material conflicting evidence always requires review.

Jev answers constrained relevance, source category and evidence-location questions,
plus a support/conflict/unclear/none question for each populated page/profile field.
Every question explicitly treats page text as untrusted data. The application builds
readable reasons from field excerpts and decisions; they are not hidden model
reasoning. Initial model decisions are distinguished from the returned label after
rubric checks. Model-assessed evidence is labeled `model`. Missing data and failures
use operational reasons. Prompt-injection resistance and relevance accuracy still
need the planned live labeled evaluation; offline tests do not establish either.

Source categories describe page type, not reputation. Provider probabilities are
not exposed as identity percentages. Model ID, SDK version and `context-v1` rubric
version accompany successful classifications.

## Failure handling, ordering and reuse

The SDK owns the only retry policy: at most one retry, honoring `Retry-After`.
HTTP operations time out after at most four seconds; an outer ten-second deadline
also bounds in-flight requests and backoff. Authentication/request errors are not
retried. Missing credentials, invalid responses and exhausted retries return
`unknown` with a structured error. Raw provider errors are not echoed.

Stage B has a 120-second batch deadline. Completed results survive; unfinished
items receive explicit timeout outcomes. External cancellation propagates.
The future combined Stage A/B orchestrator must enforce the overall batch budget.

Canonical results sort `high`, `medium`, `low`, `unknown`, preserving input order
for ties. Every accepted record remains in `candidates`; flagged results also
appear in `reviewCandidateIds`. High relevance cannot clear image, quality,
multiple-face or storage concerns.

Equivalent records reuse one classification only when subject, original source
URL, full original page/profile metadata and a backend-verified image digest agree.
Duplicates retain their own provenance and image outcomes, and reference the first
canonical record. The same image on different pages remains separate evidence.
Missing, invalid or unverifiable images cannot establish duplicate records.
Reuse is confined to this batch; no persistent cache is created. Reusing image
analysis before vision inference remains part of Stage C's combined orchestrator.

## Verification and live smoke test

Verification on October 11, 2026: **141 Python tests pass**, including 53 new
Stage B cases and all prior regressions. `pip check` reports no broken requirements.

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

The suite covers real SDK/mock HTTP serialization, required answer keys/types,
unknown enums, missing credentials, timeouts, retry exhaustion, cancellation,
name collisions, sparse context, contradictions, embedded instructions, bounded
excerpts, source categories, stable ranking, duplicates and complete failure results.
A real local MediaPipe blank-image test feeds Stage B with mocked Jev decisions.
These checks are synthetic and make no live classification or accuracy claim.

Configure `TYPESAFE_API_KEY` securely in the **server process environment**, then run:

```bash
.venv/bin/python -m candidate_processing.stage_b --live
```

This command sends only the built-in fictitious example text. It does not download
anything or send photographs. A validated provider response yields exit code 0
and `liveProviderResponseValidated: true`; failures yield exit code 1. No `--live`
flag means no request. `.env.example` is a template; this module does not load
dotenv files. `TYPESAFE_DEFAULT_MODEL` defaults to `jev-latest`. The adapter fixes
the endpoint to TypeSafe's official API. Avoid SDK debug logging for real subject
data: the SDK can log request and response bodies at debug level.

## Review and commit

Commit Stage B in four functional groups, in the order below. AI-02 and AI-03
share one module and test suite, so they stay together. SDK behavioral tests are
also in that shared suite and enter in the second commit. No file splitting or
additional files are needed.

If all 14 Stage B files are already staged, first unstage them. This preserves
their contents. Run from the project root, with only these Stage B changes staged:

```bash
git branch --show-current
git restore --staged .
```

The current branch is `ba-image-proc-2`. Before each commit, review
`git diff --cached --stat` and `git diff --cached` (press `q` to exit the viewer).

### B1 — AI-01: Jev SDK and contract support (4 files)

Adds the server-side client, deadlines/retries, configuration template and
SDK-version field with its matching schema. Existing contract tests apply here;
the shared SDK/classification behavior tests arrive in B2.

```bash
git add .env.example candidate_processing/jev_client.py \
  candidate_processing/contracts.py contracts/member3-output.schema.json
.venv/bin/python -m unittest tests.member3.test_contracts -q
git diff --cached --stat
git diff --cached
git commit -m "feat: add server-side Jev client and SDK metadata"
```

### B2 — AI-02 and AI-03: relevance and source classification (2 files)

Adds the contextual rubric, evidence excerpts, source categories and shared
SDK/classifier tests. Depends on B1.

```bash
git add candidate_processing/classification.py tests/member3/test_classification.py
.venv/bin/python -m unittest tests.member3.test_classification -q
git diff --cached --stat
git diff --cached
git commit -m "feat: classify candidate relevance and source categories"
```

### B3 — AI-04: prioritization and Stage A handoff (2 files)

Adds classification attachment, duplicate-record reuse, stable ranking, review
preservation, batch deadlines and the synthetic live smoke command with tests.
Depends on B1 and B2. Run the full regression suite at this checkpoint.

```bash
git add candidate_processing/stage_b.py tests/member3/test_stage_b.py
.venv/bin/python -m unittest discover -s tests -q
git diff --cached --stat
git diff --cached
git commit -m "feat: prioritize candidates and preserve batch review outcomes"
```

### B4 — Stage B documentation and handoff (6 files)

Records usage, verification, current implementation status, the pending live
provider check and the Stage C boundary. Updates stale documentation links.

```bash
git add README.md contracts/README.md docs/member-3-implementation-plan.md \
  docs/member-3-stage-a.md docs/member-3-stage-b.md docs/member-3-storage.md
git diff --cached --stat
git diff --cached
git commit -m "docs: document Stage B workflow and acceptance status"
git status --short
git log -4 --oneline
```

If these are the only local changes, the final status is empty. This guide does
not publish the branch. The live Jev acceptance check remains pending credentials;
successful offline tests do not change that status.

Stage C retains the combined image/classification orchestrator, shared vision reuse,
HTTP/Strands adapters, teammate contract acceptance and manually labeled evaluation.
Stage A's real-photo calibration and physical-camera acceptance also remain pending.
