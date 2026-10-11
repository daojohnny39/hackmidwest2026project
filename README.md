# Hack Midwest: mock internet

A local internet simulator for developing search, scraping, deduplication, and
profile parsing without spending search credits. It uses Python's standard
library and a persistent SQLite database. No API keys, package installs, cloud
services, or live search requests are needed.

## Run it

From this repository directory (`Project Directory/`), with Python 3.10 or newer:

```bash
python3 -m mock_internet serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) to search and browse the fictional
web. The first start creates and seeds `data/mock_internet.sqlite3`; later starts
preserve the database. On Windows, `py -3` can replace `python3`.

In a second terminal, from the same directory:

```bash
# End-to-end search, combine three image fixture result sets, and scrape their pages.
python3 -m mock_internet demo

# Integration tests create their own temporary database and HTTP server.
python3 -m unittest discover -s tests -v
```

Optional server settings:

```bash
python3 -m mock_internet serve --port 9000 --db data/another-world.sqlite3
```

For a different port, set `MOCK_INTERNET_URL=http://127.0.0.1:9000` in the environment
of the client/demo process. `.env.example` documents the setting; `.env` files are
not loaded automatically. The default listener is `127.0.0.1`.

## What's in the database

The seed contains **six fictional people, 47 pages, 73 links, and six image lookup
fixtures**. All names, companies, posts, and events are invented for testing.

| Site | Content |
| --- | --- |
| `linkup.test` | Professional profiles and employment bios |
| `chatter.test` | Social profiles and individual posts |
| `photogram.test` | Photo-oriented bios and a private album |
| `codeforge.test` | Developer profiles |
| `fieldnotes.test` | Blog profiles and a deleted post |
| `riverbend-news.test` | An interview mentioning multiple people |
| `riverbend-makers.test` | A community event with cross-links |
| `archive.test` | An outdated speaker biography |
| `syndicated.test` | A duplicate article with a canonical link |
| `everyday.test` | Unrelated background articles |

Alex Morgan the Kansas City engineer and Alex Morgan the Portland photographer
are different people. Sam Rivera also posts as Sammy Rivera. Some profiles list
outdated employers, while Casey Brooks has only a small public footprint.

Pages contain normal HTML: titles, bios, timestamps, links, Open Graph metadata,
and JSON-LD. Every navigable link stays on the local server. Original fictional
URLs are retained in search result metadata as `mock_original_url`.

## API

All endpoints use GET. No authentication is required.

| Endpoint | Purpose |
| --- | --- |
| `/health` | Readiness and database counts |
| `/search.json?q=Alex+Morgan` | Text search (`/search` and `/api/search` also work) |
| `/search.json?engine=yandex_images&url=mock://alex-front` | Canned image search results |
| `/api/reverse-image?image_id=alex-front` | Simpler fixture lookup endpoint |
| `/fetch?url=https%3A%2F%2Flinkup.test%2Fin%2Falexbuilds` | JSON containing page HTML, text, and links |
| `/web/linkup.test/in/alexbuilds` | Direct HTML for browsers and scrapers |
| `/fixtures` | Available image fixtures, platforms, and scenarios |
| `/images/alex-front.svg` | Local placeholder image for that fixture |
| `/stats` | In-memory request/status counts and database counts |

Text search returns `organic_results`. Image lookup returns `image_results` with
`title`, `link`, `snippet`, `source`, `thumbnail`, and `original_image` fields.
The image result shape follows a subset of the documented
[SerpApi Yandex reverse-image contract](https://serpapi.com/yandex-reverse-image-api).
This is a small API-compatible surface for our tools, not a full SerpApi emulator.
Unknown engines and misspelled fixture IDs return HTTP 400.

Search supports:

- `q`: case-insensitive terms (all must match), quoted phrases, `-excluded` terms,
  and `site:chatter.test` filters. Yandex-style `text` is accepted as an alias.
- `site`: an optional separate hostname filter; can be used without `q`.
- `platform`: `professional`, `social`, `photos`, `code`, `blog`, or `web`.
- `num`: page size, 1–100; default 10.
- `start`: zero-based offset; default 0. More results include a local
  `serpapi_pagination.next` URL.
- `scenario`: one of the cases below.
- `delay_ms`: deterministic latency from 0–5000 ms, also supported on fetch and HTML routes.

Search ranking is deterministic keyword matching, not semantic search. Platform
and site filters apply to text searches. Stored image fixture results use their
seeded ordering. All search result `link` values are directly fetchable local URLs.

Examples:

```bash
curl --get http://127.0.0.1:8765/search.json \
  --data-urlencode 'q="Alex Morgan" -photographer' \
  --data-urlencode 'platform=professional'

curl --get http://127.0.0.1:8765/search.json \
  --data-urlencode 'engine=yandex_images' \
  --data-urlencode 'url=mock://alex-front' \
  --data-urlencode 'scenario=ambiguous'

curl --get http://127.0.0.1:8765/fetch \
  --data-urlencode 'url=https://linkup.test/in/alexbuilds'
```

## Connect future agent tools

Use this adapter as the search/scrape boundary:

```python
from mock_internet.client import MockInternetClient, MockHTTPError

internet = MockInternetClient()  # Reads MOCK_INTERNET_URL; defaults to localhost.

results = internet.search('"Alex Morgan"', num=20)["organic_results"]
for hit in results:
    try:
        page = internet.fetch(hit["link"])
        print(page["title"], page["text"], page["links"])
    except MockHTTPError as error:
        print("Simulated page failure:", error.status)

fixtures = internet.reverse_image("alex-front")["image_results"]
```

The adapter only accepts HTTP loopback origins, ignores proxy settings, and never
follows redirects or falls back to live search. `/fetch` looks up the supplied URL
in SQLite; it never fetches that URL over the internet. Unknown pages return 404.
The server does not read search credentials, and it excludes `api_key` from
responses and request logs.

Existing clients that still call real search providers must be changed to use this
adapter or explicitly configured to use this server. Running the mock server does
not intercept other applications' traffic. No Strands, Jev, or LLM calls are made
by this service or demo; model calls added to another pipeline remain separate.

Image lookup is a **fixture mapping**, not image recognition. Use `alex-front`,
`alex-left`, `alex-right`, `sam-front`, `casey-front`, or `unknown`. The three Alex
fixtures overlap, so merging them exercises deduplication. `unknown` deliberately
has zero matches. Input can be an `image_id`, a `mock://` URL, or an exact local
image URL returned by `/fixtures`. SVGs are labeled placeholders, not photographs.

## Repeatable test cases

| Scenario | Expected behavior |
| --- | --- |
| `default` | Seeded results, including natural text-search ambiguity |
| `empty` | Empty search results |
| `ambiguous` | Alex image fixtures also return the other Alex's profiles |
| `duplicates` | Repeated hits; Alex image fixtures also include a mirrored article |
| `stale` | Alex image fixtures include an old biography and deleted page |
| `rate_limited` | HTTP 429 and `Retry-After: 1` |
| `server_error` | HTTP 503 and `Retry-After: 1` |

Scenarios are per-request, so concurrent tests cannot change each other's world.
The `ambiguous` and `stale` additions specifically apply to the Alex image fixtures;
text searches already retrieve the same underlying pages when their terms match.
The two error scenarios also work with `/fetch` and direct `/web/` routes.
The deleted post always returns 404; the private album always returns 403.
The client exposes status, parsed response body, and headers on `MockHTTPError`.

The integration suite covers search and filters, pagination, all page/link targets,
the SerpApi subset, multi-image deduplication, concurrent requests, real HTTP
failures, timeouts, persistence/reset, and blocked external connections.

## Edit or reset the world

Edit `mock_internet/seed.py`: `PEOPLE` defines fictional identities and `build_world()`
defines profiles, posts, cross-links, and image result sets. Stop the server, then:

```bash
# Replaces all data in this mock database with the seed world.
python3 -m mock_internet seed --reset
python3 -m mock_internet serve
```

Use `--db` on both commands if you use a custom database path. Running `seed`
without `--reset` preserves existing data. Direct SQLite edits also persist across
server restarts. Database files and local `.env` files are gitignored.

Tables: `entities`, `pages`, `links`, `image_fixtures`, `image_matches`, `metadata`.
For assertions, `pages.entity_id` provides synthetic ground truth. Ground-truth
IDs are absent from search responses and scraped HTML, so retrieval tests must
work with the visible content. Request counters reset when the server restarts;
page data persists.

## Agent pipeline

The pipeline in `pipeline/` follows `docs/agent-pipeline-spec.md`; the spec's opening
lines say which sections are built. Section 1 (search and scrape) runs one collector
per image: a reverse-image search, then page fetches and scraping, merged into
`runs/<run_id>/01_raw_evidence.json`. It needs Python 3.12 and the packages in
`requirements.txt`. If your Python has no pip, [uv](https://docs.astral.sh/uv/) can
create the environment:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Search uses backend V3 by default (`SEARCH_BACKEND=rekognition`). For each photo,
[Amazon Rekognition](https://aws.amazon.com/rekognition/) finds the face, SerpApi
searches the open web for a crop of it with Yandex and Google Lens, and Rekognition
then compares the participant's face with every face in every result image. Results
whose best face scores under `REKOGNITION_MIN_SIMILARITY` (default 80) are dropped,
and the rest are ranked by similarity. The older backends stay selectable. V1, Yandex
alone (`SEARCH_BACKEND=serpapi`), returns every visually similar image, which for a
selfie means mostly lookalikes. V2, FaceCheck (`SEARCH_BACKEND=facecheck`), looks the
face up in FaceCheck's own index of faces instead of searching the web.

V3 needs a SerpApi key and AWS keys. Each prompt hides what you type, checks the key
first (SerpApi's free Account API, AWS STS), and saves it outside git: the SerpApi key
in `.env`, created from `.env.example` if needed, and the AWS keys in
`~/.aws/credentials` (see below).

```bash
.venv/bin/python -m pipeline set-key serpapi
.venv/bin/python -m pipeline set-key aws --region us-east-1
```

The AWS user needs `rekognition:DetectFaces` and `rekognition:CompareFaces`. An account
on AWS's Free plan only allows some services in some regions: Johnny's project allows
Rekognition in us-east-2 only, so its `.env` has `AWS_REGION=us-east-2`. Each photo
costs one SerpApi search per engine (two by default; `REKOGNITION_ENGINES` picks them)
and about $0.001 per image Rekognition looks at: one DetectFaces call and up to 40
CompareFaces calls. Only the face crop leaves the machine, as a JPEG without EXIF.
Unless the AWS organization has an AI services opt-out policy, AWS may keep the images
Rekognition analyzes and use them to improve its services
([Rekognition FAQ](https://aws.amazon.com/rekognition/faqs/)); tell participants when
you ask for consent. Each engine's results are cached in the run folder, so `stage 1
--run` on the same run, or a new threshold, makes no new SerpApi search (`--refresh`
does). A result image that won't download falls back to the engine's thumbnail.

For FaceCheck instead, save its token and set `SEARCH_BACKEND=facecheck`. FaceCheck
scores each match from 0 to 100 (90+ certain, 83-89 confident, 70-82 uncertain, 50-69
weak). The prompt checks the token with FaceCheck's free `/api/info` call, which shows
the credit balance and runs no search:

```bash
.venv/bin/python -m pipeline set-key facecheck
```

Each FaceCheck search costs 3 credits ($0.30), one per image. Credits are bought
with Bitcoin or Litecoin. `FACECHECK_DEMO=true` in `.env` makes searches free, but
they scan only the first 100,000 faces, so their results aren't meaningful; use it
to test the plumbing. A run checks the balance before its first paid search and
stops if there are no credits. Results are cached in the run folder, so `stage 1
--run` on the same run doesn't pay again (`--refresh` does). A failed request is
retried by polling the same FaceCheck search, never by starting a second one. The
upload is a JPEG copy at most 1,600 px on its longest side with EXIF stripped;
FaceCheck says it doesn't add uploads to its index and deletes search history
within 24 hours. Match thumbnails show other people's faces, so they're dropped.

For Yandex alone, set `SEARCH_BACKEND=serpapi`. It uses the same SerpApi key.

`set-key typesafe`, `set-key openrouter`, and `set-key anthropic` save the other
keys the same way. A TypeSafe (Jev) key is checked first by listing TypeSafe's
models, which makes no Jev decision; a rejected key isn't saved. Saving a key doesn't
switch anything on: to use Jev, set `JEV_MODE=typesafe` in `.env` and add `--models`
to `run` or `stage`, since those calls are paid.

```bash
.venv/bin/python -m pipeline set-key typesafe
```

AWS keys (for Rekognition, S3 image hosting, and Bedrock) work differently: `set-key aws` asks for
an IAM user's access key ID and secret access key, checks them with STS
GetCallerIdentity (free, needs no permissions), and saves them as the `[default]`
profile in `~/.aws/credentials` with mode 0600, outside the repo, where boto3 finds
them. `--region` also saves `AWS_REGION` to `.env`. Temporary (`ASIA...`) keys are
refused, and root-user keys get a warning.

```bash
.venv/bin/python -m pipeline set-key aws --region us-east-1
```

Add `--mock` to search the local mock internet instead
(no key or credits needed), or set `SEARCH_BACKEND=mock` in `.env`. Start the mock
first with `python3 -m mock_internet serve`.

```bash
# Validate a mock capture bundle and write runs/<run_id>/00_capture.json.
.venv/bin/python -m pipeline run --capture tests/fixtures/captures/alex --until 0 --mock

# Search and scrape (Section 1) on the mock internet, printing hits per image.
.venv/bin/python -m pipeline run --capture tests/fixtures/captures/alex --until 1 --mock
.venv/bin/python -m pipeline run --capture tests/fixtures/captures/alex --until 1 --mock --scenario ambiguous

# Re-run one stage of a saved run (later stages' outputs are deleted first).
.venv/bin/python -m pipeline stage 1 --run runs/<run_id>

# Rebuild accounts, profiles, and trust graphs (Section 2) from a run's saved evidence.
.venv/bin/python -m pipeline stage 2 --run runs/<run_id>

# Delete a run (and, with --include-capture, its source folder under captures/).
.venv/bin/python -m pipeline purge --run runs/<run_id>

# Offline test suite: mock internet plus pipeline.
.venv/bin/python -m unittest discover -s tests
```

Every run needs a capture folder whose `manifest.json` records the participant's
consent (spec 0.4). Live search (V3, the default, FaceCheck, or SerpApi) and `--models`
(paid model calls) send data to outside services, so both require
`consent.external_processing: true`, and live runs need real image files. The mock fixtures under `tests/fixtures/`
require the mock backend (`--mock` or `SEARCH_BACKEND=mock`). The offline tests
always force the mock. `runs/`, `captures/`, and `.env` are gitignored.

SerpApi's engines only take an image URL, so V3 and SerpApi runs host each photo
briefly (spec 1.2); V3 hosts only the face crop. `IMAGE_HOST` picks how:

- `tunnel` (the default) needs only `ssh`. Stage 1 starts a web server on
  127.0.0.1 and opens a free [localhost.run](https://localhost.run) tunnel to it
  (`ssh -R`, no account or key). The tunnel is checked end to end before the first
  search. Each image is served at a random `https://<random>.lhr.life/<32 hex>.jpg`
  path until its search is over, and the tunnel closes when stage 1 ends. Nothing is
  stored off the machine. The free tunnel slows to about 25 KB/s, so each image is
  served as a JPEG copy at most 800 px on its longest side, with EXIF stripped.
  Yandex refuses Cloudflare quick-tunnel (`trycloudflare.com`) URLs, so that option
  isn't offered.
- `s3` uploads each image to a private bucket: set `IMAGE_BUCKET` and `AWS_REGION` in
  `.env`, with AWS credentials from `set-key aws` (or `aws configure`/`AWS_PROFILE`).
  Each image is uploaded under a random key, shared through a presigned URL valid for
  10 minutes, and deleted after its search. `uploads.json` in the run folder tracks
  it until then, and `purge` deletes anything left over.

Try one consenting teammate's photo first. With V3 it costs two SerpApi searches and
about 40 Rekognition calls (3 FaceCheck credits with `--backend facecheck`, one SerpApi
search with `--backend serpapi`), and it never calls a model:

```bash
.venv/bin/python -m pipeline smoke-search --capture captures/<session_id> --image-id img-1
```

With V3 or FaceCheck, each hit carries a face score: the console view prints
`face 97/100` before the title, and the per-image agent is told what the score means.
For V3 that's Rekognition's similarity: 99+ strong, 95-98 likely, 90-94 possible. For
FaceCheck, anything under 70 is a likely lookalike. Even 99 isn't proof, so ask the
participant which hits are really theirs. `search_extras` in `01_raw_evidence.json`
counts what happened to every V3 result (matched, below_threshold, no_face,
image_unavailable, ...).

With `LLM_MODE=anthropic` (or `bedrock`) and `--models`, each image gets its own
Strands agent that decides which hits to fetch and which pages are about the
person. Without them, stage 1 fetches every hit in rank order and follows account
links from the profiles it finds. If an agent fails, its image falls back to that
deterministic path.

### Debugging a run

Every stage appends to `runs/<run_id>/trace.jsonl`, one JSON object per line. It
records:

- each stage's start, end, settings, and duration
- every search and page fetch, with its URL and timing
- each Strands agent's prompts, reasoning, tool calls and results, structured output,
  and token usage
- every Jev request and answer
- every error the pipeline handled or fell back from, with the exception that caused
  it and the file and line where it was raised

Log lines from Strands, httpx, and the model SDKs are kept too. Keys, tokens, and
hosted image URLs are redacted. The trace holds evidence (page text, names,
prompts), so it stays in the gitignored run folder and `purge` deletes it.
`run_log.jsonl` is the metadata-only summary.

```bash
# Errors and fallbacks, with their root causes
jq -c 'select(.level != "info") | {stage, event, agent, image, url, reason, code, cause: .error.chain[-1]}
       | with_entries(select(.value != null))' runs/<run_id>/trace.jsonl

# One image agent's whole conversation
jq -c 'select(.agent == "collector-img-1")' runs/<run_id>/trace.jsonl
```

## OpenCV setup

Run from the project root using Python 3.13:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## Run the OpenCV tests

With the virtual environment activated:

```bash
python -m unittest discover -s tests -v
```

The tests check BGR-to-RGB color conversion and JPEG saving and loading using
synthetic images. They use Python's built-in `unittest` module and require no
camera, MediaPipe model, or additional testing package. Temporary images are
automatically deleted after the test.
