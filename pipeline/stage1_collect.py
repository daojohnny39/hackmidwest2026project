"""Section 1: per-image reverse-image search and scrape -> 01_raw_evidence.json (spec 1.3-1.7).

Each image gets its own ImageCollector. It can fetch only its own search hits
and the account links of profiles found directly in its search (depth 1), with
a budget of 12 distinct URLs. One PageStore per run fetches each normalized URL
once, even when several images ask for it at the same moment, and failures are
shared too. Every requesting image still records its own provenance.

With LLM_MODE=off the collector runs the deterministic path: search, fetch every
hit in rank order, then follow account links from depth-0 profiles in URL order.
Otherwise each image gets a fresh Strands agent (image_agent.py). If the agent
fails, deterministic triage continues over the stored results; a completed search
is never repeated. A per-image deadline bounds agent work and retries alike.

build_bundle() then merges across images (1.5). A page whose validated canonical
link names another collected non-profile page folds into it. url_aliases maps
every original URL to its pages key. Only images that retained a page count as
its support.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline.artifacts import log_event, read_artifact, write_artifact
from pipeline.config import ConfigError, Settings
from pipeline.contracts import (
    CaptureArtifact, CaptureImage, FetchedPage, FetchError, FoundVia, ImageEvidence, ImageHit, ImageTriage,
    PageJudgment, PageRecord, RawEvidenceBundle, validate_references,
)
from pipeline.platforms import account_handle, normalize_url, page_kind, platform_for
from pipeline.scrape import Scraped, scrape_html
from pipeline.search.base import BackendError, SearchBackend, parse_image_results, with_retries

MAX_FETCHES_PER_IMAGE = 12
IMAGE_CONCURRENCY = 3  # Proposal (1.3): at most three images at once
FETCH_CONCURRENCY = 4  # per image
IMAGE_DEADLINE_S = 35.0  # spec 5.4

Log = Callable[..., None]
AgentRunner = Callable[["ImageCollector"], Awaitable[ImageTriage]]


def _no_log(**_: object) -> None:
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PageResult:
    """The shared outcome of fetching one normalized URL."""

    url: str
    page: FetchedPage | None = None
    scraped: Scraped | None = None
    error: FetchError | None = None

    @property
    def ok(self) -> bool:
        return self.scraped is not None

    @property
    def kind(self) -> str:
        return page_kind(self.url, person_json_ld=bool(self.scraped and self.scraped.person))


class PageStore:
    """Fetches each normalized URL once per run. Concurrent requests share one task."""

    def __init__(self, backend: SearchBackend, *, log: Log = _no_log):
        self.backend = backend
        self.log = log
        self._tasks: dict[str, asyncio.Task[PageResult]] = {}

    @property
    def fetch_count(self) -> int:
        return len(self._tasks)

    async def get(self, url: str, *, deadline: float) -> PageResult:
        task = self._tasks.get(url)
        if task is None:
            task = asyncio.ensure_future(self._fetch(url, deadline))
            self._tasks[url] = task
        # One image timing out must not cancel a fetch another image is waiting on.
        return await asyncio.shield(task)

    async def _fetch(self, url: str, deadline: float) -> PageResult:
        started = time.monotonic()
        endpoint = getattr(self.backend, "fetch_endpoint", "page-fetch")
        try:
            page, retries = await with_retries(lambda: self.backend.fetch(url), deadline=deadline)
            scraped = await asyncio.to_thread(scrape_html, page.html, page_url=url, fetched_url=page.fetched_url,
                                              mock_origin=self.backend.origin)
        except BackendError as error:
            self.log(backend=self.backend.name, endpoint=endpoint, status=error.status_code or error.reason,
                     latency_ms=_ms(started), retries=error.retries, code=error.reason)
            return PageResult(url, error=FetchError(url=url, status_code=error.status_code, reason=error.reason))
        except Exception:  # a scraper or adapter bug fails this page only
            self.log(backend=self.backend.name, endpoint=endpoint, status="error", latency_ms=_ms(started),
                     code="internal_error")
            return PageResult(url, error=FetchError(url=url, status_code=None, reason="internal_error"))
        self.log(backend=self.backend.name, endpoint=endpoint, status=page.status_code, latency_ms=_ms(started),
                 retries=retries)
        return PageResult(url, page=page, scraped=scraped)

    def pending(self) -> list[asyncio.Task]:
        return [task for task in self._tasks.values() if not task.done()]


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


@dataclass
class SearchOutcome:
    hits: list[ImageHit] = field(default_factory=list)
    request_id: str | None = None
    extras: dict = field(default_factory=dict)
    error: FetchError | None = None


@dataclass
class Request:
    """One distinct URL an image asked for; counts against its budget even when shared or failed."""

    url: str
    via: FoundVia
    result: PageResult | None = None


class NotAllowed(Exception):
    """The URL is neither this image's search hit nor a permitted depth-1 account link."""


class ImageCollector:
    """Search and fetch state for one image. The agent's tools and the deterministic path both use it."""

    def __init__(self, image: CaptureImage, backend: SearchBackend, store: PageStore, *,
                 budget: int = MAX_FETCHES_PER_IMAGE, log: Log = _no_log):
        self.image = image
        self.backend = backend
        self.store = store
        self.budget = budget
        self.log = log
        self.deadline = math.inf  # time.monotonic() value, set when collection starts
        self.started_at = _now()
        self.searched_at: datetime | None = None
        self.outcome: SearchOutcome | None = None
        self.requests: dict[str, Request] = {}  # in request order
        self.judgments: dict[str, PageJudgment] | None = None  # set by agent triage
        self.method = "deterministic"
        self.fallback_reason: str | None = None
        self.problem: str | None = None  # collection-level error code (timeout, internal_error)
        self.skipped = 0  # URLs not fetched because the budget was spent
        self._search_task: asyncio.Task[SearchOutcome] | None = None
        self._slots = asyncio.Semaphore(FETCH_CONCURRENCY)

    @property
    def image_id(self) -> str:
        return self.image.image_id

    # Search

    async def search(self) -> SearchOutcome:
        """Run this image's reverse search once; later calls return the stored outcome or error."""
        if self._search_task is None:
            self._search_task = asyncio.ensure_future(self._search())
        return await asyncio.shield(self._search_task)

    async def _search(self) -> SearchOutcome:
        self.searched_at = _now()
        started = time.monotonic()
        endpoint = getattr(self.backend, "search_endpoint", "search")
        try:
            payload, retries = await with_retries(lambda: self.backend.reverse_image(self.image), deadline=self.deadline)
            result = parse_image_results(payload, mock_origin=self.backend.origin)
            self.outcome = SearchOutcome(hits=result.hits, request_id=result.request_id, extras=result.extras)
            self.log(backend=self.backend.name, endpoint=endpoint, status=200, latency_ms=_ms(started), retries=retries)
        except BackendError as error:
            self.outcome = SearchOutcome(error=FetchError(url=None, status_code=error.status_code, reason=error.reason))
            self.log(backend=self.backend.name, endpoint=endpoint, status=error.status_code or error.reason,
                     latency_ms=_ms(started), retries=error.retries, code=error.reason)
        finally:
            # Hosted copies are deleted once the search and its retries are over, even on failure.
            await asyncio.shield(self.backend.release(self.image))
        return self.outcome

    def hit_for(self, url: str) -> ImageHit | None:
        """This image's best-ranked hit for a normalized URL."""
        hits = self.outcome.hits if self.outcome else []
        return next((hit for hit in hits if hit.url == url), None)

    # Fetch permissions and budget

    def account_links(self, url: str) -> list[str]:
        """Account links on one of this image's depth-0 profile pages, or [] if url isn't one."""
        request = self.requests.get(url)
        if request is None or request.via.depth != 0 or request.result is None or not request.result.ok:
            return []
        if request.result.kind != "profile":
            return []
        links = (link.url for link in request.result.scraped.outbound_links)
        return list(dict.fromkeys(link for link in links if account_handle(link) is not None and link != url))

    def via_for(self, url: str) -> FoundVia:
        """How this image may reach url: a search hit (depth 0) or a followed account link (depth 1)."""
        hit = self.hit_for(url)
        if hit is not None:
            return FoundVia(image_id=self.image_id, hit_position=hit.position, depth=0)
        for parent in self.requests:  # request order: hits in rank order first
            if url in self.account_links(parent):
                return FoundVia(image_id=self.image_id, hit_position=None, depth=1, parent_url=parent)
        raise NotAllowed(url)

    def reserve(self, urls: list[str]) -> list[Request]:
        """Register URLs in order against the budget; already-requested URLs cost nothing."""
        reserved = []
        for url in dict.fromkeys(urls):
            if url in self.requests:
                reserved.append(self.requests[url])
                continue
            via = self.via_for(url)
            if len(self.requests) >= self.budget:
                self.skipped += 1
                continue
            self.requests[url] = request = Request(url, via)
            reserved.append(request)
        return reserved

    async def fetch(self, urls: list[str]) -> list[Request]:
        """Fetch URLs this image may reach, within budget; returns the requests that were made."""
        requests = self.reserve(urls)
        await asyncio.gather(*(self._complete(request) for request in requests))
        return requests

    async def _complete(self, request: Request) -> None:
        if request.result is None:
            async with self._slots:
                request.result = await self.store.get(request.url, deadline=self.deadline)

    # Collection paths

    async def collect_deterministic(self) -> None:
        """Search; fetch every hit in rank order; follow account links from depth-0 profiles once."""
        outcome = await self.search()
        await self.fetch([hit.url for hit in outcome.hits])
        targets = [link for url in list(self.requests) for link in self.account_links(url) if link not in self.requests]
        await self.fetch(sorted(set(targets)))

    def apply_triage(self, triage: ImageTriage) -> None:
        """Keep the agent's judgments for URLs this image actually collected."""
        judgments: dict[str, PageJudgment] = {}
        for judgment in triage.judgments:
            url = normalize_url(judgment.url, mock_origin=self.backend.origin)
            if url in self.requests and url not in judgments:
                judgments[url] = judgment.model_copy(update={"url": url, "note": judgment.note[:300]})
        self.judgments = judgments

    def keeps(self, url: str) -> bool:
        """Whether this image retains a collected URL: always on the deterministic paths;
        on the agent path unless the agent judged it not relevant (fetching it was a choice)."""
        if self.judgments is None:
            return True
        judgment = self.judgments.get(url)
        return judgment is None or judgment.relevant


async def collect_image(collector: ImageCollector, *, agent: AgentRunner | None, deadline_s: float) -> None:
    """Run one image to completion. Never raises: failures become fallbacks or error records."""
    collector.deadline = time.monotonic() + deadline_s

    def remaining() -> float:
        return collector.deadline - time.monotonic()

    try:
        if agent is not None:
            try:
                triage = await asyncio.wait_for(agent(collector), timeout=max(remaining(), 0))
            except Exception as failure:  # timeouts, model errors, invalid structured output
                collector.method = "fallback"
                collector.fallback_reason = getattr(failure, "reason", None) or (
                    "timeout" if isinstance(failure, TimeoutError) else "agent_error")
                collector.log(code=f"fallback:{collector.fallback_reason}")
            else:
                collector.method = "agent"
                collector.apply_triage(triage)
                return
        if remaining() > 0:
            await asyncio.wait_for(collector.collect_deterministic(), timeout=remaining())
        elif collector.outcome is None:
            collector.problem = "timeout"
    except TimeoutError:
        collector.problem = "timeout"
    except Exception:
        collector.problem = "internal_error"
        collector.log(code="internal_error")


async def collect_evidence(
    capture: CaptureArtifact,
    backend: SearchBackend,
    *,
    agent: AgentRunner | None = None,
    log: Log = _no_log,
    image_deadline_s: float = IMAGE_DEADLINE_S,
    concurrency: int = IMAGE_CONCURRENCY,
) -> tuple[RawEvidenceBundle, PageStore]:
    """Collect every image in parallel (at most `concurrency` at once) and merge the evidence."""
    store = PageStore(backend, log=log)
    collectors = [ImageCollector(image, backend, store, log=log) for image in capture.capture.images]
    limit = asyncio.Semaphore(concurrency)

    async def run(collector: ImageCollector) -> None:
        async with limit:
            collector.started_at = _now()
            await collect_image(collector, agent=agent, deadline_s=image_deadline_s)

    results = await asyncio.gather(*(run(collector) for collector in collectors), return_exceptions=True)
    for collector, result in zip(collectors, results):
        if isinstance(result, BaseException):
            collector.problem = collector.problem or "internal_error"

    # Nothing may still be running once the bundle is built: stop leftover searches and fetches.
    leftovers = [c._search_task for c in collectors if c._search_task and not c._search_task.done()]
    leftovers += store.pending()
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)
    return build_bundle(capture.run_id, collectors), store


def build_bundle(run_id: str, collectors: list[ImageCollector]) -> RawEvidenceBundle:
    """Merge per-image evidence into one bundle (spec 1.5)."""
    html: dict[str, PageResult] = {}
    snippets: dict[str, tuple[ImageHit, Request]] = {}
    for collector in collectors:
        for url, request in collector.requests.items():
            result = request.result
            if result is None:
                continue
            if result.ok:
                html.setdefault(url, result)
            elif request.via.depth == 0 and url not in snippets:
                # A failed direct hit keeps only its public search title/snippet; never the error body.
                hit = collector.hit_for(url)
                if hit is not None and (hit.title or hit.snippet):
                    snippets[url] = (hit, request)
    collected = set(html) | set(snippets)

    key_of = {url: url for url in collected}
    for url, result in html.items():
        target = result.scraped.canonical_url
        if target and target != url and _valid_canonical(result, html.get(target)):
            key_of[url] = target

    support: dict[str, list[str]] = {}  # pages key -> images that retained it
    page_urls: dict[str, list[str]] = {}
    order: list[str] = []
    for collector in collectors:
        kept = []
        for url in collector.requests:
            if url not in collected:
                continue
            key = key_of[url]
            if key not in order:
                order.append(key)
            if collector.keeps(url) and key not in kept:
                kept.append(key)
                support.setdefault(key, []).append(collector.image_id)
        page_urls[collector.image_id] = kept

    pages: dict[str, PageRecord] = {}
    for key in order:
        sources = sorted((url for url in collected if key_of[url] == key), key=lambda url: (url != key, url))
        retaining = support.get(key)
        found_via, notes = [], []
        for collector in collectors:
            vias = [request.via for url, request in collector.requests.items()
                    if url in collected and key_of[url] == key]
            if not vias:
                continue
            note = _note(collector, [url for url in collector.requests if url in collected and key_of[url] == key])
            kept = collector.image_id in (retaining or [])
            if note or not kept:
                notes.append(f"{collector.image_id}{'' if kept else ' excluded'}" + (f": {note}" if note else ""))
            if retaining and not kept:
                continue  # an excluded observation stays visible in notes but is not support
            best = min(vias, key=lambda via: (via.depth, via.hit_position or 0, via.parent_url or ""))
            if best.parent_url is not None:
                best = best.model_copy(update={"parent_url": key_of.get(best.parent_url, best.parent_url)})
            found_via.append(best)
        pages[key] = _record(key, sources, html.get(key), snippets.get(key), found_via, notes, bool(retaining))

    images = [_evidence(collector, page_urls[collector.image_id]) for collector in collectors]
    return RawEvidenceBundle(run_id=run_id, num_images=len(images), images=images, pages=pages,
                             url_aliases={url: key_of[url] for url in sorted(collected)})


def _valid_canonical(page: PageResult, target: PageResult | None) -> bool:
    """Use a canonical hint only when it names a collected HTML page that is canonical to
    itself, and neither page is a profile: an untrusted tag never collapses accounts."""
    if target is None or not target.ok:
        return False
    if target.scraped.canonical_url not in (None, target.url):
        return False
    return page.kind != "profile" and target.kind != "profile"


def _note(collector: ImageCollector, urls: list[str]) -> str:
    if not collector.judgments:
        return ""
    return "; ".join(collector.judgments[url].note for url in urls if url in collector.judgments
                     and collector.judgments[url].note)


def _record(key: str, sources: list[str], result: PageResult | None, snippet: tuple[ImageHit, Request] | None,
            found_via: list[FoundVia], notes: list[str], relevant: bool) -> PageRecord:
    if result is not None:
        scraped = result.scraped
        return PageRecord(
            url=key, source_urls=sources, canonical_url=scraped.canonical_url, fetched_url=result.page.fetched_url,
            status_code=result.page.status_code, platform=platform_for(key), page_kind=result.kind,
            title=scraped.title, meta_description=scraped.meta_description, og=scraped.og, json_ld=scraped.json_ld,
            text=scraped.text, outbound_links=scraped.outbound_links, published_at=scraped.published_at,
            found_via=found_via, relevant=relevant, notes=notes, extraction="html",
        )
    hit, request = snippet
    return PageRecord(
        url=key, source_urls=sources, fetched_url=hit.fetch_url, status_code=request.result.error.status_code,
        platform=platform_for(key), page_kind=page_kind(key), title=hit.title, text=hit.snippet,
        found_via=found_via, relevant=relevant, notes=notes, extraction="snippet",
    )


def _evidence(collector: ImageCollector, page_urls: list[str]) -> ImageEvidence:
    outcome = collector.outcome or SearchOutcome(error=FetchError(url=None, status_code=None,
                                                                  reason=collector.problem or "timeout"))
    errors = [outcome.error] if outcome.error else []
    for url, request in collector.requests.items():
        if request.result is None:
            errors.append(FetchError(url=url, status_code=None, reason="timeout"))
        elif request.result.error is not None:
            errors.append(request.result.error)
    extras = dict(outcome.extras)
    if collector.skipped:
        extras["budget_skipped"] = collector.skipped
    if collector.fallback_reason:
        extras["fallback_reason"] = collector.fallback_reason
    if collector.problem and outcome.error is None:
        extras["collection_error"] = collector.problem
    return ImageEvidence(
        image_id=collector.image_id, angle=collector.image.angle, backend=collector.backend.name,
        request_id=outcome.request_id, searched_at=collector.searched_at or collector.started_at,
        hits=outcome.hits, page_urls=page_urls, search_extras=extras, collection_method=collector.method,
        errors=errors,
    )


# Stage entry point and console view


def make_backend(settings: Settings, capture: CaptureArtifact, run_dir: Path, *, refresh: bool) -> SearchBackend:
    if capture.backend == "mock":
        from pipeline.search.mock_backend import MockSearchBackend

        return MockSearchBackend(settings.mock_internet_url, scenario=capture.scenario, delay_ms=settings.mock_delay_ms)
    from pipeline.search.serpapi_backend import build_serpapi_backend

    return build_serpapi_backend(settings, capture, run_dir, refresh=refresh)


def make_agent(settings: Settings) -> AgentRunner | None:
    """A per-image agent runner when a real LLM is enabled, else None (deterministic path)."""
    if settings.llm_mode == "off":
        return None
    from pipeline.image_agent import make_agent_runner
    from pipeline.llm import build_model

    build_model(settings)  # fail fast on bad configuration, before any search
    return make_agent_runner(lambda: build_model(settings))


def run_stage1(run_dir: Path, settings: Settings, *, refresh: bool = False, backend: SearchBackend | None = None,
               agent: AgentRunner | None = None) -> RawEvidenceBundle:
    """Read 00_capture.json, collect evidence, validate it against the capture, and write 01_raw_evidence.json."""
    capture = read_artifact(run_dir, 0)
    if capture.backend == "serpapi" and settings.search_backend != "serpapi":
        raise ConfigError("this run searches with SerpApi; load settings with the serpapi backend")
    backend = backend or make_backend(settings, capture, run_dir, refresh=refresh)
    agent = agent or make_agent(settings)

    def log(**fields: object) -> None:
        log_event(run_dir, stage=1, **fields)

    async def collect() -> RawEvidenceBundle:
        try:
            bundle, _ = await collect_evidence(capture, backend, agent=agent, log=log)
        finally:
            await backend.aclose()
        return bundle

    bundle = asyncio.run(collect())
    validate_references(capture=capture, raw=bundle)
    write_artifact(run_dir, 1, bundle)
    return bundle


def run(run_dir: Path, settings: Settings, refresh: bool) -> str:
    """CLI stage runner (STAGE_RUNNERS[1]): collect, write 01_raw_evidence.json, return the hits view."""
    return format_hits(run_stage1(run_dir, settings, refresh=refresh))


def format_hits(bundle: RawEvidenceBundle) -> str:
    """The diagram's first view: hits per image, then the pages kept."""
    lines = ["Hits per image:"]
    for image in bundle.images:
        status = f"{len(image.hits)} hits, {len(image.page_urls)} pages kept, {image.collection_method}"
        if image.errors:
            status += ", errors: " + ", ".join(sorted({error.reason for error in image.errors}))
        lines.append(f"  {image.image_id} ({image.angle}): {status}")
        for hit in image.hits:
            lines.append(f"    {hit.position:>2}. {hit.title[:60]}  {hit.url}")
    html = sum(page.extraction == "html" for page in bundle.pages.values())
    lines.append(f"Pages: {len(bundle.pages)} ({html} HTML, {len(bundle.pages) - html} snippet-only)")
    for page in bundle.pages.values():
        images = ",".join(sorted({via.image_id for via in page.found_via}))
        flag = "" if page.relevant else " (excluded)"
        lines.append(f"  [{page.platform}/{page.page_kind}] {page.url}  via {images}{flag}")
    return "\n".join(lines)
