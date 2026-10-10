# Agent pipeline spec

Owner: Johnny. Drafted 2026-10-10. Status: draft.

This spec covers the agent pipeline, the right half of `docs/architecture.png`: everything from the AWS Strands Agent onward. The capture pipeline (camera, MediaPipe, frame selection) belongs to the teammates and shows up here only as the input contract in Section 0.

Items marked **Proposal** answer an open question from `brain/open-questions.md`. The rest of the spec builds on them as defaults. When the team decides differently, change them here.

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

Each section reads the previous section's artifact from disk and writes its own. That lets you build and test one section at a time. For example, Section 2 can be developed against a saved `01_raw_evidence.json` without re-running any searches.

## 2. Ground rules

- **Consent gate.** The pipeline refuses to start unless the capture bundle says the participant consented. Test only on team members.
- **Mock first.** Every test and every default run uses the mock internet in `mock_internet/`. SerpApi is called only when a run is started with `--live`, and tests can't turn that on.
- **Code holds the data; models make judgments.** Tools fetch and store pages. The Strands LLM returns choices and short notes that refer to stored records by URL or ID. It never retypes page content, and it never produces a confidence number.
- **Every model step works without the model.** Each stage has a deterministic path that runs with no LLM and no Jev. Tests use it by default, and the demo falls back to it if a service is down. Jev had an outage in its first week.
- **Scraped data stays local.** `runs/` is gitignored. Images uploaded for a live search are deleted after the search.
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
    platforms.py              domain → platform, account-URL patterns, URL normalization
    scrape.py                 HTML → PageRecord (meta, Open Graph, JSON-LD, links)
    search/
      base.py                 SearchBackend protocol
      mock_backend.py         wraps MockInternetClient
      serpapi_backend.py      SerpApi yandex_images + live HTTP fetch + response cache
    hosting.py                live only: upload image, presigned URL, delete
    jev.py                    Jev wrapper with modes typesafe | openrouter | fake | off
    llm.py                    Strands model factory (Anthropic API or Bedrock)
    stage1_collect.py
    stage2_profiles.py
    stage3_verify.py
    stage4_summarize.py
  tests/
    fixtures/captures/        mock capture bundles: alex, sam, casey, unknown
    test_mock_internet.py     exists
    test_stage1.py … test_stage4.py, test_end_to_end.py
  runs/                       gitignored; one folder per run
  requirements.txt
```

### 0.2 Dependencies

The pipeline needs `strands-agents[anthropic]`, `typesafe-sdk`, `httpx`, `beautifulsoup4`, and `python-dotenv`. It also needs `boto3`, but only for live image hosting. Pydantic comes with Strands. This machine has Python 3.14; if any package won't install on it, use a Python 3.12 virtual environment.

### 0.3 Configuration

| Variable | Values | Default | Used by |
|---|---|---|---|
| `SEARCH_BACKEND` | `mock`, `serpapi` | `mock` | Section 1 |
| `MOCK_INTERNET_URL` | loopback URL | `http://127.0.0.1:8765` | mock backend |
| `MOCK_SCENARIO` | `default`, `ambiguous`, `stale`, `duplicates`, `empty`, `rate_limited`, `server_error` | `default` | mock backend |
| `SERPAPI_KEY` | key | none | serpapi backend |
| `IMAGE_BUCKET` + AWS credentials | S3 bucket name | none | `hosting.py` (live only) |
| `JEV_MODE` | `fake`, `typesafe`, `openrouter`, `off` | `fake` | Sections 2, 3 |
| `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` | key | none | `jev.py` |
| `LLM_MODE` | `off`, `anthropic`, `bedrock` | `off` | Sections 1, 2, 4 |
| `ANTHROPIC_API_KEY` | key | none | `llm.py` |
| `LLM_MODEL` | model ID | `claude-sonnet-5-5` | `llm.py` |

`config.py` loads `.env` with python-dotenv. If a mode is turned on without its key, it stops with a message that names the missing variable. `.env.example` lists every variable with no values.

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
  "consent": { "confirmed": true, "participant": "Johnny", "confirmed_at": "2026-10-10T14:01:50Z" },
  "images": [
    { "image_id": "img-1", "file": "img-1.jpg", "angle": "front", "yaw": 1.5,   "pitch": -2.0, "roll": 0.3 },
    { "image_id": "img-2", "file": "img-2.jpg", "angle": "left",  "yaw": -32.0, "pitch": 0.0,  "roll": 1.1 },
    { "image_id": "img-3", "file": "img-3.jpg", "angle": "right", "yaw": 30.5,  "pitch": 1.2,  "roll": -0.4 }
  ]
}
```

- Images are JPEG or PNG, at least 400 px on the short side, ideally cropped close to the face.
- `angle` is a label: `front`, `left`, `right`, `up`, or `down`. `yaw`, `pitch`, and `roll` are MediaPipe's angles in degrees and are optional.
- The pipeline accepts 1–8 images. **Proposal:** use X = 3 (front, left, right). Each image costs one SerpApi search, and the free plan allows 250 a month and 50 an hour.
- Mock bundles add `"mock_fixture": "alex-front"` to each image and don't need image files.
- If the teammates later prefer an HTTP handoff, this JSON becomes the request body with the images base64-encoded. Only `capture.py` changes.

```python
class Consent(BaseModel):
    confirmed: bool
    participant: str
    confirmed_at: datetime

class CaptureImage(BaseModel):
    image_id: str
    file: str | None = None           # required unless mock_fixture is set
    angle: Literal["front", "left", "right", "up", "down"]
    yaw: float | None = None
    pitch: float | None = None
    roll: float | None = None
    mock_fixture: str | None = None   # tests only

class CaptureBundle(BaseModel):
    session_id: str
    captured_at: datetime
    consent: Consent
    images: list[CaptureImage]        # 1–8
```

### 0.5 Run artifacts

Each run gets a folder `runs/<run_id>/` containing:

- `00_capture.json`
- `01_raw_evidence.json`
- `02_profiles.json`
- `03_verification.json`
- `04_final.json`
- `run_log.jsonl`, with one line per external call: backend, endpoint, status, and latency. Jev calls also record the returned `model` string and token usage.

`run_id` is `<session_id>-<4 random hex chars>`.

### 0.6 Done when

- [ ] `python3 -m pipeline run --capture tests/fixtures/captures/alex --until 0` validates the manifest and writes `00_capture.json`.
- [ ] The same command refuses a manifest with `"confirmed": false`, or with no `consent` block.
- [ ] Every example artifact in this spec round-trips through `contracts.py`.

## 4. Section 1: Per-image search and scrape → raw structured data

**Goal:** for each face image, find the pages it appears on and scrape them. Hand back one merged, deduplicated bundle of raw structured data that records which image found what.

**Diagram:** AWS Strands Agent, Reverse-image search tool, "Finds internet presence", raw structured data.

**In:** `00_capture.json`. **Out:** `01_raw_evidence.json` (`RawEvidenceBundle`).

### 1.1 Search backends: mock for tests, SerpApi for real runs

Both backends implement the same interface. Stage code never checks which one it has.

```python
class SearchBackend(Protocol):
    name: str                                             # "mock" or "serpapi"
    def image_ref(self, image: CaptureImage) -> str: ...  # what to search with
    def reverse_image(self, image_ref: str) -> dict: ...  # SerpApi-shaped response
    def fetch(self, url: str) -> FetchedPage: ...         # status, final URL, HTML or text, links
```

| | Mock (default) | Live (`--live`) |
|---|---|---|
| Image reference | `mock://<mock_fixture>` | presigned S3 URL from `hosting.py` |
| Reverse search | `MockInternetClient.reverse_image()` → `image_results` | `GET https://serpapi.com/search.json?engine=yandex_images&url=…&api_key=…` → `image_results` |
| Page fetch | `MockInternetClient.fetch()`: reads SQLite, no network | `httpx.get`, 10 s timeout, browser User-Agent, no cookies |
| Failure tests | `MOCK_SCENARIO`, `delay_ms` | real failures only |
| Cost | free | 1 SerpApi search per image, cached |

Rules:

- The mock's image results follow a subset of SerpApi's Yandex shape (`title`, `link`, `snippet`, `source`, `thumbnail`, `original_image`), so one normalizer handles both backends. It accepts `thumbnail` either as a string or as an object with a `link`. SerpApi extras such as `knowledge_graph` and `image_tags` go into `search_extras`.
- In mock mode, hit `link`s are local URLs. The page's identity is `mock_original_url`, and the local URL is kept only for fetching. `platforms.normalize()` also maps `http://127.0.0.1:8765/web/<host>/<path>` back to `https://<host>/<path>`. That way, links and JSON-LD `sameAs` values on mock pages compare equal to search results.
- The live backend caches every SerpApi response at `runs/cache/serpapi/<sha256 of image bytes>.json`, so re-running a capture costs nothing. `--refresh` skips the cache.
- **Tests can't reach SerpApi.** The serpapi backend refuses to build unless the CLI passed `--live`. The shared test base class asserts `SEARCH_BACKEND=mock`. The mock client already refuses non-loopback hosts.
- The first live use is a smoke test, not a full run. `python3 -m pipeline smoke-search --image <one team member's photo> --live` uploads one image, runs the search, prints the hits, and reports the searches left on the plan. Run it once with the default tab and once with `tab=about`, then keep whichever tab returns pages where the face appears. That costs 2 searches.

### 1.2 Image hosting (live only, **Proposal**)

SerpApi's Yandex engine accepts only a public image `url`. `hosting.py` does the following:

1. Uploads each image to a private S3 bucket under a random key.
2. Returns a presigned GET URL that is valid for 10 minutes.
3. Deletes the object once the search returns, and again on `purge`.

Mock mode skips hosting.

### 1.3 Per-image agent

Each image gets its own Strands `Agent`, created fresh for that image; agents keep conversation state, so they can't be shared. All of them run at once:

```python
results = await asyncio.gather(
    *(collect_for_image(img, backend, page_store) for img in capture.images),
    return_exceptions=True,
)
```

A failed image becomes an `ImageEvidence` with an error entry. It never stops the other images.

The tools are built per image with closures, so each agent can touch only its own image:

| Tool | What it does | Limit |
|---|---|---|
| `reverse_image_search()` | Runs the reverse search on this agent's image. It takes no arguments, so it can't search other images or URLs. | 1 call |
| `fetch_page(url)` | Fetches and scrapes a page into a `PageRecord` and stores it in the shared page store. Returns a short summary: platform, kind, title, display name, first 300 characters, and outbound account links. | 12 fetches per image |
| `follow_account_links(url)` | Fetches the links on an already-fetched profile page that match an account-URL pattern in `platforms.py`. Goes one level deep only. | shares the 12 |

The shared page store is a dict keyed by normalized URL and guarded by a lock. When two images find the same page, it's fetched once and both images are recorded in its `found_via`.

The agent's job is triage. Real Yandex results are noisy (lookalikes, stock photos, Pinterest boards). The agent decides three things:

- which hits look like pages about this person,
- which of those to fetch,
- which profiles' account links to follow.

Its structured output contains only those judgments:

```python
class PageJudgment(BaseModel):
    url: str
    relevant: bool
    note: str = ""          # one sentence, e.g. "profile; photo matches the search image"

class ImageTriage(BaseModel):
    judgments: list[PageJudgment]
```

Code builds `ImageEvidence` from the page store plus these judgments. Judgments for URLs the tools never fetched are dropped.

The system prompt tells the agent:

- It has one face image of a consenting participant.
- Run the search once.
- Fetch the hits most likely to be this person's own profiles or posts, or articles about them.
- Follow account links from profiles.
- Skip shopping, stock, and lookalike pages.
- Stop when the budget is spent or nothing useful is left.

**Deterministic path** (`LLM_MODE=off`, which tests use):

1. Search.
2. Fetch every hit in rank order, up to the budget.
3. Follow account links from every profile page.
4. Mark every page that returned 200 as relevant.

On the mock this finds the same pages the agent should, so tests are stable and free.

Strands notes:

- Call `await agent.invoke_async(prompt, structured_output_model=ImageTriage)` and read `result.structured_output`. The older `agent.structured_output()` method is deprecated.
- Keep output models loose, with optional fields and short lists. A reported Strands bug (sdk issue #4482) retries failed structured-output validation without a limit, so wrap each agent call in `asyncio.wait_for(…, 90)`.
- Check that blocking tool I/O doesn't make the agents run one after another (the `delay_ms` test in 1.8 catches this). If it does, make the tools `async def` or wrap the client calls in `asyncio.to_thread`.

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

- `profile` if the URL matches the platform's account pattern, or JSON-LD says `Person`. Example patterns: `linkedin.com/in/<handle>`, `chatter.test/<handle>`, `fieldnotes.test/@<handle>`.
- `post` if the URL matches a post pattern, such as `chatter.test/<handle>/status/<id>`.
- `article` otherwise.

**Live caveat.** LinkedIn, Facebook, and Instagram usually show scripts a login wall. When a fetch fails or returns a login page, keep the search hit's title and snippet as the page's evidence and set `extraction="snippet"`. Don't drop the page. Hit titles often contain the display name, as in "Jane Doe - Software Engineer - Atlas LLC | LinkedIn".

### 1.5 Merging across images (**Proposal**)

Merge and deduplicate at the end of Section 1, before Jev sees anything. Pages are keyed by canonical URL, or by normalized URL when there is no canonical link. Each page keeps `found_via`: the images and hit positions that led to it. The number of different angles that found a page is one of the strongest signals in Section 3, so it must survive the merge.

### 1.6 Contracts

```python
class FoundVia(BaseModel):
    image_id: str
    hit_position: int | None      # rank in that image's results; None if reached by a followed link
    depth: int                    # 0 = search hit, 1 = followed from a profile
    parent_url: str | None = None

class LinkRef(BaseModel):
    url: str                      # normalized
    label: str = ""

class PageRecord(BaseModel):
    url: str                      # normalized original URL
    canonical_url: str | None = None
    fetched_url: str
    status_code: int | None       # None when only the search snippet is known
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

class FetchError(BaseModel):
    url: str | None               # None when the search itself failed
    status_code: int | None
    reason: str                   # not_found, forbidden, rate_limited, server_error, timeout, login_wall

class ImageEvidence(BaseModel):
    image_id: str
    angle: str
    backend: Literal["mock", "serpapi"]
    request_id: str               # search_metadata.id
    searched_at: datetime
    hits: list[ImageHit]
    page_urls: list[str]          # pages this image led to, after triage
    search_extras: dict = {}
    errors: list[FetchError] = []

class RawEvidenceBundle(BaseModel):
    run_id: str
    num_images: int
    images: list[ImageEvidence]
    pages: dict[str, PageRecord]  # keyed by canonical URL
```

### 1.7 Errors and retries

- **429 and 503:** wait as long as `Retry-After` says, retry twice, then record a `FetchError` and continue.
- **404 and 403 on fetch:** record the error and don't retry. The mock always returns 404 for Alex's deleted post and 403 for Sam's private album.
- **Timeouts:** each request gets 10 s. A timeout is recorded as `timeout`.
- **Zero hits on every image:** Section 1 still writes a valid bundle, and Sections 2–4 produce a no-match result.

### 1.8 Done when (mock)

Using `tests/fixtures/captures/alex`, whose images map to `alex-front`, `alex-left`, and `alex-right`, on the deterministic path:

- [ ] 3 reverse-image requests return 13 hits total, with 10 unique URLs. That means exactly 10 fetches, because the shared page store prevents duplicates.
- [ ] After the canonical merge there are 9 pages: the syndicated copy merges into the Riverbend News interview.
- [ ] There is one 404 `FetchError` for `fieldnotes.test/@alexbuilds/old-project`, and the run continues.
- [ ] There are 5 profile pages (Alex's LinkUp, Chatter, Photogram, CodeForge, and FieldNotes), each with `found_via` filled in. `chatter.test/alexbuilds` lists both `img-1` and `img-2`.

Other cases:

- [ ] `tests/fixtures/captures/sam` (`sam-front`): following account links adds Sam's CodeForge and FieldNotes profiles at depth 1, and the private album records a 403.
- [ ] `MOCK_SCENARIO=rate_limited`: each image retries as `Retry-After` says, then ends with a `rate_limited` error. No exception escapes.
- [ ] `delay_ms=1000`: three images take about as long as one, not three times as long.
- [ ] `unknown` fixture: no hits and a valid bundle.
- [ ] Optional, costs LLM calls (`LLM_MODE=anthropic`): on `alex`, the agent path marks the same 5 profiles relevant.

**Live check**, once the mock tests pass: smoke-search on one team member's photo returns at least one page that is really theirs.

## 5. Section 2: Parse, build profiles, connection graph (Jev)

**Goal:** turn raw pages into accounts ("LinkedIn: John Doe"), group the accounts into candidate people, and build a link graph with trust values for each candidate.

**Diagram:** Typesafe Jev, "Parses name and general bio", Builds Structured Profiles, Graph.

**In:** `01_raw_evidence.json`. **Out:** `02_profiles.json` (`ProfilesBundle`).

**Where Jev fits (Proposal).** Jev answers typed questions with probabilities and can't return text, so it can't read a name off a page. Extraction (2a) uses the structured data the scraper already pulled out, with the LLM as a fallback. Jev makes the grouping decisions in 2b: do these two accounts belong to the same person?

### 2a. Extract accounts

For each relevant `profile` page, build an `Account`. Use these sources in priority order:

1. JSON-LD `Person`: `name`, `description`, `worksFor`, `homeLocation`, `sameAs`.
2. Open Graph and meta description.
3. Title patterns, such as `"<name> | <site>"` and `"<name> - <role> - <company> | LinkedIn"`.
4. The URL handle, from the platform's account pattern.
5. LLM fallback, only when `LLM_MODE` is on: one call for each page that still has no name after steps 1–4. It uses `structured_output_model=AccountFields` and sees only that page's title, meta tags, and first 2,000 characters.

Posts and articles become `Mention`s:

- A post is tied to its author's account through its "Author" link or the handle in its URL.
- An article keeps the names it mentions and the account URLs it links to.

Mentions don't form profiles. They carry image evidence to the accounts they point at (see `image_support` in Section 3), and they give Section 4 dated workplace evidence.

The printable form matches the diagram:

```
LinkedIn: Alex Morgan
X: Alex M.
Instagram: Alex M.
GitHub: Alex Morgan
Blog: Alex Morgan
```

### 2b. Group accounts into candidate profiles

1. **Deterministic merges.** Join two accounts when each links to the other, through page links or `sameAs`.
2. **Jev for the remaining pairs.** One Jev request covers every remaining pair if it fits in Jev's 32K-token context; otherwise split it across several requests. The state lists every account by ID, and each pair is one `noul` question:

```python
state = {
    "accounts": {
        "a1": {"platform": "linkedin", "name": "Alex Morgan", "handle": "alexbuilds",
               "workplace": "Prairie Atlas Labs", "location": "Kansas City, MO",
               "bio": "I build open-source transit maps ...",
               "links_to": ["a2", "a4"], "found_by_images": ["img-1"]},
        # ...
    }
}
questions = {
    "same_a1_a3": Noul(
        instructions="Do accounts a1 and a3 belong to the same real person?",
        criteria={"true": "Same person: consistent name, work, or location, or one links to the other.",
                  "false": "Different people, even if the names match."}),
    # ... one per pair
}
```

3. **Join on threshold.** Join pairs with probability ≥ 0.70 using union-find. The threshold is a starting value; tune it on the mock cases.
4. **Name the profile.** Use its most common display name. On a tie, use the LinkedIn account's name.

**Fallback without Jev** (`JEV_MODE=fake` or `off`). Two accounts are the same person if any of these is true:

- they link to each other;
- one links to the other, and the names are compatible;
- neither links to the other, the names are compatible, and they share a workplace or a location.

Names are compatible when the first names are equal or one is a prefix of the other, and the last names are equal or one is the other's initial. So "Alex M." matches "Alex Morgan", and "Sam" matches "Sammy".

In `fake` mode, `jev.py` returns these rule results as probabilities of 0.95 or 0.05. That way tests run the same code path as live Jev.

Every Jev request is logged in `jev_calls` with the returned `model` string, token usage, and each question's probability.

### 2c. Connection graph and trust

The graph is built once per candidate profile, as in the diagram:

1. **Sources** are the profile's accounts whose pages were fetched.
2. **Targets.** A source's targets are its outbound links that match an account-URL pattern on any platform, excluding itself.
3. **Count.** For each target, `count` is the number of distinct sources in this profile that link to it.
4. **Trust** comes from `count` (**Proposal**, built to match the diagram's two data points; configurable in `config.py`):

| Connections | Trust |
|---|---|
| 0 | 0 |
| 1 | 50 |
| 2 | 95 |
| 3 or more | 99 |

5. **Target status.** Each target is labeled `in_profile`, `other_profile` (with that profile's ID), or `not_collected`.

A target in another profile with trust ≥ 95 suggests the two profiles are one person. That's the diagram's case: two of John Doe's accounts link to a Facebook account named Johnny Appleseed. These links are recorded as `cross_profile` flags, and Section 3 shows them. Profiles are not merged automatically; re-merging on this signal is a stretch goal.

Two profile-level numbers feed Section 3:

- `mean_link_trust`: the mean trust over the profile's own accounts, where an account nobody links to counts as 0.
- `internal_link_density`: the number of links between the profile's accounts, divided by n(n−1).

### 2d. Contracts

```python
class Account(BaseModel):
    account_id: str                 # "linkedin:alexbuilds"
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
    image_ids: list[str] = []       # images that found this page, or found a post or article pointing at it
    best_hit_position: int | None = None
    depth: int
    extraction: Literal["json_ld", "meta", "title", "llm", "snippet"]

class Mention(BaseModel):
    url: str
    kind: Literal["post", "article"]
    author_account_id: str | None = None
    names: list[str] = []
    links_to: list[str] = []
    image_ids: list[str] = []
    published_at: str | None = None

class PairDecision(BaseModel):
    a: str
    b: str
    probability: float
    method: Literal["mutual_link", "jev", "rule"]

class CandidateProfile(BaseModel):
    profile_id: str                 # "p1", "p2", ...
    name: str
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

class ProfilesBundle(BaseModel):
    run_id: str
    accounts: dict[str, Account]
    mentions: list[Mention]
    profiles: list[CandidateProfile]
    graphs: dict[str, ProfileGraph]
    jev_calls: list[dict]
```

### 2e. Done when (mock)

- [ ] **Diagram parity** (unit test with hand-built accounts): if LinkedIn links to Facebook and GitHub, and Instagram links to Facebook and Twitter, the result is Facebook 2/95, GitHub 1/50, Twitter 1/50.
- [ ] **`alex`, default:** 5 accounts, with the display names printed in 2a, and one profile "Alex Morgan" containing all 5. LinkedIn, X, GitHub, and Blog each have 2 connections (trust 95); Instagram has 0. `mean_link_trust` = 76.
- [ ] **`alex`, ambiguous:** two profiles named Alex Morgan, one Kansas City and one Portland.
  - Kansas City is `alexbuilds`, with 5 accounts.
  - Portland is `alexframes`: LinkUp, Chatter, and FieldNotes, plus CodeForge at depth 1.
  - No pair across the two profiles is merged.
- [ ] **`sam`:** one profile containing both "Sam Rivera" (LinkUp, CodeForge, FieldNotes) and "Sammy Rivera" (Chatter, Photogram). This is the alias case.
- [ ] **`casey`:** one profile with 2 accounts, each with 1 connection (trust 50).
- [ ] **Interview link:** the Riverbend interview links to Sam's LinkUp page, but Sam doesn't appear in Alex's run, because articles aren't followed.
- [ ] **Optional real-Jev check** (costs Jev calls, `JEV_MODE=typesafe`): on `alex` with `ambiguous`, cross-person pairs come back below 0.5, and "Alex M." / "Alex Morgan" pairs come back at 0.7 or higher. Set the threshold from the real numbers.

## 6. Section 3: Verification and confidence

**Goal:** give each candidate profile a confidence that it's the person in front of the camera, accept at most one, and record why.

**Diagram:** Verification/Synthesize Pass → "John Doe 94% ✓, Johnny Appleseed 30% ✗, Jane Doe 2% ✗".

**In:** `01_raw_evidence.json`, `02_profiles.json`. **Out:** `03_verification.json`.

**Who runs it (Proposal).** One synthesis agent starts after all the per-image agents finish, and it runs both Section 3 and Section 4. Section 3's numbers come from code and Jev, through tools. The agent only adds a one-sentence rationale per candidate.

### 3.1 Signals per candidate (computed in code)

| Signal | Definition | Why it matters |
|---|---|---|
| `image_support` | Share of the X images that found any page of this profile: its accounts, their posts, or an article linking to one of its accounts | Matches from several angles are the strongest face evidence |
| `top_hit_share` | Share of images whose highest-ranked hit belongs to this profile | Separates the real person from same-name profiles further down the results |
| `mean_link_trust` | From 2c, 0–100 | Accounts that vouch for each other |
| `platform_count` | Number of distinct platforms in the profile | A wider footprint |
| `consistency` | Share of accounts whose workplace matches the profile's most common workplace. With Jev on, a Jev `score` question instead (levels: contradictory, mixed, mostly consistent, consistent), scaled to 0–1. | Catches strangers merged into one profile. Stale employers lower it slightly. |
| `name_collision` | Another candidate has a compatible name | Flags the two-Alex case |
| `newest_update` | Latest `updated_at` | Used by Section 4 to pick the current workplace |

### 3.2 Confidence

**Primary method (Jev on, Proposal).** Make one Jev request. The state holds every candidate's account summary and signals, plus the number of images. It asks two kinds of question:

- `match_<profile_id>`, a `noul`: "Is candidate <id> the person whose face was searched, given the reverse-image evidence and account links?" The probability is that candidate's confidence.
- `best`, a `choice` over the candidate IDs plus `none`: "Which candidate, if any, is the searched person?" This is a cross-check.

**Fallback formula.** It's used when `JEV_MODE` is `fake` or `off`, and it's always computed for comparison:

```
z = −4 + 2·image_support + 3·top_hit_share + 1.5·(mean_link_trust / 100)
       + 0.3·min(platform_count, 5) + 1·consistency − 0.5·name_collision
confidence = 1 / (1 + e^(−z))
```

The weights are starting values. On the mock cases they give:

| Case | Candidate | Confidence |
|---|---|---|
| `alex` default | Alex Morgan (Kansas City) | 0.99 |
| `alex` ambiguous | Alex Morgan (Kansas City) | 0.98 |
| `alex` ambiguous | Alex Morgan (Portland) | 0.69 |
| `sam` | Sam Rivera | 0.99 |
| `casey` | Casey Brooks | 0.97 |

`top_hit_share` is the signal that separates the two Alexes. The ambiguous scenario returns the photographer's pages for all three angles, so `image_support` alone scores them almost the same.

**Decision rule:**

- Accept the top candidate if its confidence is ≥ 0.80 and it leads the runner-up by ≥ 0.20.
- Accept at most one candidate.
- If Jev's `best` answer is `none` with probability ≥ 0.5, accept nobody.
- If Jev and the fallback differ by more than 0.4 for a candidate, keep Jev's number and flag the candidate.

### 3.3 Contract

```python
class CandidateSignals(BaseModel):
    image_support: float
    top_hit_share: float
    mean_link_trust: float
    platform_count: int
    consistency: float
    name_collision: bool
    newest_update: str | None = None

class CandidateVerdict(BaseModel):
    profile_id: str
    name: str
    confidence: float               # 0–1, shown as a percentage
    fallback_confidence: float
    method: Literal["jev", "fallback"]
    decision: Literal["accepted", "rejected"]
    signals: CandidateSignals
    flags: list[str] = []           # e.g. "jev_fallback_disagree", "cross_profile_link"
    rationale: str | None = None    # one sentence from the agent; never used for scoring
    jev_model: str | None = None

class VerificationResult(BaseModel):
    run_id: str
    verdicts: list[CandidateVerdict]   # highest confidence first
    accepted_profile_id: str | None
    reason: Literal["accepted", "below_threshold", "too_close", "jev_none", "no_candidates"]
```

### 3.4 Done when (mock)

- [ ] **`alex` default:** Kansas City Alex is accepted.
- [ ] **`alex` ambiguous:** Kansas City Alex is accepted and Portland Alex is rejected. The photographer must never be accepted, whatever the weights.
- [ ] **`alex` stale:** Kansas City Alex is accepted, and the archived 2021 bio doesn't become a separate candidate.
- [ ] **`sam`:** Sam Rivera is accepted, with both name forms in one profile.
- [ ] **`unknown`:** the result is `reason="no_candidates"`, and no Jev or LLM calls are made.
- [ ] **Jev unavailable** (simulated with an exception): the run finishes on the fallback with `method="fallback"`.
- [ ] **Optional, real Jev:** record Jev's number next to the fallback's for every case, then set the thresholds from them.

## 7. Section 4: Summarization

**Goal:** write the final profile (name, workplace, bio) for the accepted candidate. If there is none, return a clear "no confident match".

**Diagram:** Summarization → Profile (Name, Workplace, Bio).

**In:** `02_profiles.json`, `03_verification.json`. **Out:** `04_final.json`.

### 4.1 Behavior

- **Accepted candidate.** The synthesis agent receives that profile's accounts and mentions and returns a `FinalProfile` through `structured_output_model`. Every field lists the source URLs it came from.
- **Citation check.** Code checks that every cited URL is in the evidence. A field with an unknown or missing source is dropped, and the agent is asked once more. If the answer is still wrong, the deterministic template is used instead.
- **Workplace.** The most recently dated source wins. Alex's 2022 Photogram profile and 2021 archived bio both say Old Mill Software, but the current answer is Prairie Atlas Labs.
- **Bio.** At most 3 sentences, built from the person's own bios. It must stay within the sources and leave out the categories excluded in the ground rules.
- **Deterministic template** (`LLM_MODE=off`):
  - name: the profile name
  - workplace and role: from the newest account that has them
  - location: the most common one
  - bio: the LinkedIn bio, or else the longest bio
- **No accepted candidate.** Return `NoMatch` with the top three candidates and their confidences.

### 4.2 Contract

```python
class Sourced(BaseModel):
    value: str
    sources: list[str]

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
    bio: Sourced
    accounts: list[FinalAccount]
    confidence: float
    generated_at: datetime

class NoMatch(BaseModel):
    status: Literal["no_match"] = "no_match"
    run_id: str
    reason: str
    top_candidates: list[dict]        # {"name": ..., "confidence": ...}
```

Example output for `alex` on the mock, LLM path (abridged):

```json
{
  "status": "match",
  "name":      { "value": "Alex Morgan", "sources": ["https://linkup.test/in/alexbuilds"] },
  "role":      { "value": "Software engineer", "sources": ["https://linkup.test/in/alexbuilds"] },
  "workplace": { "value": "Prairie Atlas Labs",
                 "sources": ["https://linkup.test/in/alexbuilds", "https://riverbend-news.test/people/alex-morgan-transit"] },
  "location":  { "value": "Kansas City, MO", "sources": ["https://linkup.test/in/alexbuilds"] },
  "bio":       { "value": "Software engineer at Prairie Atlas Labs who builds open-source transit maps and volunteers at the Riverbend maker club.",
                 "sources": ["https://linkup.test/in/alexbuilds"] },
  "confidence": 0.99
}
```

### 4.3 Done when (mock)

- [ ] **`alex` default and stale:** the workplace is Prairie Atlas Labs, not Old Mill Software.
- [ ] **`sam`:** the name is "Sam Rivera" and the workplace is Mosaic Kite Design.
- [ ] **`unknown`, and cases where every candidate is rejected:** the result is `NoMatch`.
- [ ] **Sources:** every `sources` URL in the output is a page in `01_raw_evidence.json`.
- [ ] **Optional, with the LLM on:** the same checks pass when the agent writes the bio.

## 8. Section 5: End-to-end run and demo

### 5.1 CLI

```bash
# Mock (default). Start the mock internet in another terminal first.
python3 -m mock_internet serve
python3 -m pipeline run --capture tests/fixtures/captures/alex
python3 -m pipeline run --capture tests/fixtures/captures/alex --scenario ambiguous

# Re-run one stage from a saved run
python3 -m pipeline stage 2 --run runs/<run_id>

# Live: real SerpApi and real pages
python3 -m pipeline smoke-search --image path/to/face.jpg --live
python3 -m pipeline run --capture captures/<session_id> --live

# Delete a run's data and any uploaded images
python3 -m pipeline purge --run runs/<run_id>
```

As it runs, `run` prints each of the diagram's views in turn:

1. hits per image
2. the parsed account list
3. the profiles
4. each profile's trust table
5. the verdicts with percentages
6. the final profile

### 5.2 Tests

`python3 -m unittest discover -s tests -v` runs everything without paid calls. The tests start their own mock server on a free port, the way `tests/test_mock_internet.py` already does, and run with `JEV_MODE=fake` and `LLM_MODE=off`. Paid tests run only when `RUN_PAID_TESTS=1` is set.

### 5.3 Fallbacks for the demo

| If this is down | The pipeline does this |
|---|---|
| SerpApi (quota or outage) | Uses the cached response for that image. With no cache, that image fails and the rest continue. |
| Jev | Uses the fallback rules (2b) and formula (3.2) and marks results `method="fallback"` |
| Claude or Bedrock | Uses deterministic triage, extraction, and the template summary |
| A page fetch | Keeps the search hit's title and snippet in place of the page |
| Everything external | Run the mock demo |

### 5.4 Time budget (X = 3, live)

These are targets, not measurements. Check them against `run_log.jsonl`.

| Step | Target |
|---|---|
| Upload and reverse search (parallel) | ≤ 10 s |
| Fetch and triage (parallel, ≤ 12 pages per image) | ≤ 25 s |
| Extraction and one Jev grouping call | ≤ 5 s |
| Verification (one Jev call) and summary (one LLM call) | ≤ 10 s |
| Total | ≤ 50 s |

### 5.5 Done when

- [ ] The end-to-end mock test passes for every row of the test matrix below.
- [ ] One live run on a consenting team member returns the right person, or a clear no-match, in under a minute.
- [ ] `purge` removes the run folder and its S3 objects.

## 9. Mock test matrix

| Capture | Scenario | Section 1 | Section 2 | Section 3 | Section 4 |
|---|---|---|---|---|---|
| alex (front, left, right) | default | 13 hits → 10 URLs → 9 pages; one 404 | 1 profile, 5 accounts | accepted, 0.99 | Prairie Atlas Labs |
| alex | ambiguous | photographer pages under every angle | 2 Alex profiles, not merged | Kansas City accepted; Portland rejected | Kansas City Alex |
| alex | stale | old bio and deleted post under every angle | 1 profile | accepted | newest workplace wins |
| alex | duplicates | repeated hits and the mirror removed | 1 profile | accepted | same as default |
| alex | rate_limited, server_error | errors recorded after retries | no accounts | `no_candidates` | `NoMatch` |
| sam (front) | default | 403 private album; 2 accounts added at depth 1 | 1 profile, Sam + Sammy | accepted, 0.99 | Mosaic Kite Design |
| casey (front) | default | 2 hits | 1 profile, trust 50/50 | accepted, 0.97 | Blue Finch Music School |
| unknown | default | 0 hits | none | `no_candidates` | `NoMatch` |

## 10. Decisions to confirm

| Open question | What this spec assumes | Who decides |
|---|---|---|
| Capture handoff format and X | A folder with `manifest.json`; X = 3 | Johnny and teammates |
| Jev key: TypeSafe or OpenRouter | `jev.py` supports both; pick with `JEV_MODE` | Johnny |
| What Jev decides | Grouping (2b) and confidence (3), plus an optional consistency score | Johnny |
| Merge per-image results before or after Jev | Before, at the end of Section 1, keeping per-image provenance | Johnny |
| Which agent runs verification and summary | One synthesis agent after the per-image agents | Johnny |
| Image hosting for Yandex | Private S3 bucket, 10-minute presigned URL, deleted after the search | Johnny |
| Trust formula | Table: 0 → 0, 1 → 50, 2 → 95, 3+ → 99 | Johnny |
| Trust → confidence | Jev `noul` per candidate; fallback formula in 3.2 | Johnny |
| Strands model | Claude Sonnet 5.5 through the Anthropic API (`claude-sonnet-5-5`). Use Bedrock instead if the team has AWS model access. Haiku 4.5 (`claude-haiku-4-5-20251001`) is a cheaper option for the per-image agents. | Johnny |
| Where the final profile is shown | `04_final.json` and the console; no UI decided yet | Team |

## 11. Build checklist

Section 0

- [ ] `requirements.txt`, the `pipeline/` package, `config.py`, and new `.env.example` entries
- [ ] `contracts.py` with every model in this spec
- [ ] `capture.py` with the consent gate, plus mock capture bundles for alex, sam, casey, and unknown
- [ ] `artifacts.py` and `run_log.jsonl`
- [ ] Send the capture contract (0.4) to the teammates

Section 1

- [ ] `platforms.py`: domain table, account and post URL patterns, URL normalization (including mock local URLs)
- [ ] `scrape.py`, tested against mock pages
- [ ] `search/mock_backend.py`
- [ ] Deterministic `collect_for_image` and the cross-image merge; Section 1 mock tests
- [ ] Strands per-image agent with its tools and `ImageTriage` output
- [ ] `hosting.py` and `search/serpapi_backend.py` with the cache; smoke-search on one team member

Section 2

- [ ] Account and mention extraction
- [ ] `jev.py`: `fake` mode first, then the real key
- [ ] Grouping: mutual links, Jev pair questions, union-find
- [ ] Graph and trust table; diagram parity test
- [ ] Section 2 mock tests; record real Jev probabilities and set the threshold

Section 3

- [ ] Signals, fallback formula, and decision rule
- [ ] Jev confidence call and the disagreement flag
- [ ] Section 3 mock tests

Section 4

- [ ] Deterministic template and `NoMatch`
- [ ] Synthesis agent with sourced fields and the citation check
- [ ] Section 4 mock tests

Section 5

- [ ] CLI (`run`, `stage`, `smoke-search`, `purge`) and console output
- [ ] End-to-end mock test for every matrix row
- [ ] First live run on a team member
