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

The pipeline in `pipeline/` follows `docs/agent-pipeline-spec.md`. Only Section 0
(foundation) is built so far: settings, artifact contracts, the capture loader and
consent gate, run folders, and the CLI. It needs Python 3.12 and the packages in
`requirements.txt`. If your Python has no pip, [uv](https://docs.astral.sh/uv/) can
create the environment:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Search uses SerpApi by default. Save your key once; the prompt hides what you
type, checks the key with SerpApi's free Account API (no search credits), and
writes it to the gitignored `.env`, creating it from `.env.example` if needed:

```bash
.venv/bin/python -m pipeline set-key serpapi
```

`set-key typesafe`, `set-key openrouter`, and `set-key anthropic` save the other
keys the same way. Add `--mock` to search the local mock internet instead
(no key or credits needed), or set `SEARCH_BACKEND=mock` in `.env`.

```bash
# Validate a mock capture bundle and write runs/<run_id>/00_capture.json.
.venv/bin/python -m pipeline run --capture tests/fixtures/captures/alex --until 0 --mock

# Delete a run (and, with --include-capture, its source folder under captures/).
.venv/bin/python -m pipeline purge --run runs/<run_id>

# Offline test suite: mock internet plus pipeline.
.venv/bin/python -m unittest discover -s tests
```

Every run needs a capture folder whose `manifest.json` records the participant's
consent (spec 0.4). SerpApi search (the default) and `--models` (paid model calls)
send data to outside services, so both require `consent.external_processing: true`,
and SerpApi runs need real image files. The mock fixtures under `tests/fixtures/`
only run with `--mock`. The offline tests always force the mock. `runs/`,
`captures/`, and `.env` are gitignored.

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
