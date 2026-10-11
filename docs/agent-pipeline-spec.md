# Agent pipeline spec

Owner: Johnny. Drafted 2026-10-10. Revised 2026-10-10 after review. Status: draft.

This spec covers the agent pipeline, the right half of `../docs/architecture.png`: everything from the AWS Strands Agent onward. The capture pipeline (camera, MediaPipe, frame selection) belongs to the teammates and shows up here only as the input contract in Section 0. Paths here are relative to the repo root.

Items marked **Proposal** answer an open question from `../brain/open-questions.md`. Dependent contracts, thresholds, and test targets are proposed implementation defaults too. Only `../brain/decisions.md` records decisions. The recorded choices are one Strands agent per image, Jev for typed decisions, Yandex through SerpApi, and the local mock for development. FaceCheck face search replaced Yandex as the default backend on 2026-10-10 (Johnny), after a live Yandex run on his own photo returned only lookalikes; the state before that is tagged `cyberpi-backend-v1`. Backend V3 replaced FaceCheck as the default later that day, at Johnny's request: SerpApi searches the open web for a crop of the participant's face, and Amazon Rekognition checks the face in every result (`SEARCH_BACKEND=rekognition`, 1.1). FaceCheck's default state is commit `25e807f`. Johnny then dropped FaceCheck, and V3 added Trace's face index as its main engine, since Yandex and Lens only find lookalikes for a new photo. Section 0 (the `pipeline/` package foundation and mock capture fixtures), Section 1 (search and scrape), Section 2 (profiles and graph), Section 3 (verification), and Section 4 (summarization) are built; Section 5 is not finished. Johnny owns Sections 0–5; teammates own capture and must agree to the handoff.

## Contents

1. How the diagram maps to sections
2. Ground rules
3. Section 0: Foundation
4. Section 1: Per-image search and scrape → raw structured data
5. Section 2: Parse, build profiles, connection graph (Jev)
6. Section 3: Verification and confidence
7. Section 4: Summarization
8. Section 5: End-to-end run and demo
9. Mock test matrix
10. Decisions to confirm
11. Build checklist

## 1. How the diagram maps to sections

| Diagram box | Section | Artifact written |
|---|---|---|
| Handoff from capture | 0 | `00_capture.json` |
| AWS Strands Agent → Reverse-image search tool → "Finds internet presence" → raw structured data | 1 | `01_raw_evidence.json` |
| Typesafe Jev → "Parses name and general bio" | 2a | `02_profiles.json` (accounts) |
| Builds Structured Profiles | 2b | `02_profiles.json` (profiles) |
| Graph (per profile, trust values) | 2c | `02_profiles.json` (graphs) |
| Verification/Synthesize Pass → confidence per candidate | 3 | `03_verification.json` |
| Summarization → Profile | 4 | `04_final.json` |

Each section reads the artifacts named in its **In** line and writes its **Out** artifact. Section 0 creates the run and consent record. Later stages validate that record too. This lets Section 2 use saved evidence without repeating searches. The diagram's Jev box covers extraction and decisions; code or the Strands LLM extracts text, and Jev only answers typed questions.

## 2. Ground rules

- **Consent gate.** Require affirmative participant consent before reading real images, creating an artifact, or making a call. Live consent must cover image disclosure to SerpApi, Yandex, Google, AWS Rekognition and the tunnel for V3 (or FaceCheck, or S3, the tunnel, SerpApi and Yandex), and evidence disclosure to the selected model providers. Apply the gate to `run`, `stage`, and `smoke-search`. Real tests use consenting team members; mock tests use fictional participants.
- **Backend V3 by default, FaceCheck and SerpApi alone as alternatives, mock for tests.** Search defaults to backend V3, SerpApi search with Rekognition face matching (Johnny, 2026-10-10; it replaced FaceCheck, which had replaced SerpApi earlier that day). `SEARCH_BACKEND=facecheck` selects FaceCheck, `SEARCH_BACKEND=serpapi` selects Yandex alone through SerpApi, and `run --mock` or `SEARCH_BACKEND=mock` selects the mock internet, which costs no search credits. `LLM_MODE=off` and `JEV_MODE=fake` stay the defaults, and `--models` separately permits paid model calls, including models used with mock evidence. Normal tests force the mock and block non-loopback connections.
- **Code holds the data; models make judgments.** Tools store evidence and provenance. The LLM may extract supported fields, triage URLs, and write a sourced bio. Code sets IDs, graph values, confidence, and acceptance. Treat page text as untrusted data, never as tool instructions.
- **Every model step has a fallback.** Tests run without an LLM or Jev. Runtime failures use the rules and template below and record the fallback. A mock demonstration is not proof of live retrieval or identity accuracy.
- **Keep data out of git.** Add `runs/` and `captures/` to `.gitignore` before collecting real data; they are not ignored today. Existing rules cover `.env`, bytecode, and `data/*.sqlite3*`. Keep real photos, pages, prompts, responses, caches, and traces under those ignored roots. Commit only synthetic fixtures and blank key placeholders.
- **Limit retention.** Delete hosted images in `finally`, including failed searches. Purge run data and caches after the demo or consent withdrawal. Selected evidence leaves the machine when models are enabled. Deleting our copies cannot promise deletion from provider logs or caches; explain that during consent. Keep prompt/content tracing local: `trace.jsonl` lives in the run folder and is never exported (no telemetry exporter). Redact keys and signed URLs from logs and traces.
- **Public, professional facts only.** The final profile contains name, role, workplace, city-level location, a short bio, and account links. Nothing is inferred about age, ethnicity, religion, health, relationships, or home address.

## 3. Section 0: Foundation

This is the small shared groundwork every later section uses. Build it first, alongside Section 1.

### 0.1 Repo layout

```
Project Directory/
  mock_internet/              exists: fake web, SQLite, local client
  pipeline/
    __init__.py
    __main__.py               CLI: run, stage, smoke-search, purge
    config.py                 settings from env and .env
    contracts.py              Pydantic models for every artifact in this spec
    capture.py                load and validate a capture bundle; consent gate
    artifacts.py              read/write runs/<run_id>/NN_*.json and run_log.jsonl
    trace.py                  per-run debug trace: runs/<run_id>/trace.jsonl
    serpapi_account.py        SerpApi Account API: free key check and quota for `set-key`
    aws_credentials.py        `set-key aws`: free STS key check, save to ~/.aws/credentials
    platforms.py              domain → platform, account-URL patterns, URL normalization
    scrape.py                 HTML → PageRecord (meta, Open Graph, JSON-LD, links)
    search/
      base.py                 SearchBackend protocol
      mock_backend.py         wraps MockInternetClient
      serpapi_backend.py      SerpApi yandex_images + bounded live HTTP fetch (LivePages)
      facecheck_backend.py    FaceCheck.ID face search; reuses LivePages for page fetches
      rekognition_backend.py  backend V3: SerpApi search of a face crop, Rekognition CompareFaces on every result
    hosting.py                live only: upload image, presigned URL, delete
    jev.py                    Jev wrapper with modes typesafe | openrouter | fake | off
    llm.py                    Strands model factory (one provider first)
    stage1_collect.py
    stage2_profiles.py
    stage3_verify.py
    stage4_summarize.py
  tests/
    fixtures/captures/        mock capture bundles: alex, sam, casey, unknown
    test_mock_internet.py     exists
    test_stage1.py … test_stage4.py, test_end_to_end.py
  captures/                   proposed ignored root for real capture folders
  runs/                       must be gitignored; one folder per run
  requirements.txt
```

### 0.2 Dependencies

Use Python 3.12 for the pipeline. Declare `pydantic`, `httpx`, `beautifulsoup4`, `pillow` (image validation), and `python-dotenv` directly. Add `strands-agents[anthropic]` for the proposed Anthropic route, `typesafe-sdk` for direct TypeSafe, and `boto3` for S3, Bedrock, or Rekognition (backend V3). The existing mock needs only the standard library. `requirements.txt` pins the versions that installed cleanly on Python 3.12 (created with `uv venv --python 3.12 .venv`, since this machine's Python 3.14 has no pip) and passed an import-only SDK smoke check. Import model clients lazily so offline mode needs no credentials.

**Proposal:** build one LLM provider and the Jev route matching Johnny's key first. Keep the wrapper boundary; defer the second provider, text-search expansion, and graph-driven re-merging. Plain Python owns stage order. No graph database or multi-agent orchestration framework is needed.

### 0.3 Configuration

| Variable | Values | Default | Used by |
|---|---|---|---|
| `SEARCH_BACKEND` | `rekognition` (V3), `facecheck`, `serpapi`, `mock` | `rekognition` | Section 1 |
| `MOCK_INTERNET_URL` | loopback URL | `http://127.0.0.1:8765` | mock backend |
| `MOCK_SCENARIO` | `default`, `ambiguous`, `stale`, `duplicates`, `empty`, `rate_limited`, `server_error` | `default` | mock backend |
| `MOCK_DELAY_MS` | integer 0–5000, search requests only | `0` | concurrency tests |
| `FACECHECK_API_KEY` | token; save with `set-key facecheck` | none | facecheck backend |
| `FACECHECK_DEMO` | `true`/`false`; true searches only 100,000 faces, free | `false` | facecheck backend |
| `SERPAPI_KEY` | key; save with `set-key serpapi` | none | rekognition and serpapi backends |
| `TRACE_API_KEY` | key (`trk_live_…`); save with `set-key trace` | none | rekognition backend, `trace` engine |
| `REKOGNITION_ENGINES` | comma-separated: `trace`, `yandex_images`, `google_lens` | all three | rekognition backend |
| `REKOGNITION_MIN_SIMILARITY` | CompareFaces similarity 0–100; results under it are dropped | `80` | rekognition backend |
| `REKOGNITION_PLATFORMS` | Platforms to keep, comma-separated (`linkedin`, `x`, `instagram`, `github`, `facebook`, `blog`, `news`); results on other sites are dropped before comparing. `linkedin,facebook` is V3_1 | unset: every site | rekognition backend |
| `IMAGE_HOST` | `tunnel` or `s3` | `tunnel` | `hosting.py` (live only) |
| `IMAGE_BUCKET` + AWS credentials | S3 bucket name; keys via `set-key aws` (to `~/.aws/credentials`) | none | `hosting.py` (`IMAGE_HOST=s3`) |
| `JEV_MODE` | `fake`, `typesafe`, `openrouter`, `off` | `fake` | Sections 2, 3 |
| `JEV_MODEL` | route-specific model ID | `jev-latest` for TypeSafe; `typesafe/jev-1.13` for OpenRouter | `jev.py` |
| `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` | key | none | `jev.py` |
| `LLM_MODE` | `off`, `anthropic`, `bedrock` | `off` | Sections 1–4 |
| `ANTHROPIC_API_KEY` | key | none | `llm.py` |
| `LLM_MODEL` | provider-specific model ID | `claude-opus-5-5` for Anthropic (V4); required for Bedrock | `llm.py` |
| `LLM_EFFORT` | `low`, `medium`, `high`, `xhigh`, `max`; applies to Opus 5.5, Sonnet 5.5, and Fable 5.1 on Anthropic | `medium` (V4; V3 ran Sonnet 5.5 at `low`) | `llm.py` |
| `AWS_REGION` | region with Rekognition, bucket, or model access | required for V3, S3, and Bedrock | Rekognition, hosting, and Bedrock |

`config.py` loads `.env` without overriding exported variables. CLI options win. Search defaults to `rekognition` (V3), which requires `SERPAPI_KEY`, `AWS_REGION`, and AWS credentials; `facecheck` requires `FACECHECK_API_KEY`; `serpapi` requires `SERPAPI_KEY`; `run --mock` or `SEARCH_BACKEND=mock` selects the mock. Hosting settings (`IMAGE_HOST`, then `ssh` for the tunnel or `IMAGE_BUCKET` for S3) are checked by `hosting.require_hosting` before a SerpApi search, and AWS credentials by `rekognition_backend.require_rekognition` before a V3 search, not at load, so stages that only read saved evidence don't need them. Real `JEV_MODE` or `LLM_MODE` requires `--models`. Missing credentials or an unimplemented provider fail before uploads. Bedrock uses AWS credentials and model access, not `ANTHROPIC_API_KEY`. Add these variables to `.env.example` during implementation, with blank secrets and safe non-secret defaults.

`python -m pipeline set-key {facecheck,serpapi,typesafe,openrouter,anthropic,aws}` prompts for a key with hidden input, so it never lands in shell history, and writes it to `.env`. A missing `.env` starts as a copy of `.env.example`. python-dotenv's parser locates the assignment, so quoted keys, multi-line values, comments, and CRLF endings survive. The file is replaced atomically, through a temp file named `.env.*`, so a crash can't leave an unignored copy. The command requests mode 0600, which protects the key on filesystems that enforce POSIX permissions. On `/mnt/c` those modes aren't enforced, and `.env` inherits the folder's Windows ACLs, so protect the repo folder there. If the terminal can't hide input, the command refuses before reading anything. A FaceCheck token is first checked with FaceCheck's free `POST /api/info`, which returns the credit balance and runs no search (a missing or invalid token gets HTTP 200 with `code` `INVALID_TOKEN` or `EXCEPTION`, checked 2026-10-10); a rejected token isn't saved. A SerpApi key is first checked against SerpApi's free [Account API](https://serpapi.com/account-api), which doesn't count toward the search quota. The command prints only the plan and quota fields, and saves nothing if SerpApi rejects the key. If SerpApi can't be reached it saves with a warning. A TypeSafe key is checked by listing TypeSafe's models (`GET /v1/models`, authenticated; a fake key gets 401, checked 2026-10-10), which makes no Jev decision. A rejected key is not saved, and an unreachable check saves with a warning. `--no-check` skips the checks. Saving a model key never changes `JEV_MODE` or `LLM_MODE`; the command prints the setting to change, and real modes still need `--models`. `set-key aws` (`aws_credentials.py`) is the exception to `.env`: Rekognition, S3 hosting and Bedrock use boto3's standard credential chain, so it saves an IAM user's access key pair as the `[default]` profile of `~/.aws/credentials` (or `AWS_SHARED_CREDENTIALS_FILE`), keeping other profiles, with mode 0600 in a 0700 folder, outside the repo and off `/mnt/c`. Both prompts are hidden. Key formats are checked first, and temporary `ASIA` keys are refused. The keys are checked with STS GetCallerIdentity, which is free and needs no IAM permissions, and the command prints only the account and ARN, warning on root-user keys. Rejected keys aren't saved (real STS answers `InvalidClientTokenId` for AWS's example key, checked 2026-10-10). `--region` also saves `AWS_REGION` to `.env`. `OfflineTestCase` points `AWS_SHARED_CREDENTIALS_FILE` and `AWS_CONFIG_FILE` at `os.devnull` and sets `AWS_EC2_METADATA_DISABLED`, so tests never load the saved keys or ask the instance metadata service.

The Anthropic model ID above is documented. The existing Haiku alternative, `claude-haiku-4-5-20251001`, is also documented; access still needs a smoke check. Never reuse an Anthropic API ID as a Bedrock ID. See [Claude model IDs](https://platform.claude.com/docs/en/models/overview) and [Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview).

### 0.4 Capture handoff contract (**Proposal**, to agree with teammates)

The capture side writes one folder per session:

```
captures/<session_id>/
  manifest.json
  img-1.jpg
  img-2.jpg
  img-3.jpg
```

```json
{
  "session_id": "2026-10-10T14-02-11-johnny",
  "captured_at": "2026-10-10T14:02:11Z",
  "consent": { "confirmed": true, "participant": "Johnny", "confirmed_at": "2026-10-10T14:01:50Z", "external_processing": true },
  "images": [
    { "image_id": "img-1", "file": "img-1.jpg", "angle": "front", "yaw": 1.5,   "pitch": -2.0, "roll": 0.3 },
    { "image_id": "img-2", "file": "img-2.jpg", "angle": "left",  "yaw": -32.0, "pitch": 0.0,  "roll": 1.1 },
    { "image_id": "img-3", "file": "img-3.jpg", "angle": "right", "yaw": 30.5,  "pitch": 1.2,  "roll": -0.4 }
  ]
}
```

- Images are JPEG or PNG. **Proposal:** cap each at 10 MB; recommend 400 px on the short side without rejecting smaller captures. Agree on crop and image quality with the capture team.
- `angle` is `front`, `left`, `right`, `up`, `down`, or `unknown`. Optional `yaw`, `pitch`, and `roll` are capture-side estimates in degrees; agree on axes and sign conventions. Do not assume Face Landmarker returns these angles directly.
- The pipeline accepts 1–8 images. **Proposal:** use X = 3 (front, left, right). Budget one successful uncached search per image; retries can add requests. Current free limits are in 1.1.
- Mock bundles use one fixture per image, without files: alex = `alex-front`, `alex-left`, `alex-right`; sam = `sam-front`; casey = `casey-front`; unknown = `unknown`. Alex image IDs are `img-1`, `img-2`, `img-3` in that order. Synthetic manifests set both consent fields true so optional model smoke checks can use them. The placeholder SVGs are not photos and do not need image validation.
- Reject duplicate image IDs, empty bundles, unknown fixtures, and paths or symlinks escaping the capture folder. Live mode requires real files and rejects `mock_fixture`. Validate actual image format and size before uploading. Session IDs must be safe path components.
- HTTP/base64 handoff is deferred. It would need an endpoint, payload limits, and revised input validation as well as `capture.py`.

```python
class Consent(BaseModel):
    confirmed: bool
    participant: str
    confirmed_at: datetime
    external_processing: bool = False  # required true for live search or --models

class CaptureImage(BaseModel):
    image_id: str
    file: str | None = None           # required unless mock_fixture is set
    angle: Literal["front", "left", "right", "up", "down", "unknown"]
    yaw: float | None = None
    pitch: float | None = None
    roll: float | None = None
    mock_fixture: str | None = None   # tests only

class CaptureBundle(BaseModel):
    session_id: str
    captured_at: datetime
    consent: Consent
    images: list[CaptureImage]        # 1–8

class CaptureArtifact(BaseModel):
    run_id: str
    capture: CaptureBundle
    capture_dir: str                  # validated local folder; images remain here
    backend: Literal["mock", "serpapi", "facecheck", "rekognition"]
    scenario: str                     # mock scenario; "default" for live search
    llm_mode: Literal["off", "anthropic", "bedrock"]
    jev_mode: Literal["fake", "typesafe", "openrouter", "off"]
    llm_model: str | None = None
    jev_model: str | None = None

class UploadRecord(BaseModel):
    bucket: str
    key: str
    version_id: str | None = None
    deleted: bool = False

class UploadsManifest(BaseModel):
    run_id: str
    objects: list[UploadRecord]
```

### 0.5 Run artifacts

Each run gets a folder `runs/<run_id>/` containing:

- `00_capture.json` (`CaptureArtifact`, not the bare manifest)
- `01_raw_evidence.json`
- `02_profiles.json`
- `03_verification.json`
- `04_final.json`
- `run_log.jsonl`: stage, backend, endpoint path without query, status, latency, retry count, and fallback/error code. Jev also records returned model and usage. No raw prompts, evidence, authorization headers, or signed URLs.
- `trace.jsonl` (`trace.py`): the debug record, appended by every stage the CLI runs. It holds each stage's start and end (settings, duration, outcome, and the exception if the stage raised one), every search and page fetch with its URL, and each Strands agent's prompts, messages (text, reasoning, tool calls and results, structured output), model and tool calls, and usage. It also holds Jev requests, answers, and HTTP error bodies, and every error a stage turned into a fallback or a code, with its exception chain. That chain includes causes hidden by `raise ... from None`. Library log records at INFO and above are kept too. The trace holds evidence, so it stays in the run folder and `purge` deletes it. Every value goes through the `run_log.jsonl` scrubber, which redacts keys, tokens, signatures, URL query strings, and tunnel URLs.
- `cache/facecheck/`: converted FaceCheck results (URLs and scores, no thumbnails) for this run only, keyed by image hash and demo mode.
- `cache/rekognition/`: V3's sanitized SerpApi results per engine, keyed by image hash and engine, and its final face-matched results, keyed by image hash, engines, and threshold.
- `cache/serpapi/`: sanitized successful responses for this run only, keyed by image hash plus engine and tab. Redact submitted image URLs and embedded signed URLs. `--refresh` bypasses this local cache; it does not force a new provider search.
- `uploads.json` (`UploadsManifest`): bucket, random object key, optional version ID, and deletion status. Record each key before upload so crash cleanup can retry. Never store presigned URLs here.

`run_id` is `<session_id>-<8 random hex chars>`; create the folder exclusively and retry on collision. Write artifacts atomically. Reject mismatched run IDs, missing prerequisites, and malformed artifacts. A stage rerun invalidates later outputs first. Use the same schema and validation rules for tool and disk outputs; constrain probabilities to 0–1, trust to 0–100, and depth to 0 or 1. Use `Field(default_factory=...)` for lists and dicts in the implementation. No field may contain NaN or infinity.

Contract snippets omit imports and validators. Use postponed annotations, `BaseModel`/`Field`, `datetime`, and `Literal`/`Protocol`. Require strict consent booleans and timezone-aware consent/capture timestamps. Check references between artifacts: account IDs, profile IDs, graph keys, accepted ID, and source URLs must exist. `NoMatch.reason` cannot be `accepted`.

### 0.6 Done when

- [x] `python3 -m pipeline run --capture tests/fixtures/captures/alex --until 0 --mock` validates the manifest and writes `00_capture.json`.
- [x] The same command refuses a manifest with `"confirmed": false`, or with no `consent` block.
- [x] Full artifacts for each stage round-trip through `contracts.py`; snippets marked abridged are illustrative only.
- [x] Missing external consent, duplicate IDs, escaping paths, and mock fixture IDs in live mode fail before any call. `purge` remains available after consent withdrawal.
- [x] Ignore rules cover captures, artifacts, caches, and traces before a real run.

## 4. Section 1: Per-image search and scrape → raw structured data

**Goal:** for each face image, find the pages it appears on and scrape them. Hand back one merged, deduplicated bundle of raw structured data that records which image found what.

**Diagram:** AWS Strands Agent, Reverse-image search tool, "Finds internet presence", raw structured data.

**In:** `00_capture.json`. **Out:** `01_raw_evidence.json` (`RawEvidenceBundle`).

**Built** (2026-10-10): `pipeline/platforms.py`, `scrape.py`, `search/` (`base.py`, `mock_backend.py`, `serpapi_backend.py`, later `facecheck_backend.py` and `rekognition_backend.py`), `hosting.py`, `llm.py`, `stage1_collect.py` (store, collectors, merge, `run_stage1`, `STAGE_RUNNERS[1]`), and `image_agent.py`. Tests: `tests/test_platforms.py`, `test_scrape.py`, `test_search.py`, `test_stage1.py`, `test_image_agent.py`, `test_serpapi_backend.py`, `test_facecheck_backend.py`, `test_rekognition_backend.py`, `test_hosting.py`, `test_llm.py`, and the stage-1 cases in `test_cli.py`. Every mock target in 1.8 and the Section 1 column of the matrix (9) pass against the real collector. Not yet done: a live smoke-search on a consenting teammate, which needs an S3 bucket, and the optional paid agent check. These choices fill gaps in 1.1–1.7. They are implementation defaults, not decisions:

- **Identity.** `normalize_url` also drops `www.`, maps `twitter.com` and the mobile hosts of known platforms to their main host, and strips trailing slashes. A redirect keeps the requested URL as the page key; `final_url` and `fetched_url` record where it landed.
- **Provenance.** Each page keeps one `FoundVia` per image: its best route, a hit before a followed link and then the lowest rank. A depth-1 page's parent is the best-ranked depth-0 profile that links to it, or depth-0 post that it wrote.
- **Post authors** (added 2026-10-11). A depth-0 post leads to one depth-1 target: its author's account. `scrape.post_authors` lists the candidates, most trusted first: the handle in the post URL, the handle in the post URL the page gives for itself (`og:url`, then canonical) on the same platform, then account links labeled "Author". Only the first is followed. A post that failed to load still leads to the handle in its URL. `instagram.com/p/<id>` names no author, but Instagram's `og:url` (`instagram.com/<handle>/reel/<id>`) does, so `instagram.com/<handle>/(p|reel|reels|tv)/<id>` is a post pattern too. Handles that are site paths (`x.com/i/status/<id>`) name no one.
- **Snippet records.** A failed direct hit keeps a snippet record only when the hit has a title or snippet. Otherwise only the `FetchError` remains.
- **Agent triage.** Pages the agent fetched but didn't judge count as retained, because fetching them was its choice. `relevant=false` excludes a page, and the exclusion is noted as `img-N excluded: <note>`. An agent that returns without searching falls back (`no_search`).
- **Bookkeeping.** `search_extras` carries only `status`, `total_results`, `skipped_hits` (unusable result links), `budget_skipped`, `fallback_reason`, and `collection_error`. `run_log.jsonl` names endpoint paths only: `/api/reverse-image` and `/fetch` for the mock, and `/search.json` and `page-fetch` for live runs. Live page URLs are evidence, so `run_log.jsonl` never names them; only `trace.jsonl`, which holds evidence like the artifacts, does.
- **Live fetch.** HTML over 1 MB is truncated, not rejected. LinkedIn's non-standard 999 status counts as `forbidden`. Final URLs on login paths (`/login`, `/authwall`, `/accounts/login`, `/checkpoint`, ...) are `login_wall`. The SerpApi search gets 20 s instead of the 10 s in 1.7, because Yandex searches can take several seconds and a timed-out search may still use a credit. Page fetches keep 10 s.

### 1.1 Search backends: mock for tests, SerpApi for real runs

Both backends implement the same interface. Stage code never checks which one it has.

```python
class SearchBackend(Protocol):
    name: str                                             # "mock" or "serpapi"
    async def reverse_image(self, image: CaptureImage) -> dict: ...
    async def fetch(self, url: str) -> FetchedPage: ...

class FetchedPage(BaseModel):
    requested_url: str
    final_url: str                 # normalized original page identity
    fetched_url: str               # local_url in mock; response URL in live
    status_code: int
    content_type: str
    html: str
```

The backend owns fixture lookup or image hosting and cleanup. The pipeline uses normalized original page URLs. Map mock fetch dictionary keys `url`, `local_url`, `status_code`, `content_type`, and `html` into `FetchedPage`. Catch `MockHTTPError` using `.status` and `.headers`; ignore any HTML in its `.payload`. HTTP errors carry status and `Retry-After` to the collector; they do not return a successful `FetchedPage`.

| | Mock (`--mock`; tests) | SerpApi (default) |
|---|---|---|
| Image reference | bare fixture ID, e.g. `alex-front` | presigned S3 URL from `hosting.py` |
| Reverse search | `client.reverse_image(image.mock_fixture, scenario=scenario, delay_ms=delay)` | `GET https://serpapi.com/search.json` with `engine=yandex_images`, `url`, `tab=about`, `api_key` |
| Page fetch | `client.fetch(original_url, scenario=scenario)` uses loopback HTTP; the server reads SQLite | async HTTP client, 10 s timeout, identifiable User-Agent, no cookies |
| Failure tests | adapter passes `MOCK_SCENARIO`; `MOCK_DELAY_MS` delays searches only | injected adapter failures; optional live smoke |
| Cost | no search credits | budget one successful uncached search per image; retries can add requests |

Rules:

- The mock uses a subset of the Yandex result shape. Normalize `image_results`; default missing title/source/snippet to empty strings and assign rank from array order when `position` is absent. Accept a thumbnail string or an object with `link`. Missing `image_results` with successful search status means no page hits. A provider `error` or failed search status is a failure, even on HTTP 200. Do not silently treat `similar_images` as matching pages. See the [Yandex reverse-image contract](https://serpapi.com/yandex-reverse-image-api).
- `reverse_image("alex-front")` is the mock client API. `reverse_image("mock://alex-front")` fails because that method sends `image_id`. The `mock://` form belongs in the `url` parameter of `/search.json?engine=yandex_images`, not in `image_id`.
- In mock mode, `mock_original_url` is the page identity. Normalize `/web/<host>/<path>` links only when their origin exactly matches the configured mock origin, including its port. This covers tests on an ephemeral port. Do not hard-code port 8765 or unwrap arbitrary live URLs. Keep a fetch URL separately.
- Live normalization removes fragments and known tracking parameters, preserves meaningful query parameters and case-sensitive paths, and resolves relative links. Use canonical hints only after validation; keep source URLs and merged provenance. Do not collapse different account URLs based only on an untrusted canonical tag.
- Cache only successful search responses inside the run (0.5). Replaying Section 2 onward needs no search cache. Defer shared caches across participants/runs; they complicate consent and purge.
- Normal tests block all non-loopback sockets as well as selecting mock/fake/off. Real runs use SerpApi unless `--mock` is given; real models require `--models`. Starting the mock alone does not redirect another client's traffic.
- Smoke-search uses a consent-bearing capture: `python3 -m pipeline smoke-search --capture captures/<session_id> --image-id img-1`. It writes a run and cleanup record, uploads one photo, queries `tab=about`, and prints hits. `about` is already Yandex's default; do not pay to compare it with itself. The free plan currently lists 250 searches/month and 50/hour; check the account dashboard before the demo. Quota reporting is optional and uses the separate Account API; allowlist counters because its response includes the key. See [pricing](https://serpapi.com/pricing) and [Account API](https://serpapi.com/account-api).

**FaceCheck backend (V2, `facecheck_backend.py`; the default on 2026-10-10 until V3).** A live Yandex run on Johnny's own selfie (run `johnny-1-25669bcf`) returned 102 results, all lookalikes: Yandex's `about` tab matches overall appearance, not identity. FaceCheck.ID matches the face against its own index of faces from public pages ([API](https://facecheck.id/en/Face-Search/API), [OpenAPI](https://facecheck.id/swagger/v1/swagger.json)). Built as follows:

- `POST https://facecheck.id/api/upload_pic` (multipart field `images`) returns `id_search`; `POST /api/search` with `{id_search, with_progress: true, status_only: false, demo}` is polled every 2 s until `output` arrives, for at most 180 s. The token goes only in the `Authorization` header. Errors arrive on HTTP 200 with `error` and `code`: token problems map to `unauthorized`, credit problems to `no_credits`, the rest to `provider_error`.
- The upload is a JPEG copy at most 1,600 px on its longest side, EXIF stripped (`hosting.search_copy`). No hosting or tunnel is needed.
- Output items (`score`, `url.value`, `base64`) become the Yandex-shaped payload `parse_image_results` reads: best score first, title empty, source the domain, snippet `FaceCheck face match 92/100 (certain match)`, and `face_score` on each `ImageHit`. Thumbnails are dropped because they show other people's faces. `search_extras` records `demo` and `searched_faces`.
- Cost: 3 credits ($0.30) per search, one per image. `FACECHECK_DEMO=true` is free but scans only the first 100,000 faces. Before the first paid search of a run, `build_facecheck_backend` calls the free `/api/info` and stops with a configuration error if `has_credits_to_search` is false. A retry polls the image's existing `id_search` instead of uploading again, so it can't start a second paid search. A timeout is not retried.
- Results are cached under `cache/facecheck/` (0.5); demo results are cached separately and never reused for a real search.
- The image deadline becomes 35 s plus 180 s (`extra_deadline_s`), because FaceCheck can queue a search.
- The per-image agent sees `face_score` on each hit and is told FaceCheck's bands (90+ certain, 83–89 confident, 70–82 uncertain, under 70 weak), to prefer high scores, and to treat hits under 70 as likely lookalikes.
- Not yet checked live: the polling behavior, error codes beyond the token errors, and the result quality. The first paid run on Johnny's photo is the check.

**Backend V3 (default since 2026-10-10, `rekognition_backend.py`).** Johnny asked for a version that searches the open web through SerpApi and checks faces with Rekognition, instead of looking faces up in FaceCheck's own database. Yandex alone finds lookalikes (above); V3 keeps Yandex's recall, adds Google Lens, and lets Rekognition decide which results show the same face. Built as follows:

- **Face first.** `DetectFaces` runs on a JPEG copy at most 1,600 px on its longest side, EXIF stripped and upright. The largest face is the participant's. An image with no face fails as `no_face` before any SerpApi search or upload.
- **Search the crop.** The face box, padded by half its width and height on each side and clipped to the image, is cut out as a JPEG. Only this crop is hosted (tunnel or S3, 1.2) and searched, once per engine in `REKOGNITION_ENGINES`: `yandex_images` with `tab=about` and `google_lens`, in parallel. If one engine fails, the other's results are used and `failed_engines` names it. If all fail, the search fails, and a retry searches again, without a second `DetectFaces` or upload. SerpApi's "hasn't returned any results" counts as zero results.
- **Merge.** Results are merged by normalized page URL, alternating engines by rank, and the first 40 are compared. Each keeps every engine's full-size image URL (`original_image.link` for Yandex, `image` for Lens) ahead of the thumbnails.
- **Compare.** Each result image is downloaded with the guard page fetches use: public addresses only, every redirect hop checked, at most 8 MB in 8 s, `image/*` content. It is shrunk to at most 1,600 px and sent to `CompareFaces` with the crop as the source and `SimilarityThreshold=0`, so every face in it gets a similarity. A failed download, an unsupported image, or an image with no face falls back to the next URL, usually the thumbnail; the result counts as `no_face` only if none of its images that loaded shows a face. `face_score` is the highest similarity in the first image with a face, rounded down. Eight comparisons run at once, and comparing stops after 45 s.
- **Filter.** Results under `REKOGNITION_MIN_SIMILARITY` (default 80, CompareFaces' own default) and images without a face are dropped. The rest become the Yandex-shaped payload `parse_image_results` reads, ranked by similarity, with each engine's title, snippet, and thumbnail kept. `search_extras` records `engines`, `failed_engines`, `min_similarity`, `candidates`, and `outcomes`, which counts `matched`, `below_threshold`, `no_face`, `no_image`, `image_unavailable`, `compare_error`, `not_compared`, `over_limit`, and `out_of_scope`.
- **V3_1 (2026-10-11).** V3 with `REKOGNITION_PLATFORMS=linkedin,facebook`. Same engines and costs, but after the merge, results whose page isn't on a listed platform (`platform_for`) are dropped as `out_of_scope` before any download or comparison, so the 40-result cap counts only LinkedIn and Facebook results. `search_extras.platforms` records the list, and the final-payload cache key includes it. Account links followed from the kept pages in stage 1 are not limited. First run, `groupmate-2-808787c9`: 177 merged results, 170 out of scope, 7 compared, none at 80 or above (best 71.66, the same LinkedIn profile V3 ranked first).
- **Errors.** Rekognition error codes map to the codes in 1.7: `AccessDeniedException` and credential errors become `unauthorized` and stop the image's search at once, throttling becomes `rate_limited`, and `InvalidParameterException` from `CompareFaces` means the image has no face.
- **Cache.** Each engine's sanitized reply is cached under `cache/rekognition/`, so a rerun or a new threshold makes no new SerpApi search. The final payload is cached only when every engine answered and every result was compared. `--refresh` skips both.
- **Setup.** `SERPAPI_KEY`, `AWS_REGION`, and AWS credentials with `rekognition:DetectFaces` and `rekognition:CompareFaces`. `run` and `smoke-search` look the credentials up (no AWS request) before creating a run folder, and `build_rekognition_backend` does again before opening the tunnel. The image deadline becomes 35 s plus 75 s (`extra_deadline_s`).
- **Cost.** One SerpApi search per engine per image, and about $0.001 per Rekognition image: one `DetectFaces` and up to 40 `CompareFaces` per capture image, about $0.12 for X = 3, plus one more `CompareFaces` for each result image that shows no face.
- **Agent.** The per-image agent sees `face_score` and V3's bands: 99+ strong, 95–98 likely, 90–94 possible, lower weak. These bands are ours, not AWS's. AWS recommends 99 or higher for public-safety uses, says even 99 doesn't guarantee a match, and asks for human review ([thresholds](https://docs.aws.amazon.com/rekognition/latest/dg/thresholds-collections.html), [public safety](https://docs.aws.amazon.com/rekognition/latest/dg/considerations-public-safety-use-cases.html)).
- **Privacy.** Unless the AWS account opts out with an AWS Organizations AI services opt-out policy, AWS may keep the analyzed images and use them to improve its services ([Rekognition FAQ](https://aws.amazon.com/rekognition/faqs/)). Consent must cover that.
- **Checked live 2026-10-10 (run `johnny-1-8c9c0190`, Johnny's own photo):** Johnny's account is a Free-plan "project" from AWS's new sign-up. Its sandbox SCP allows Rekognition only in us-east-2 (us-east-1 and us-west-2 are denied), so `AWS_REGION=us-east-2`. DetectFaces found 2 faces and kept the largest. Yandex returned 104 results and Google Lens 60; of the first 40 compared, 39 scored under 80 (best 56.6) and 1 had no face, so V3 returned no hits, where V1 had passed lookalikes on. Stage 1 took 38 s. Still unknown: whether comparing all 164 results, or more images per person, finds the participant's own pages.
- **Trace engine (added 2026-10-10, `trace_face.py`).** The live runs above showed that Yandex and Lens can't find a person from a new photo, and Johnny ruled out FaceCheck, so V3 got Trace's face index ([API](https://traceaifacescan.app/ai-face-scan-api/), [OpenAPI](https://traceaifacescan.app/openapi.json)) as a third engine, on by default. The crop is uploaded (`POST /api/v1/scans`, multipart `image`, at least 200 px; the upright copy goes instead when the crop is smaller) with an Idempotency-Key derived from the capture image's hash, then `GET /scans/{id}` is polled every 3 s for at most 300 s (Trace says 25-45 s; the first live scan was at 87% after 90 s, timed out under the old 90 s limit, and cost a credit). A locked scan (no credits when made) is revealed with `POST /scans/{id}/reveal`; 402 becomes `no_credits`. Problem-document codes and failed-scan `error_code`s map to the codes in 1.7. Each match's page URL, platform, handle and score become a result titled `<handle> on <platform>` with the snippet `Trace face match 96/100 (near certain)`. Rekognition compares the crop with the match's preview (`/scans/{id}/previews/{index}`, fetched with the key, which is sent only to that path). Trace's matches are merged first and every one is compared; the 40-result cap applies only to the SerpApi results (until 2026-10-11 it covered Trace too, and the first groupmate run compared only 40 of Trace's 50 matches). `release()` deletes a finished scan (`DELETE /scans/{id}`), so Trace replies aren't cached and a rerun pays again. A scan that timed out is left running; a rerun of the same photo within 24 hours sends the same key, gets the same scan back, and pays nothing. With Trace on, the image deadline is 35 s plus 355 s. Cost: one credit per image, in packs of 5 for $4, 40 for $22, or 100 for $50, by card. `set-key trace` checks the key with the free `GET /account` (a bad key gets 401 `unauthenticated`, checked live 2026-10-10) and prints only credit counts. Not yet checked live: a paid search, its result quality, and the preview format.

### 1.2 Image hosting (live only; default changed to a tunnel, see below)

SerpApi's Yandex and Google Lens engines require an image `url`; they document no file upload parameter. V3 hosts only the face crop, the same way. The URL must be fetchable without our credentials. **Unverified integration:** whether Yandex can fetch this team's presigned S3 URLs. Test that early. `hosting.py`'s S3 route does the following:

1. Uploads each image to a private S3 bucket under a random key.
2. Returns a presigned GET URL that is valid for 10 minutes.
3. Deletes the object in `finally` after the search and bounded retries. `purge` retries any failed deletion from `uploads.json`.

Use a dedicated bucket/prefix with public listing disabled and a cleanup lifecycle rule. Signed-URL expiry does not delete the object. Prefer no versioning for these temporary objects; otherwise track and delete the uploaded version too. If hosting or search fails, return an image error and use the mock demo. Do not quietly switch to permanent public hosting.

Mock mode skips hosting.

Built as specified in `hosting.py` as `IMAGE_HOST=s3`. It reads `IMAGE_BUCKET` and `AWS_REGION` from `.env`, with credentials from the standard AWS chain. `run` and `smoke-search` refuse to start a SerpApi run without them, before creating a run folder. Keys look like `hackmidwest-faces/<32 hex>.jpg`. `purge` deletes leftovers when `AWS_REGION` is set. Still unverified: whether Yandex can fetch the presigned URLs. Johnny's AWS organization denies S3 by SCP, so this route can't run in his account.

**Default since 2026-10-10: `IMAGE_HOST=tunnel`** (`TunnelImageHost` in `hosting.py`). Johnny asked for real-internet search, and S3 is blocked. A `ThreadingHTTPServer` on 127.0.0.1 serves only registered random paths. `ssh -T -R 80:127.0.0.1:<port> nokey@localhost.run` (BatchMode, no key offered, host key pinned on first use) publishes it as `https://<random>.lhr.life`. `build_serpapi_backend` opens the tunnel and checks it end to end with a throwaway file before any image's 35 s deadline starts. A failure there is a configuration error, and no search is made. Each image is served as a JPEG copy at most 800 px on its longest side, with EXIF stripped, at `/<32 hex>.jpg`. The path is removed by `release()`, and the tunnel and server stop in `aclose()`. Nothing is stored remotely, so `uploads.json` stays empty. `run` and `smoke-search` need only `ssh` on PATH. Checked live on 2026-10-10 with a landmark photo, not a person:
- Yandex refuses `*.trycloudflare.com` (Cloudflare quick tunnel) URLs without fetching them.
- Yandex fetches `lhr.life` URLs (`YandexImageResizer/2.0`).
- A full-size 270 KB photo took 10–13 s through the free tunnel, and Yandex answered "not publicly accessible". The 800 px copy (about 60 KB) returned 50–94 results in 3–5 s.
- The tunnel opened and passed its check in about 1.5 s.

### 1.3 Per-image agent

With `--models` and `LLM_MODE` set to a real provider, each image gets its own fresh Strands `Agent` and isolated tools. **Proposal:** run at most three images concurrently. The model-free path calls the same collector directly and constructs no agents; it tests plumbing, not the decided agent topology.

```python
results = await asyncio.gather(
    *(collect_for_image(img, backend, page_store) for img in capture.images),
    return_exceptions=True,
)
```

`collect_for_image` acquires an `asyncio.Semaphore(3)`. Use `asyncio.to_thread` around the synchronous mock client; use async HTTP for live calls. A failed image becomes an `ImageEvidence` with an error entry. It never stops the other images. `return_exceptions=True` still needs explicit conversion of exceptions into those records.

The tools are built per image with closures, so each agent can touch only its own image:

| Tool | What it does | Limit |
|---|---|---|
| `reverse_image_search()` | Runs the reverse search on this agent's image. It takes no arguments. Repeated tool calls return the stored result or error. | 1 logical search; bounded retries |
| `fetch_page(url)` | Fetches and scrapes a page into a `PageRecord` and stores it in the shared page store. Returns a short summary: platform, kind, title, display name, first 300 characters, and outbound account links. | 12 fetches per image |
| `follow_account_links(url)` | Fetches account links from a profile found directly in search, or the author's account of a post found directly in search. Does not follow links from depth-1 pages or articles. | shares the 12 |

Tools enforce limits in code. `fetch_page` accepts only this image's search hits or permitted depth-1 targets. A per-URL future/task in the shared store deduplicates in-flight fetches, including failures; do not hold a global lock during network I/O. Every requesting image adds its own provenance even on a cache hit. The per-image budget counts distinct requested URLs, including failures and shared results. Search hits consume the budget first, in rank order; followed targets then use normalized URL order.

Live fetches accept only HTTP(S) public destinations. Block local/private/link-local addresses, credentials in URLs, and unsafe redirect targets. Bound redirects to three and HTML to 1 MB. Enforce destination checks at connection time too. No browser automation, login, or generic network/shell tool is needed.

The agent's job is triage. Real Yandex results are noisy (lookalikes, stock photos, Pinterest boards). The agent decides three things:

- which hits look like pages about this person,
- which of those to fetch,
- which profiles' account links, and which posts' authors, to follow.

Its structured output contains only those judgments:

```python
class PageJudgment(BaseModel):
    url: str
    relevant: bool
    note: str = ""          # e.g. "public profile returned by image search"

class ImageTriage(BaseModel):
    judgments: list[PageJudgment]
```

Code builds `ImageEvidence` from the page store plus these judgments. Keep judgments only for that image's collected URLs, including retained search snippets. A page is globally relevant if any image retained it; only those images contribute scoring provenance. Preserve excluded observations for debugging without counting them as support.

The system prompt tells the agent:

- It owns the search for one consenting participant's image. It receives search evidence, not pixels, and must not claim to have compared faces.
- Run the search once.
- Fetch the hits most likely to be this person's own profiles or posts, or articles about them.
- Follow account links from profiles, and posts to their authors.
- Skip shopping, stock, and lookalike pages.
- Stop when the budget is spent or nothing useful is left.

**Deterministic path** (`LLM_MODE=off`, which tests use):

1. Search.
2. Fetch every hit in rank order, up to the budget.
3. Follow account links from depth-0 profile pages, and depth-0 posts to their author's account, once.
4. Keep successful HTML and search-snippet-only records as relevant. Snippets never supply graph edges or justify bypassing a 403/login wall.

This gives stable retrieval targets. LLM triage may differ; optional model tests measure that separately.

Strands notes, checked against the [tools guide](https://strandsagents.com/docs/user-guide/sdk/tools/), [Python quickstart](https://strandsagents.com/docs/user-guide/sdk/quickstart/python/), and [structured output guide](https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/):

- Call `await agent.invoke_async(prompt, structured_output_model=ImageTriage)` and read `result.structured_output`. The older `agent.structured_output()` method is deprecated.
- Import `Agent, tool` from `strands`; decorate typed tool functions with `@tool`. Pass `AnthropicModel(model_id=..., max_tokens=...)` from `strands.models.anthropic` to `Agent(model=..., tools=[...], callback_handler=None)`. For Bedrock, use `BedrockModel` from `strands.models` with its model ID and region. Plain `Agent()` uses Bedrock; never construct it in offline mode. See [Anthropic provider](https://strandsagents.com/docs/user-guide/sdk/model-providers/anthropic/) and [Bedrock provider](https://strandsagents.com/docs/user-guide/sdk/model-providers/amazon-bedrock/).
- Use strict field validation and bounded output lists. Catch `StructuredOutputException`, validation errors, and timeouts. Allow one correction, then continue with deterministic triage over stored results; never repeat a completed search. A per-image deadline bounds both agent work and retries.
- **Unverified:** the original draft cites SDK issue [#4482](https://github.com/strands-agents/sdk-python/issues/4482) for unbounded structured-output retries. The issue was not accessible during review. Keep timeouts regardless; do not depend on that report or weaken schemas to avoid it.
- The isolated search-delay test in 1.8 checks adapter concurrency. Also test agent tool I/O with a stub model; the isolated test alone cannot catch a blocking agent loop.

**Stretch:** the diagram labels the tool "Search API (GPT or Claude)". A text-search tool could take a name found by the image search and look for more of that person's accounts, through the mock's `search()` or a web search tool. It's off by default because it pulls in strangers with the same name, which is exactly the two-Alex case in the mock.

### 1.4 Scraping a page (`scrape.py`)

From each fetched page, extract:

- the title, meta description, and Open Graph tags
- every JSON-LD block:
  - Person: `name`, `description`, `worksFor`, `homeLocation`, `sameAs`
  - Article: `headline`, `author`, `datePublished`
- the canonical link and `<time datetime>`
- outbound links with their labels
- the visible text, cut to 4,000 characters

Then classify the page.

**`platform`**, from the domain table in `platforms.py`:

| Platform | Live domains | Mock domain |
|---|---|---|
| `linkedin` | linkedin.com | linkup.test |
| `x` | x.com, twitter.com | chatter.test |
| `instagram` | instagram.com | photogram.test |
| `github` | github.com | codeforge.test |
| `facebook` | facebook.com | none |
| `blog` | medium.com, substack.com, personal sites | fieldnotes.test |
| `news` / `other` | everything else | riverbend-news.test, archive.test, syndicated.test, … |

**`page_kind`**:

- `post` first if the full path matches a post pattern, such as `chatter.test/<handle>/status/<id>`.
- `profile` if the full path matches an account pattern, such as `linkedin.com/in/<handle>`, `chatter.test/<handle>`, or `fieldnotes.test/@<handle>`. Anchor patterns so `/old-project` and `/private-album` do not become accounts. A top-level JSON-LD `Person` can support an otherwise unknown profile URL; an Article's nested Person author cannot.
- `article` otherwise, including the archived speaker bio, mirror, deleted project, and private album in the mock.

**Live caveat.** Pages may return a login wall, scripts, or an error. Retain only the search title/snippet with `extraction="snippet"`; discard error-page bodies and links, including HTML exposed in a mock error payload. Do not infer account ownership from access denial. If there was no search snippet, keep only the fetch error. Read names from profile titles only when the URL is an account URL.

### 1.5 Merging across images (**Proposal**)

Merge before Jev. Key pages by the validated canonical URL or normalized original URL. The mock mirror merges into the fetched Riverbend interview. Prefer the canonical page's content and links, retain both source URLs, and union `found_via`. Rewrite `page_urls` through the alias map; keep raw hit URLs and ranks unchanged. Repeated hits remain in `hits` but never multiply image support or link counts. Multi-angle retrieval is a heuristic signal, not independent face verification.

### 1.6 Contracts

```python
class FoundVia(BaseModel):
    image_id: str
    hit_position: int | None      # rank in that image's results; None if reached by a followed link
    depth: int                    # 0 = search hit, 1 = followed from a profile or a post's author
    parent_url: str | None = None

class LinkRef(BaseModel):
    url: str                      # normalized
    label: str = ""

class PageRecord(BaseModel):
    url: str                      # same canonical/normalized key as pages
    source_urls: list[str]         # original URLs merged into this record
    canonical_url: str | None = None
    fetched_url: str
    status_code: int | None       # retain 403/404; None if no HTTP response arrived
    platform: str
    page_kind: Literal["profile", "post", "article"]
    title: str = ""
    meta_description: str = ""
    og: dict[str, str] = {}
    json_ld: list[dict] = []
    text: str = ""
    outbound_links: list[LinkRef] = []
    published_at: str | None = None
    found_via: list[FoundVia]
    relevant: bool = True
    notes: list[str] = []         # agent notes, one per image
    extraction: Literal["html", "snippet"] = "html"

class ImageHit(BaseModel):
    position: int
    title: str
    url: str                      # normalized original URL
    fetch_url: str                # the local URL in mock mode
    source: str
    snippet: str = ""
    thumbnail_url: str | None = None
    face_score: int | None = None  # 0-100: FaceCheck's score or V3's Rekognition similarity; None for Yandex and the mock

class FetchError(BaseModel):
    url: str | None               # None when the search itself failed
    status_code: int | None
    reason: str                   # not_found, forbidden, rate_limited, server_error, timeout, login_wall

class ImageEvidence(BaseModel):
    image_id: str
    angle: str
    backend: Literal["mock", "serpapi"]
    request_id: str | None = None # search_metadata.id; absent on request failure
    searched_at: datetime          # attempt start, including a failed search
    hits: list[ImageHit]
    page_urls: list[str]          # pages this image led to, after triage
    search_extras: dict = {}      # allowlisted metadata only; no echoed image URL/key
    collection_method: Literal["agent", "deterministic", "fallback"]
    errors: list[FetchError] = []

class RawEvidenceBundle(BaseModel):
    run_id: str
    num_images: int
    images: list[ImageEvidence]
    pages: dict[str, PageRecord]  # includes snippet-only records
    url_aliases: dict[str, str]   # original URL -> pages key, including identity mappings
```

### 1.7 Errors and retries

- **429 and transient 5xx:** at most two retries per request, inside the stage deadline. Honor `Retry-After` seconds or HTTP date; if it exceeds the remaining budget, stop and record the error. Otherwise use bounded backoff. Avoid multiplying retries across SDK and wrapper layers.
- **404 and 403 on fetch:** record the error and don't retry. The mock always returns 404 for Alex's deleted post and 403 for Sam's private album.
- **Timeouts:** each request gets 10 s. A timeout is recorded as `timeout`.
- **400, 401, invalid JSON, provider errors, and bad schemas:** report the actual error; do not retry bad configuration. Record sanitized codes such as `invalid_request`, `unauthorized`, `invalid_response`, `hosting_error`, or `budget_exhausted`. A failed search has no hits and a nullable request ID.
- **Zero hits on every image:** Section 1 still writes a valid bundle, and Sections 2–4 produce a no-match result.

### 1.8 Done when (mock)

Using `tests/fixtures/captures/alex`, whose images map to `alex-front`, `alex-left`, and `alex-right`, on the deterministic path:

- [x] 3 reverse-image requests return 13 hits total, with 10 unique URLs. That means exactly 10 fetches, because the shared page store prevents duplicates.
- [x] The 10 fetches yield 9 successful HTML responses and one 404. Canonical merging leaves 8 HTML records plus one snippet-only 404 record: 9 `PageRecord`s total.
- [x] There is one 404 `FetchError` for `fieldnotes.test/@alexbuilds/old-project`, and the run continues.
- [x] There are 5 profile pages (Alex's LinkUp, Chatter, Photogram, CodeForge, and FieldNotes), each with `found_via` filled in. `chatter.test/alexbuilds` lists `img-1` and `img-2` at depth 0, and `img-3` at depth 1 as the author of its Chatter post.

Other cases:

- [x] `tests/fixtures/captures/sam` (`sam-front`): following account links adds Sam's CodeForge and FieldNotes profiles at depth 1, and the private album records a 403.
- [x] `MOCK_SCENARIO=rate_limited`: each image retries as `Retry-After` says, then ends with a `rate_limited` error. No exception escapes.
- [x] `MOCK_DELAY_MS=1000`: isolate the three search calls; their intervals overlap and elapsed time is below 2.5 s on an idle local machine. Do not apply this bound to all scraping or model work.
- [x] `unknown` fixture: no hits and a valid bundle.
- [x] A stub-model test creates X distinct agents for X images and verifies isolated image tools, shared fetch deduplication, and failure fallback without external calls.
- [ ] Optional, costs LLM calls (`--models`, `LLM_MODE=anthropic`): on `alex`, check that the agent path retains the 5 expected profiles. Record differences instead of treating model triage as deterministic.

**Live check:** record whether a consenting teammate's photo returns a page they confirm is theirs. A clear zero-hit result is valid behavior, but it does not pass retrieval-quality validation. If fresh camera images do not retrieve known accounts, demo the mock and flag the live limitation; do not retune confidence to manufacture a match.

## 5. Section 2: Parse, build profiles, connection graph (Jev)

**Goal:** turn raw pages into accounts ("LinkedIn: John Doe"), group the accounts into candidate people, and build a link graph with trust values for each candidate.

**Diagram:** Typesafe Jev, "Parses name and general bio", Builds Structured Profiles, Graph.

**In:** `01_raw_evidence.json`. **Out:** `02_profiles.json` (`ProfilesBundle`).

**Where Jev fits (Proposal).** Jev answers typed questions with probabilities and can't return text, so it can't read a name off a page. Extraction (2a) uses the structured data the scraper already pulled out, with the LLM as a fallback. Jev makes the grouping decisions in 2b: do these two accounts belong to the same person?

### 2.0 Jev adapter

Implement the route matching Johnny's key first:

| Route | Request | Model |
|---|---|---|
| TypeSafe | `TypeSafeClient` / `AsyncTypeSafeClient` from `typesafe_sdk`; `system_one(state=..., questions=...)`. REST: `POST https://api.typesafe.ai/v1/systemone` | `jev-latest`; log the returned version |
| OpenRouter | `POST https://openrouter.ai/api/alpha/decisions` with Bearer key and JSON `model`, `state`, `questions` | `typesafe/jev-1.13`; alias `~typesafe/jev-latest` |

Use `Noul`, `Choice`, and `Score` from `typesafe_sdk` for SDK questions. The pinned `typesafe-sdk==0.7.4` returns `SystemOneResponse` with only `model`, `usage`, and `answers`. `answers[key]` is discriminated by `type`: `NoulAnswer.noul`; `ChoiceAnswer.choice`, `.confidence`, `.probabilities`; `ScoreAnswer.score`, `.confidence`, `.probabilities`, `.legend`. The docs' `result.nouls[key]`-style accessors do not exist in this version (checked 2026-10-10). TypeSafe documents OpenRouter configuration with `base_url="https://openrouter.ai/api"` and its route-specific key/model. Direct HTTP to Decisions is a small fallback if SDK routing differs. Do not send Jev to a chat-completions endpoint or use it as the Strands text model. See [TypeSafe quickstart](https://docs.typesafe.ai/introduction/quickstart), [SDK usage](https://docs.typesafe.ai/sdk/python/usage), and [OpenRouter Jev guide](https://openrouter.ai/docs/guides/community/jev).

Normalize both routes to `JevCall` (2d). Validate question IDs, types, probability bounds, and choices before using answers. `noul` is itself a probability, with no `confidence` field. `choice.criteria` maps option IDs (including `none`) to descriptions; use `probabilities[choice]`, not `choice.confidence`. `score.criteria` is an ordered list; its answer is a possibly fractional zero-based level. See the [Decisions schema](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request).

Catch transport, schema, and API errors. The SDK documents `TypeSafeAPIError.status` and `.request_id`, and `TypeSafeRateLimitError.retry_after_ms` in its [exception reference](https://docs.typesafe.ai/sdk/python/api/exceptions). Set one retry budget across wrapper and SDK; fall back after the stage deadline. Keep input summaries well below the documented 32K context; do not send whole scraped pages.

### 2a. Extract accounts

For each relevant `profile` page, build an `Account`. Use these sources in priority order:

1. JSON-LD `Person`: `name`, `description`, `worksFor`, `homeLocation`, `sameAs`.
2. Open Graph and meta description.
3. Title patterns, such as `"<name> | <site>"` and `"<name> - <role> - <company> | LinkedIn"`.
4. Keep the URL handle separately. Do not present it as an extracted name.
5. LLM fallback, only when models are enabled: one batched extraction call for incomplete accounts. Use `AccountExtraction` (2d), with the page URL and at most 2,000 characters per page. Unknown fields stay null. Reject invented URLs and unsupported values; use partial deterministic fields on failure.

For the mock, Person `worksFor.name` supplies workplace and `homeLocation` is a string. There is no JSON-LD `jobTitle`; extract role from the visible `<role> at <workplace>.` sentence. `<time datetime>` supplies `updated_at`. The live parser also accepts object-valued locations and `jobTitle`. Read these from HTML, not SQLite ground-truth columns. Each Account's facts cite its own page URL.

Posts and articles become `Mention`s:

- A post is tied to its author's account through the first collected account among `scrape.post_authors`: the handle in its URL, the handle in the post URL it gives for itself (`og:url` or canonical), or its "Author" link.
- An article keeps names from explicit author metadata or labeled account links and all account URLs it links to. Set `subject_account_ids` only for an explicit post author or an account whose extracted full display name appears in the article title. If the title could refer to multiple linked accounts, leave attribution empty.

Mentions don't form profiles. The only crawling a mention triggers is a depth-0 post's author account (1.3). Only `subject_account_ids` carry image support. Riverbend's title names Alex, so its interview supports Alex's LinkUp account, not Sam's merely linked account. The archived bio has no account link and creates no account. Ambiguous or unlinked mentions remain evidence without attribution. Do not transfer image support through arbitrary outbound links.

For each Account, `depth` is the minimum collection depth. `image_ids` contains its retained page provenance plus attributed mention images. `direct_image_ids` excludes depth-1-only discovery. `best_hit_position` is the minimum direct rank across its page and attributed mentions, or null. Keep all PageRecord provenance intact so these fields can be recomputed.

The printable form matches the diagram:

```
LinkedIn: Alex Morgan
X: Alex M.
Instagram: Alex M.
GitHub: Alex Morgan
Blog: Alex Morgan
```

### 2b. Group accounts into candidate profiles

**Proposal:** retain at most 12 accounts, preferring direct hits by rank, then followed URLs. Record truncation. This caps pair questions at 66 and avoids an open-ended clustering system.

1. **Deterministic merges.** Join mutually linked accounts only when names/handles support the link and there is no strong conflict. A social link alone can mean a colleague. Prefer these explicit links over weak text similarity.
2. **Jev for plausible remaining pairs.** Compare pairs with an account link, matching handle, or compatible name. Contradictory current workplaces plus distinct locations without an ownership link keep a pair separate. State includes account summaries by ID; each tested pair is one `noul`:

```python
state = {
    "accounts": {
        "a1": {"platform": "linkedin", "display_name": "Alex Morgan", "handle": "alexbuilds",
               "workplace": "Prairie Atlas Labs", "location": "Kansas City, MO",
               "bio": "I build open-source transit maps ...",
               "links_to": ["https://chatter.test/alexbuilds", "https://codeforge.test/alexbuilds"],
               "image_ids": ["img-1"]},
        # ...
    }
}
questions = {
    "same_a1_a3": Noul(
        instructions="Do accounts a1 and a3 belong to the same real person?",
        criteria={"true": "Ownership links and compatible identity facts support the same person.",
                  "false": "Different people or insufficient evidence. A shared name or city alone is insufficient."}),
    # ... one per pair
}
```

3. **Join on threshold.** Start at ≥ 0.70, descending by probability with stable ID tie-breaks. Before joining components, reject a merge if any cross-component pair has a strong conflict or a real Jev result below 0.5. A fake 0.05 means no rule support, not a veto. Plain union-find without the guard can join strangers through one weak bridge. Leave unresolved accounts separate. Mock tests are regression checks, not live threshold calibration.
4. **Name the profile.** Use its most common nonempty display name. On a tie, prefer LinkedIn, then stable account ID order. Unknown names stay null. Sort account URLs to assign stable account IDs and profile IDs; never let completion order set them.

**Fallback without Jev** (`JEV_MODE=fake` or `off`). Two accounts are the same person if any of these is true:

- they link to each other with a compatible name or exact handle and no strong conflict;
- one links to the other, and the names are compatible;
- neither links to the other, the names are compatible, and they share an explicitly stated workplace. City alone is too weak.

Apply the component conflict guard to every rule. For this prototype, a strong conflict means both accounts state different workplaces and different city locations, without a link between them. Missing values are not conflicts. Include source dates in Jev's state so stale facts remain visible.

Normalize case and punctuation. First names must match, be an initial, or use an explicit alias pair such as Sam/Sammy; last names must match or be an initial. Thus Alex M./Alex Morgan, Sam Rivera/Sammy Rivera, and C. Brooks/Casey Brooks work. Do not infer arbitrary prefixes or drop arbitrary surname tokens. The photographer's "Alex Morgan Photo" joins through mutual links and exact handle `alexframes`.

In `fake` mode, `jev.py` returns rule results as synthetic 0.95/0.05 answers to exercise typed parsing. Label them `method="rule"` and `provider="fake"`; they are not measured model probabilities. `off` uses the rules directly. Fallback failures leave pairs unmerged unless rules support them.

Keep every grouping request's normalized answers in `ProfilesBundle.jev_calls`; keep verification calls in `VerificationResult.jev_calls`. Store metadata only in `run_log.jsonl`; `trace.jsonl` keeps each request and its answers for debugging.

### 2c. Connection graph and trust

The graph is built once per candidate profile, as in the diagram:

1. **Sources** are the profile's accounts with successful HTML extraction. Snippet-only accounts contribute no outgoing edges.
2. **Targets.** Use the union of outbound account links and Person `sameAs`, excluding self-links. Deduplicate by source account and target account URL; HTML plus JSON-LD is still one edge.
3. **Count.** For each target, `count` is the number of distinct sources in this profile that link to it.
4. **Trust** comes from `count` (**Proposal**, built to match the diagram's two data points; configurable in `config.py`):

| Connections | Trust |
|---|---|
| 0 | 0 |
| 1 | 50 |
| 2 | 95 |
| 3 or more | 99 |

5. **Target status.** Each target is labeled `in_profile`, `other_profile` (with that profile's ID), or `not_collected`.

Set `cross_profile_link` when a target in another profile has trust ≥ 95. This is evidence to inspect, not proof of common ownership. The diagram does not establish that its Facebook target is Johnny Appleseed. Do not merge profiles on this flag. Count per account URL; group rows under platform labels for display, without combining different accounts on the same platform.

Two profile-level numbers feed Section 3:

- `mean_link_trust`: the mean trust over the profile's own accounts, where an account nobody links to counts as 0.
- `internal_link_density`: distinct directed internal edges divided by n(n−1); 0 for fewer than two accounts.

### 2d. Contracts

```python
class AccountFields(BaseModel):
    display_name: str | None = None
    bio: str | None = None
    role: str | None = None
    workplace: str | None = None
    location: str | None = None

class AccountExtraction(BaseModel):
    fields_by_url: dict[str, AccountFields]

class Account(BaseModel):
    account_id: str                 # stable "a1", "a2", ...; URL is identity
    platform: str
    url: str
    handle: str | None = None
    display_name: str | None = None
    bio: str | None = None
    role: str | None = None
    workplace: str | None = None
    location: str | None = None
    updated_at: str | None = None
    links_to: list[str] = []        # normalized account URLs
    image_ids: list[str] = []       # direct or followed provenance; not independent face matches
    direct_image_ids: list[str] = [] # direct search or attributed mention only
    best_hit_position: int | None = None
    depth: int
    extraction: Literal["json_ld", "meta", "title", "llm", "snippet", "partial"]

class Mention(BaseModel):
    url: str
    kind: Literal["post", "article"]
    author_account_id: str | None = None
    subject_account_ids: list[str] = []
    names: list[str] = []
    links_to: list[str] = []
    image_ids: list[str] = []
    published_at: str | None = None

class PairDecision(BaseModel):
    a: str
    b: str
    probability: float | None      # null for mutual-link/off rules; synthetic in fake mode
    method: Literal["mutual_link", "jev", "rule"]

class CandidateProfile(BaseModel):
    profile_id: str                 # "p1", "p2", ...
    name: str | None
    account_ids: list[str]
    pair_decisions: list[PairDecision]

class Connection(BaseModel):
    target_url: str
    target_platform: str
    count: int
    sources: list[str]              # account_ids
    trust: int
    target_status: Literal["in_profile", "other_profile", "not_collected"]
    target_profile_id: str | None = None

class ProfileGraph(BaseModel):
    profile_id: str
    connections: list[Connection]
    mean_link_trust: float
    internal_link_density: float
    flags: list[str] = []           # cross_profile_link

class JevCall(BaseModel):
    provider: Literal["typesafe", "openrouter", "fake"]
    model: str | None = None        # returned build; null on failure
    request_id: str | None = None
    answers: dict[str, dict] = {}   # validate each typed answer as in 2.0
    usage: dict = {}
    error: str | None = None        # sanitized code

class ProfilesBundle(BaseModel):
    run_id: str
    accounts: dict[str, Account]
    mentions: list[Mention]
    profiles: list[CandidateProfile]
    graphs: dict[str, ProfileGraph]
    jev_calls: list[JevCall]
    flags: list[str] = []           # accounts_truncated, grouping_fallback
```

### 2e. Done when (mock)

- [x] **Diagram parity** (unit test with hand-built accounts): if LinkedIn links to Facebook and GitHub, and Instagram links to Facebook and Twitter, the result is Facebook 2/95, GitHub 1/50, Twitter 1/50.
- [x] **`alex`, default:** 5 accounts, with the display names printed in 2a, and one profile "Alex Morgan" containing all 5. LinkedIn, X, GitHub, and Blog each have 2 connections (trust 95); Instagram has 0. `mean_link_trust` = 76.
- [x] **`alex`, ambiguous:** two profiles named Alex Morgan, one Kansas City and one Portland.
  - Kansas City is `alexbuilds`, with 5 accounts.
  - Portland is `alexframes`: LinkUp, Chatter, and FieldNotes, plus CodeForge at depth 1.
  - No pair across the two profiles is merged.
- [x] **`sam`:** one profile containing both "Sam Rivera" (LinkUp, CodeForge, FieldNotes) and "Sammy Rivera" (Chatter, Photogram). This is the alias case.
- [x] **`casey`:** one profile with 2 accounts, each with 1 connection (trust 50).
- [x] **Counts:** Alex and Sam have density 0.4. Portland Alex has four accounts with incoming counts LinkUp 2, Chatter 1, CodeForge 2, FieldNotes 1; mean trust 72.5 and density 0.5. Casey's names are "Casey Brooks" and "C. Brooks"; density 1.0. Duplicate HTML/`sameAs` links do not double these counts.
- [x] **Interview link:** the Riverbend interview links to Sam's LinkUp page, but Sam doesn't appear in Alex's run, because articles aren't followed.
Checked 2026-10-10 in `tests/test_stage2.py` against `tests/seed_evidence.py`, which builds `01_raw_evidence.json` bundles from the mock seed the way the deterministic collector (1.3) does. Rerun these targets through the real Section 1 collector once it lands.

- [x] **Optional real-Jev check** (`--models`, selected real `JEV_MODE`): record pair answers for the two-Alex and alias cases. Confirm plausible grouping and schema handling; do not assert deterministic provider probabilities. Validate thresholds separately on consenting teammates, including no-match cases. Recorded 2026-10-10 on the mock with TypeSafe `jev-1.13.0` (four calls, about 1.1 s each, 1.6–2.9K input tokens): groupings matched the fallback rules for `alex` default and ambiguous, `sam`, and `casey` (Casey needed no call). Pairs within one person scored 0.75–0.95, so every asked pair cleared 0.70. Jev scored 0.90 for two pairs that the rules reject but that join anyway through a mutual link ("Alex Morgan Photo" with the same handle), and 0.75–0.81 for Kansas City accounts beside the stale 2022 Photogram page. The 20 Kansas City × Portland pairs, which the conflict guard never asks, scored 0.07–0.18 when probed directly, so Jev also separates the namesakes. Thresholds on consenting teammates remain open.

## 6. Section 3: Verification and confidence

**Goal:** score how strongly the retrieved evidence supports each candidate, accept at most one, and record why. Neither the graph nor text-only Jev verifies a face. Percentages are model judgments or uncalibrated rule scores, not measured identification accuracy.

**Diagram:** Verification/Synthesize Pass → "John Doe 94% ✓, Johnny Appleseed 30% ✗, Jane Doe 2% ✗".

**In:** `01_raw_evidence.json`, `02_profiles.json`. **Out:** `03_verification.json`.

**Who runs it (Proposal).** One synthesis Strands agent is created after collection and grouping. It reviews Section 3 and writes Section 4. Python calls verification exactly once and exposes the stored result through a read-only `@tool`; the agent cannot rescore candidates or fetch pages. An optional `VerificationNotes` response adds rationales only. Section 4 reuses the agent. Stage replay creates a fresh agent from artifacts; no persisted chat history is needed. Offline mode uses code rationales and the template.

### 3.1 Signals per candidate (computed in code)

| Signal | Definition | Why it matters |
|---|---|---|
| `image_support` | Distinct image IDs in the profile's `direct_image_ids`, divided by input X | Repeated retrieval supports a candidate; angles are correlated |
| `top_hit_share` | Share of input images whose rank-1 hit maps to an account or attributed Mention of this profile | Separates mock namesakes returned at lower ranks |
| `mean_link_trust` | From 2c, 0–100 | Accounts that vouch for each other |
| `platform_count` | Number of distinct platforms in the profile | A wider footprint |
| `consistency` | Accounts matching the most common nonempty normalized workplace, divided by all accounts. Missing workplaces contribute 0; all missing gives 0. | Exposes disagreement and missing data |
| `name_collision` | Another candidate has a compatible name | Flags the two-Alex case |
| `newest_update` | Latest valid `updated_at`, or null | Helps compare dated claims; a fetch timestamp is not an update |
| `face_score` | Highest `face_score` among the hits that reached this profile's accounts or attributed Mentions, kept after triage; null when none has one (Yandex alone, the mock) | How close the search backend's face comparison was |
| `face_match` | `face_score` on its backend's scale, clamped to 0–1: 0 at the floor of the "possible"/"uncertain" band, 1 at the "strong"/"certain" band (V3 90→99, FaceCheck 70→90); null with `face_score` | Puts V3's and FaceCheck's scores on one scale |

Followed links never create a new direct image match. Failed/empty searches remain in the denominator. Resolve hit URLs through `url_aliases`, and attribute mentions as in 2a. An image's rank-1 hit counts toward `top_hit_share` only if that image kept the page after triage. Break workplace ties lexically for deterministic scoring. The optional Jev consistency `score` is deferred; keeping one definition makes fallback comparisons meaningful.

### 3.2 Confidence

**Primary method (`typesafe` or `openrouter`, Proposal).** Make one Jev request. The state holds every candidate's account summary and signals, plus the number of images. It asks two kinds of question:

- `match_<profile_id>`, a `noul`: "Does the retrieved evidence support candidate <id> as the searched participant? Treat missing or conflicting evidence as uncertainty; you have no pixels or face comparison." Its `noul` value becomes the displayed model confidence. When any candidate has a `face_match`, the question ends "you have no pixels, only face_match" instead, and the state explains `face_match` and says a strong match puts the participant in a page's photo without proving the account is theirs. Jev gets `face_match` (two decimals) but not `face_score`, whose scale depends on the backend.
- `best`, a `choice` over the candidate IDs plus `none`: "Which candidate, if any, is the searched person?" This is a cross-check.

**Fallback formula (Proposal).** Use it for `fake`, `off`, or failed Jev calls. Always compute it for comparison. In `fake` mode, stage 3 still sends synthetic answers derived from the formula through the Jev adapter, so typed parsing is exercised and the call is recorded, but scoring uses the formula. These weights preserve the draft's mock behavior; they are not calibrated probabilities:

```
z = −4 + 2·image_support + 3·top_hit_share + 1.5·(mean_link_trust / 100)
       + 0.3·min(platform_count, 5) + 1·consistency − 0.5·name_collision
       + 4·(face_match − 0.5)          # only when face_match is set
confidence = 1 / (1 + e^(−z))
```

The face term (added 2026-10-11) moves z by up to 2 either way: a weak match lowers confidence and a strong one raises it. Before it, live V3 runs scored a candidate whose only face match was 80 at 0.79 (it was the rank-1 hit) and one at 99.86 at 0.15 (rank 2). The mock has no face scores, so the values below are unchanged.

With the proposed extraction and graph rules, the formula gives these values (rounded to two decimals for display):

| Case | Candidate | Confidence |
|---|---|---|
| `alex` default | Alex Morgan (Kansas City) | 0.99 |
| `alex` ambiguous | Alex Morgan (Kansas City) | 0.98 |
| `alex` ambiguous | Alex Morgan (Portland) | 0.69 |
| `sam` | Sam Rivera | 0.99 |
| `casey` | Casey Brooks | 0.97 |

`top_hit_share` is the signal that separates the two Alexes. The ambiguous scenario returns the photographer's pages for all three angles, so `image_support` alone scores them almost the same.

**Decision rule (evaluate in this order).** Johnny decided the 0.80/0.20 thresholds and the live review policy on 2026-10-10 (`../brain/decisions.md`); the rest of the rule is Proposal.

- If there are no candidates, use `no_candidates` when collection completed without errors but found no candidate accounts. Use `evidence_unavailable` if collection failed or yielded no usable accounts after access errors. Do not call Jev or the synthesis model for an empty candidate list.
- Validate the complete Jev response or fall back for the whole verification stage. Do not mix partial Jev scores with fallback scores in one ranking.
- With a valid real Jev response, if `best.probabilities["none"] >= 0.5`, accept nobody (`jev_none`). If `best.choice` differs from the highest `noul` candidate, accept nobody (`jev_disagreement`). Skip this check on the formula path.
- Require a nonempty sourced name, direct image support, and at least one HTML account. Otherwise return `insufficient_evidence`. Snippets alone cannot establish a match.
- Accept the top candidate only if confidence is ≥ 0.80 and the lead over the runner-up is ≥ 0.20. With one candidate, use runner-up 0. Use unrounded values; the lead comparison allows only 1e-9 of float noise, so 0.85 vs 0.65 counts as 0.20. Otherwise use `below_threshold` or `too_close`. Accept at most one.
- Keep the Jev number but flag `jev_fallback_disagree` when the absolute difference exceeds 0.4. Carry graph/grouping flags. Add `partial_search_failure` when some image searches failed, `sparse_footprint` for fewer than three accounts, and `verification_fallback` when a real Jev call failed or returned an incomplete or invalid response.
- **Decided (Johnny, 2026-10-10; narrowed 2026-10-11):** a live run whose real Jev call failed (an error, an invalid answer, or a request over the size cap) returns `review_required`, with no automatic match. This prevents the synthetic rule weights from appearing to verify a real person during an outage. Until 2026-10-11 this covered every live run on the formula, so with `JEV_MODE=fake` (the default) or `off` no live run could accept anyone. Johnny called that a bug. Those modes choose the formula on purpose, so their live runs now go through the checks below like the mock.

### 3.3 Contract

```python
VerificationReason = Literal[
    "accepted", "below_threshold", "too_close", "jev_none", "jev_disagreement",
    "no_candidates", "evidence_unavailable", "insufficient_evidence", "review_required"
]

class VerificationNotes(BaseModel):
    rationales: dict[str, str]       # existing profile IDs only; one sentence each

class CandidateSignals(BaseModel):
    image_support: float
    top_hit_share: float
    mean_link_trust: float
    platform_count: int
    consistency: float
    name_collision: bool
    newest_update: str | None = None
    face_score: int | None = None   # 0-100; set together with face_match
    face_match: float | None = None # 0-1

class CandidateVerdict(BaseModel):
    profile_id: str
    name: str | None
    confidence: float               # 0–1; display with method, not as accuracy
    fallback_confidence: float
    method: Literal["jev", "fallback"]
    decision: Literal["accepted", "rejected"]
    signals: CandidateSignals
    flags: list[str] = []           # e.g. "jev_fallback_disagree", "cross_profile_link"
    rationale: str | None = None    # code template or agent note; never used for scoring
    jev_model: str | None = None

class VerificationResult(BaseModel):
    run_id: str
    verdicts: list[CandidateVerdict]   # highest confidence first
    accepted_profile_id: str | None
    reason: VerificationReason
    jev_calls: list[JevCall]
```

### 3.4 Done when (mock)

Built 2026-10-10 in `pipeline/stage3_verify.py` and revised after a GPT-6 Astra review. `tests/test_stage3.py` checks these on real Section 2 output built from the seed (`tests/seed_evidence.py`), in fake and off modes. The outage rows inject terminal search errors because the seed helper doesn't model server faults. Choices the spec left open:

- A `best` answer that picks any of the tied top-noul candidates passes the cross-check; the tie then fails as `too_close`.
- A live run whose real Jev call failed returns `review_required` before any other check. Fake and off runs skip this check, live or not.
- Dates without a timezone count as UTC for `newest_update`.
- The profile name may come from a snippet account if another account in the profile has HTML evidence.
- Jev's question text names candidates only by ID, never by scraped name. The state caps text fields, and a request over 24,000 bytes of JSON falls back without a call.
- Profile ID `none` is refused, because it is Jev's no-match option.

- [x] **`alex` default:** Kansas City Alex is accepted.
- [x] **`alex` ambiguous:** under the specified defaults, Kansas City Alex is accepted and Portland Alex is rejected. Weight changes must rerun this regression case; do not promise this for arbitrary weights.
- [x] **`alex` stale:** Kansas City Alex is accepted, and the archived 2021 bio doesn't become a separate candidate.
- [x] **`sam`:** Sam Rivera is accepted, with both name forms in one profile.
- [x] **`unknown`:** the result is `reason="no_candidates"`; no downstream model calls are made. With the default fake/off settings, no model calls occur anywhere. A real per-image agent would already have used its LLM to search.
- [x] **Jev unavailable** (simulated with an exception): the run finishes on the fallback with `method="fallback"`.
- [x] **Boundaries:** test 0.80, the 0.20 margin, single candidates, ties, `best=none`, conflicting `best`, malformed answers, and all-search failures. A partial search failure lowers support; it is not silently removed from X.
- [x] **Live fallback policy:** simulated live metadata plus failed Jev yields `review_required` and no accepted ID, with no live calls in the test. Simulated live metadata with fake or off Jev reaches the same reason as the mock.
- [x] **Face scores** (2026-10-11): with the same evidence, a closer face match gives higher confidence; a candidate with a strong face match outranks a rank-1 candidate with a weak one; hits dropped in triage don't count; the mock and Yandex results are unchanged; the largest request with a `face_match` on every candidate still fits in 24,000 bytes.
- [ ] **Optional, real Jev:** record its answers beside the fallback. Fixture success alone does not establish live accuracy.

## 7. Section 4: Summarization

**Goal:** write the final profile (name, workplace, bio) for the accepted candidate. If there is none, return a clear "no confident match".

**Diagram:** Summarization → Profile (Name, Workplace, Bio).

**In:** `01_raw_evidence.json`, `02_profiles.json`, `03_verification.json`. **Out:** `04_final.json` (`FinalProfile | NoMatch`).

### 4.1 Behavior

- **Accepted candidate.** Code builds name, role, workplace, location, account links/trust, run ID, confidence/method, and timestamp. The summary input contains the accepted account facts and attributed mentions. The synthesis agent returns `SummaryDraft`, containing a sourced bio. It never returns `FinalProfile` or invents a confidence number. Validation excludes rejected candidates even if the agent saw them during verification.
- **Citation check.** Canonicalize citations through `url_aliases` and require nonempty sources belonging to the accepted profile or its attributed mentions. URL membership alone does not prove a claim: structured fields must copy extracted values, and the short bio must stay within the supplied bios. On bad citations or unsupported additions, allow one correction, then use the source bio template. Missing optional fields stay null; missing bio stays null.
- **Workplace.** Prefer the newest dated first-party account with an explicit workplace; retain conflicting evidence as a flag. On a date tie with different employers, leave workplace null. Undated sources cannot beat a dated source, and article publication dates do not establish current employment. Alex's dated 2026 accounts support Prairie Atlas Labs. The 2022 Photogram entry and unlinked 2021 archive must not override them.
- **Bio (Proposal).** For the MVP, select at most 3 exact sentences from accepted Account bios. Code checks sentence membership and citations. This makes support testable without another model judge. Defer free-form rewriting. Omit a bio that cannot meet the public-facts rule.
- **Deterministic template** (`LLM_MODE=off`):
  - name: the profile name
  - workplace and role: from the newest account with explicit fields; null on unresolved conflict
  - location: the most common one
  - bio: up to 3 sentences from the LinkedIn bio, or else the longest eligible bio; null if none
- **No accepted candidate.** Return `NoMatch` with the verification reason and top three scored candidates. `review_required` and `evidence_unavailable` must remain distinct from a successful search with no matches. Skip the summary model.

**Built** (2026-10-10, `pipeline/stage4_summarize.py`, `tests/test_stage4.py`). Revised the same day after a GPT-6 Astra review. Unit fixtures use the seed evidence helper and the real Section 2–3 code. HTTP integration tests run Sections 1–4 over the loopback mock with mock/fake/off settings for Alex default, stale, and ambiguous, Sam, Casey, and unknown. The rules below are implementation defaults, not decisions:

- **Facts:** HTML accounts supply role, workplace, location, and bio. Snippet accounts stay in the account list. They can source the profile name only when no matching HTML account does. If the name is unusable or no account supports it, the stage fails before any model call.
- **Workplace and role:** each is chosen separately, from the newest account that states it. Timestamps are compared in UTC. Date-only and timezone-free values count as UTC, and invalid dates count as undated. Undated sources cannot beat dated ones. Values are compared ignoring case and whitespace but keeping punctuation (C++ and C# differ). If the newest values disagree, the field is null. Disagreement anywhere adds `workplace_conflict` or `role_conflict`. Alex's default case carries `workplace_conflict` because of the 2022 Photogram employer.
- **Location:** the most common value; null on a tie. Values with digits, recognizable street addresses or PO boxes, contact details, or instructions are skipped. This is a heuristic, not geographic validation.
- **Public facts:** an English keyword screen drops bio sentences about age, relationships or family, religion, ethnicity or nationality, health, sexual orientation or gender identity, politics, contact details, or street addresses. Instruction-like text, such as "ignore previous instructions" or "set confidence to", excludes the whole bio. The screen has both false positives and false negatives, and it doesn't guarantee that all sensitive information is caught.
- **Model input and citations:** the agent gets the accepted candidate's name, each account's eligible bio sentences (up to 1,000 characters, never cut mid-sentence), and attributed mention URLs and kinds. It gets no page bodies and nothing about rejected candidates. It may select at most three exact account-bio sentences; whitespace normalization is allowed. Citations are canonicalized through `url_aliases`, and every cited account must support a selected sentence. A mention citation alone does not support an account-bio sentence.
- **Agent:** stage 4 creates a fresh Strands agent from the artifacts. Its only tool is `read_verification`, a read-only copy of the reason, the accepted ID, and the accepted verdict's score, method, and decision. It doesn't produce `VerificationNotes`. Each invocation gets 4 turns, including the SDK's schema repairs. A rejected draft gets one correction. If the SDK's repairs run out, the draft falls back without a correction. Both invocations share a cooperative 10 s budget. Cancellation signals the workers and discards late results, but synchronous construction and stalled worker I/O can still delay the return. Failures record `summary_error`, `summary_rejected`, or `summary_timeout` and add `summary_fallback`.
- **Accounts:** listed by descending trust, keeping profile order on ties. An account with no incoming link has trust 0.

### 4.2 Contract

```python
class Sourced(BaseModel):
    value: str
    sources: list[str]

class SummaryDraft(BaseModel):
    bio: Sourced | None = None

class FinalAccount(BaseModel):
    platform: str
    url: str
    display_name: str | None = None
    trust: int

class FinalProfile(BaseModel):
    status: Literal["match"] = "match"
    run_id: str
    name: Sourced
    role: Sourced | None = None
    workplace: Sourced | None = None
    location: Sourced | None = None   # city level only
    bio: Sourced | None = None
    accounts: list[FinalAccount]
    confidence: float
    method: Literal["jev", "fallback"]
    flags: list[str] = []
    generated_at: datetime

class CandidatePreview(BaseModel):
    profile_id: str
    name: str | None
    confidence: float
    method: Literal["jev", "fallback"]

class NoMatch(BaseModel):
    status: Literal["no_match"] = "no_match"
    run_id: str
    reason: VerificationReason
    top_candidates: list[CandidatePreview]  # at most three
    generated_at: datetime
```

Example output for `alex` on the mock, fake Jev and optional LLM summary (abridged; display confidence rounded):

```json
{
  "status": "match",
  "name":      { "value": "Alex Morgan", "sources": ["https://linkup.test/in/alexbuilds"] },
  "role":      { "value": "Software engineer", "sources": ["https://linkup.test/in/alexbuilds"] },
  "workplace": { "value": "Prairie Atlas Labs",
                 "sources": ["https://linkup.test/in/alexbuilds"] },
  "location":  { "value": "Kansas City, MO", "sources": ["https://linkup.test/in/alexbuilds"] },
  "bio":       { "value": "I build open-source transit maps and volunteer at the Riverbend maker club.",
                 "sources": ["https://linkup.test/in/alexbuilds"] },
  "confidence": 0.99,
  "method": "fallback"
}
```

### 4.3 Done when (mock)

Checked against seed-derived fixtures and real Section 1–4 artifact runs over the loopback mock internet. Stub agents and a Strands agent on a scripted model cover the model path offline; the optional real-provider check is still open.

- [x] **`alex` default and stale:** the workplace is Prairie Atlas Labs, not Old Mill Software.
- [x] **`sam`:** the name is "Sam Rivera" and the workplace is Mosaic Kite Design.
- [x] **`unknown`, and cases where every candidate is rejected:** the result is `NoMatch`.
- [x] **Sources:** every `sources` URL in the output is a page in `01_raw_evidence.json`.
- [x] Unrelated-but-collected URLs, invented sources, changed confidence, and unsupported employers cannot enter the result. Check missing bio, conflicting dates, and summary failure fallback.
- [ ] **Optional, with the LLM on:** the same checks pass when the agent writes the bio. Stub agents and a scripted-model Strands agent cover this offline; no real model run yet.

## 8. Section 5: End-to-end run and demo

### 5.1 CLI

Today `run` works through every stage registered in `STAGE_RUNNERS` (stages 1–4 are all registered) and exits with code 3 at the first stage that isn't. `stage N --run` re-checks consent, invalidates stage N and later outputs, and reruns stage N. `smoke-search` runs one image through deterministic stage 1 with models off, and `purge` and `set-key` work. `purge` also deletes uploaded images when `AWS_REGION` is set. The rest are commands to implement.

```bash
# Backend V3 (default): the SerpApi key and AWS keys plus region (hidden prompts, checked first)
python3 -m pipeline set-key serpapi
python3 -m pipeline set-key aws --region us-east-1
# Or the FaceCheck token, for SEARCH_BACKEND=facecheck (checked with the free /api/info call)
python3 -m pipeline set-key facecheck

# Mock search, no credits. Start the mock internet in another terminal first.
python3 -m mock_internet serve
python3 -m pipeline run --capture tests/fixtures/captures/alex --mock
python3 -m pipeline run --capture tests/fixtures/captures/alex --mock --scenario ambiguous

# Re-run one stage from a saved run
python3 -m pipeline stage 2 --run runs/<run_id>

# Optional paid models with mock search; select modes and keys in .env first
python3 -m pipeline run --capture tests/fixtures/captures/alex --mock --models

# Live search (V3 by default): consent-bearing manifest with real images
python3 -m pipeline smoke-search --capture captures/<session_id> --image-id img-1
python3 -m pipeline smoke-search --capture captures/<session_id> --image-id img-1 --backend facecheck
python3 -m pipeline run --capture captures/<session_id> --models

# Delete a run's data and any uploaded images
python3 -m pipeline purge --run runs/<run_id>
# Also delete the registered source capture folder when the capture owner permits it
python3 -m pipeline purge --run runs/<run_id> --include-capture
```

- `run --until N` stops after stage N (0–4). `--scenario` overrides `MOCK_SCENARIO` for mock search; reject it with live search. Each run gets new artifacts and a new cache.
- `stage N --run ...` supports 1–4 and loads saved run settings/prerequisites. A saved run keeps the search backend it was created with, and real model calls need `--models`. Re-read the source manifest's consent; refuse a missing/revoked manifest or changed participant/session. Later stages read saved SerpApi evidence locally and make no new search. Invalidate downstream outputs and never reuse mismatched artifacts.
- `--models` authorizes the selected modes, without choosing a provider. If modes remain fake/off, print that no real model is enabled. A SerpApi run without `--models` therefore performs live collection with deterministic verification, and the formula decides whether a candidate is accepted.
- `--refresh` is valid for live search in `run`, `stage 1`, or `smoke-search`; it bypasses only this run's cache. Smoke-search stores a stage-0 bundle containing only the selected image and stage-1 evidence with `num_images=1`, while retaining the original capture folder for consent/cleanup.
- `purge` needs no consent or API search key. Attempt S3 deletion, then delete run artifacts, logs, and caches even if remote cleanup fails. On failure, keep only the minimal cleanup manifest, report failure, and allow a retry. Reject paths outside `runs/` and never follow symlinks. `--include-capture` deletes only the registered folder under `captures/`, never test fixtures or arbitrary external paths. Default purge reports the source capture still retained. The capture owner must clear it and any other runs for the participant on withdrawal.

As it runs, `run` prints each of the diagram's views in turn:

1. hits per image
2. the parsed account list
3. the profiles
4. each profile's trust table
5. the verdicts with percentages, method, flags, and reason
6. the final profile

### 5.2 Tests

`python3 -m unittest discover -s tests -v` is the normal offline suite. Future pipeline tests start their own mock server on a free port, following `tests/test_mock_internet.py`, with mock/fake/off modes forced regardless of `.env`. Patch or reject external connections and model constructors. Tests may inspect synthetic SQLite entity IDs in assertions; the pipeline never receives those IDs.

Keep optional provider smoke checks outside normal discovery. `smoke-search` forces a live backend (V3 unless `--backend` says otherwise) and requires external-processing consent; only paid-model smoke checks require `--models`. Unit tests inject 429s, timeouts, malformed SDK answers, failed cleanup, and invalid citations without paid calls. Do not write brittle tests for model wording or exact live probabilities.

Review validation, 2026-10-10: the database persistence test passed. The HTTP suite could not start because the review sandbox blocks sockets (`PermissionError`); it was not rerun successfully here. Direct calls to the existing search handler against the seed in memory confirmed the counts below. Those checks do not replace HTTP integration tests or test the unbuilt pipeline.

### 5.3 Fallbacks for the demo

| If this is down | The pipeline does this |
|---|---|
| Hosting, SerpApi quota/outage, or fresh camera images return no useful hits | Reuse a same-run sanitized cache if present. Otherwise record failure or zero hits, continue other images, and use the labeled mock demo if live retrieval is ineffective. |
| Jev access, routing, or service | Use grouping rules and formula; record `method="fallback"`. A live run whose real Jev call failed requires review; fake/off and mock runs still use the accept rule. |
| Claude/Bedrock or structured output | Reuse collected evidence with deterministic triage, partial extraction, code rationales, and the template. |
| A page fetch or login wall | Keep public search title/snippet only. No HTML edges; no match from snippets alone. |
| Too many accounts or slow calls | Apply account/page limits and deadlines; record truncation/partial evidence instead of widening the crawl. |
| Everything external | Run the mock demo |

### 5.4 Time budget (X = 3, live)

These are targets, not measurements. Reverse-image retrieval quality and image hosting are the first risks to test; Jev access is next. Check latency against `run_log.jsonl`. Set a 60 s overall deadline and reserve up to 10 s for cleanup. Within that deadline, cap each per-image agent at 35 s and each Jev/summary call at 10 s. Retries share the remaining budget. A timeout cancels async work; a worker-thread timeout does not stop its I/O, so use client timeouts and prohibit late writes into finalized artifacts.

| Step | Target |
|---|---|
| Upload and reverse search (parallel) | ≤ 10 s |
| Fetch and triage (parallel, ≤ 12 pages per image) | ≤ 25 s |
| Extraction and one Jev grouping call | ≤ 5 s |
| Verification and summary | ≤ 10 s target; defer optional agent rationale call if time is short |
| Total | ≤ 50 s |

### 5.5 Done when

- [ ] The end-to-end mock test passes for every row of the test matrix below.
- [ ] One live run on a consenting team member completes within the deadline with a participant-confirmed match or an explicit no-match/review reason. Report retrieval quality separately from graceful failure.
- [ ] `purge` removes run data, per-run caches, and S3 objects; a cleanup failure is visible and retryable. Test `--include-capture` and withdrawal cleanup with a temporary local capture and fake hosting adapter.

## 9. Mock test matrix

Section 1 counts below assume rank-order deterministic collection, one-level account following, no retries on 403/404, and canonical merging. Failed direct hits retain search snippets. Seed/server facts are checked, and `tests/test_stage1.py` checks the Section 1 column against the real collector; Sections 2–4 are proposed acceptance targets for code still to be built. Confidence values are fallback outputs rounded for display.

| Capture | Scenario | Section 1 | Section 2 | Section 3 | Section 4 |
|---|---|---|---|---|---|
| alex (front, left, right) | default | 4+4+5 = 13 hits; 10 URLs/fetches; 9 HTML responses → 8 HTML records + 1 snippet (404) | 1 profile, 5 accounts, mean trust 76 | accepted, 0.99 | Prairie Atlas Labs |
| alex | ambiguous | 7+7+8 = 22 hits; 13 hit URLs; CodeForge alexframes adds 1 fetch; 12 HTML records + 1 snippet | 2 Alex profiles: 5 KC accounts, 4 Portland accounts | KC 0.98 accepted; Portland 0.69 rejected | Kansas City Alex |
| alex | stale | 6+6+7 = 19 hits; same 10 unique URLs as default; archive/deleted additions repeat existing pages | 1 profile, 5 accounts | accepted, 0.99 | Prairie Atlas Labs |
| alex | duplicates | 6+6+7 = 19 hits; same 10 URLs and 9 evidence records as default | 1 profile, 5 accounts; no multiplied edges | accepted, 0.99 | same as default |
| alex | empty | 3 successful searches, 0 hits/fetches | no accounts | `no_candidates` | `NoMatch` |
| alex | rate_limited | 429 + `Retry-After: 1`; at most 3 attempts per image; no hits/fetches | no accounts | `evidence_unavailable` | `NoMatch` |
| alex | server_error | 503 + `Retry-After: 1`; at most 3 attempts per image; no hits/fetches | no accounts | `evidence_unavailable` | `NoMatch` |
| sam (front) | default | 4 hits; 6 fetches after CodeForge/FieldNotes; 5 HTML records + 1 snippet (403) | 1 profile, 5 accounts, Sam + Sammy, mean trust 76 | accepted, 0.99 | Mosaic Kite Design |
| casey (front) | default | 2 hits/fetches/HTML records | 1 profile, Casey Brooks + C. Brooks, trust 50/50 | accepted, 0.97; sparse flag | Blue Finch Music School |
| unknown | default | 0 hits | none | `no_candidates` | `NoMatch` |

Exact failed URLs: `https://fieldnotes.test/@alexbuilds/old-project` (404) and `https://photogram.test/samdraws/private-album` (403). Mirror: `https://syndicated.test/transit/alex-morgan` canonicalizes to `https://riverbend-news.test/people/alex-morgan-transit`. Sam's followed accounts are `https://codeforge.test/samdraws` and `https://fieldnotes.test/@samdraws`; Portland Alex adds `https://codeforge.test/alexframes`. No photographer Photogram page is found by these one-level fixtures.

The seed has six people, 47 pages, 73 links, and six fixtures. The existing `mock_internet demo` counts 9 successful fetches and 15 discovered links before canonical merging; it does not follow those links or run Sections 2–4. Do not confuse its counts with the future pipeline's normalized evidence count.

## 10. Decisions to confirm

Every default in this table is a **Proposal**, not a recorded team decision.

| Open question | What this spec assumes | Who decides |
|---|---|---|
| Capture handoff format and X | A folder with `manifest.json`; X = 3 | Johnny and teammates |
| Jev key: TypeSafe or OpenRouter | Confirm key issuer, build that route first, defer the other | Johnny |
| What Jev decides | Grouping (2b) and confidence (3); defer consistency score | Johnny |
| Merge per-image results before or after Jev | Before, at the end of Section 1, keeping per-image provenance | Johnny |
| Which agent runs verification and summary | One synthesis agent after the per-image agents | Johnny |
| Image hosting for Yandex | Private S3 bucket, 10-minute presigned URL, delete in `finally`; verify Yandex can fetch it | Johnny |
| Trust formula | Table: 0 → 0, 1 → 50, 2 → 95, 3+ → 99 | Johnny |
| Trust → confidence and live acceptance | **Decided 2026-10-10:** accept at ≥ 0.80 with a ≥ 0.20 lead, and a live run whose real Jev call failed returns `review_required` (narrowed 2026-10-11 from every live run on the formula). Still proposals: the Jev `noul` plus `best` mechanism and the uncalibrated formula weights in 3.2. | Johnny |
| Strands model | **V4, 2026-10-11:** Anthropic `claude-opus-5-5` at medium effort (`LLM_EFFORT`). Earlier runs (V3) used `claude-sonnet-5-5` at low effort, still selectable; Haiku `claude-haiku-4-5-20251001` is a documented alternative. Use Bedrock only with confirmed model access and its own ID. One provider first. | Johnny |
| Consent and retention | External-processing consent; per-run cache; delete after demo/withdrawal; capture owner clears originals and other runs | Johnny and capture teammates |
| Summary wording | Select sourced bio sentences for the MVP; defer free-form prose until factual support can be checked | Johnny |
| Where the final profile is shown | `04_final.json` and the console; no UI decided yet | Team |

External checks still open: actual package installation and authenticated provider calls, Jev key issuer/access, and S3-to-Yandex fetch compatibility. The SDK issue #4482 report is unverified (1.3). The earlier first-week Jev outage claim in `../brain/jev.md` was not independently confirmed here; fallback behavior does not depend on it. The model IDs, Jev endpoint shapes, Yandex default tab, and SerpApi free limits above were checked against the linked official docs on 2026-10-10.

## 11. Build checklist

Section 0

- [x] `requirements.txt`, the `pipeline/` package, `config.py`, and new `.env.example` entries
- [x] `contracts.py` with every model in this spec
- [x] `capture.py` with the consent gate, plus mock capture bundles for alex, sam, casey, and unknown
- [x] `artifacts.py` and `run_log.jsonl`
- [x] Ignore captures/runs/caches/traces; enforce both external-call gates and redact logs
- [ ] Send the capture contract (0.4) to the teammates

Section 1

- [x] `platforms.py`: domain table, account and post URL patterns, URL normalization (including mock local URLs)
- [x] `scrape.py`, tested against mock pages
- [x] `search/mock_backend.py`
- [x] Deterministic `collect_for_image` and the cross-image merge; Section 1 mock tests
- [x] Strands per-image agent with its tools and `ImageTriage` output; stub-model topology/concurrency test
- [x] `hosting.py`, cleanup manifest, and `search/serpapi_backend.py` with run-local cache; `smoke-search` command
- [x] Real-internet search path: `IMAGE_HOST=tunnel` (localhost.run) checked live with a landmark photo on 2026-10-10
- [ ] Consent-bearing smoke-search on one team member (tunnel hosting; checks that Yandex finds the person's pages)
- [x] Bound retries, deadlines, fetch destinations, and in-flight deduplication; preserve error snippets and aliases

Section 2

- [x] Account and mention extraction
- [x] `jev.py`: fake/off first, then the route matching Johnny's key; validate typed answers and record `JevCall` (both routes built and tested offline; no real Jev call yet)
- [x] Grouping: supported mutual links, bounded Jev pair questions, component conflict guard
- [x] Graph and trust table; diagram parity test
- [x] Section 2 mock tests
- [x] Record real Jev answers on the mock cases (2e)
- [ ] Validate live grouping thresholds on consenting teammates

Section 3

- [x] Signals, fallback formula, and decision rule
- [x] Jev confidence call, `best` cross-check, incomplete-evidence reasons, and live fallback policy
- [x] Section 3 mock tests (on hand-built 01/02 artifacts until Sections 1 and 2 land)
- [x] CLI wiring (`STAGE_RUNNERS[3]`, plus `[4]` for Section 4); `tests/test_stage3.py` runs `run --mock` through stage 4 and replays `stage 3`

Section 4

- [x] Deterministic template and `NoMatch`
- [x] Synthesis agent with read-only verification tool, `SummaryDraft`, and citation/content checks; code assembles `FinalProfile`
- [x] Section 4 mock tests, including real Section 1–4 runs over the loopback mock for the six matrix cases
- [x] CLI wiring (`STAGE_RUNNERS[4]`)

Section 5

- [ ] CLI (`run`, `stage`, `smoke-search`, `purge`) and console output
- [ ] End-to-end mock test for every matrix row
- [ ] First live run on a team member
- [ ] Purge and withdrawal drill, including failed remote deletion and source capture cleanup
