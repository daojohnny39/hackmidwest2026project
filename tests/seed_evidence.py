"""01_raw_evidence bundles for Section 2 tests, built from the mock seed without HTTP.

This mirrors Section 1's deterministic path (spec 1.3) closely enough to test
Section 2 on its own: search hits per fixture and scenario (as mock_internet/server.py
returns them), every hit fetched once per image, account links followed one level
from depth-0 profiles, 403/404 hits kept as snippet-only records, and the mirror
merged into its canonical page. Page fields match what scrape.py reads from
render_page(): trimmed JSON-LD, Open Graph, newline-separated visible text, and
outbound links as original https URLs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from mock_internet.seed import build_world
from pipeline.contracts import FetchError, FoundVia, ImageEvidence, ImageHit, LinkRef, PageRecord, RawEvidenceBundle
from pipeline.platforms import is_account_url, page_kind, platform_for

MOCK_ORIGIN = "http://127.0.0.1:8765"
FETCH_BUDGET = 12
PAGES, _LINKS, FIXTURES = build_world()
BY_ID = {page["id"]: page for page in PAGES}
BY_URL = {page["url"]: page for page in PAGES}
LINKS: dict[str, list[tuple[str, str]]] = defaultdict(list)
for _source, _target, _label in _LINKS:
    LINKS[_source].append((_target, _label))
for _targets in LINKS.values():
    _targets.sort()  # Corpus.links orders by target page ID

CAPTURES = {
    "alex": [("img-1", "front", "alex-front"), ("img-2", "left", "alex-left"), ("img-3", "right", "alex-right")],
    "sam": [("img-1", "front", "sam-front")],
    "casey": [("img-1", "front", "casey-front")],
    "unknown": [("img-1", "front", "unknown")],
}
SEARCHED_AT = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)


def local_url(page: dict) -> str:
    parts = urlsplit(page["url"])
    return f"{MOCK_ORIGIN}/web/{parts.netloc}{quote(parts.path, safe='/@')}"


def reverse_hits(fixture: str, scenario: str) -> list[dict]:
    """The pages a mock reverse-image search returns, in rank order (server.py's search())."""
    pages = [BY_ID[page_id] for page_id in FIXTURES[fixture]]
    alex = fixture.startswith("alex-")
    if scenario == "ambiguous" and alex:
        pages += [BY_ID[key] for key in ("alex-photo-professional", "alex-photo-social", "alex-photo-blog")]
    if scenario == "stale" and alex:
        pages += [BY_ID[key] for key in ("alex-old-bio", "alex-deleted")]
    if scenario == "duplicates" and pages:
        pages = [pages[0], pages[0]] + pages[1:]
        if alex:
            pages.append(BY_ID["alex-mirror"])
    if scenario == "empty":
        pages = []
    return pages


def _json_ld(page: dict) -> dict:
    """render_page()'s JSON-LD block after scrape.py trims it to the spec 1.4 fields."""
    if page["kind"] == "profile":
        same_as = [BY_ID[target]["url"] for target, _ in LINKS[page["id"]] if BY_ID[target]["kind"] == "profile"]
        block = {"@type": "Person", "name": page["display_name"] or page["title"],
                 "description": page["bio"] or page["body"],
                 "worksFor": {"@type": "Organization", "name": page["workplace"]},
                 "homeLocation": page["location"], "sameAs": same_as, "url": page["url"]}
    else:
        block = {"@type": "Article", "headline": page["title"], "datePublished": page["published_at"], "url": page["url"]}
        if page["display_name"]:
            block["author"] = {"@type": "Person", "name": page["display_name"]}
    return {key: value for key, value in block.items() if value not in ("", [], None)}


def _text(page: dict) -> str:
    host = urlsplit(page["url"]).hostname
    lines = ["Mock internet", f"/ {host}", f"{page['platform']} · {page['kind']}", page["title"], page["display_name"],
             page["workplace"], page["location"], page["body"], f"Updated {page['published_at']}"]
    lines += [label for _, label in LINKS[page["id"]]] + ["Fictional content for local testing."]
    return "\n".join(line for line in lines if line)[:4000]


def _html_record(key: str, page: dict, source_urls: list[str], vias: list[FoundVia]) -> PageRecord:
    canonical = BY_ID[page["canonical_id"]] if page["canonical_id"] else page
    return PageRecord(
        url=key, source_urls=source_urls, canonical_url=canonical["url"], fetched_url=local_url(page),
        status_code=200, platform=platform_for(key), page_kind=page_kind(key, person_json_ld=page["kind"] == "profile"),
        title=page["title"], meta_description=page["bio"] or page["body"],
        og={"og:title": page["title"], "og:type": "profile" if page["kind"] == "profile" else "article"},
        json_ld=[_json_ld(page)], text=_text(page),
        outbound_links=[LinkRef(url=BY_ID[target]["url"], label=label) for target, label in LINKS[page["id"]]],
        published_at=page["published_at"], found_via=vias,
    )


def collect(capture: str, scenario: str = "default", *, run_id: str | None = None) -> RawEvidenceBundle:
    """A RawEvidenceBundle for a mock capture, as the deterministic collector would write it."""
    run_id = run_id or f"mock-{capture}-0000abcd"
    vias: dict[str, list[FoundVia]] = defaultdict(list)  # original URL -> provenance
    snippets: dict[str, ImageHit] = {}
    images = []
    for image_id, angle, fixture in CAPTURES[capture]:
        hits = [
            ImageHit(position=rank, title=page["title"], url=page["url"], fetch_url=local_url(page),
                     source=urlsplit(page["url"]).hostname, snippet=page["body"][:240])
            for rank, page in enumerate(reverse_hits(fixture, scenario), 1)
        ]
        requested: list[str] = []
        for hit in hits:  # search hits first, in rank order; a repeated hit adds nothing
            if hit.url not in requested and len(requested) < FETCH_BUDGET:
                requested.append(hit.url)
                vias[hit.url].append(FoundVia(image_id=image_id, hit_position=hit.position, depth=0))
                snippets.setdefault(hit.url, hit)
        parents = [url for url in requested if BY_URL[url]["kind"] == "profile" and BY_URL[url]["status_code"] == 200]
        targets: dict[str, str] = {}
        for parent in parents:
            for target_id, _ in LINKS[BY_URL[parent]["id"]]:
                target = BY_ID[target_id]["url"]
                if is_account_url(target):
                    targets.setdefault(target, parent)
        for target in sorted(targets):  # then followed accounts, in URL order
            if target not in requested and len(requested) < FETCH_BUDGET:
                requested.append(target)
                vias[target].append(FoundVia(image_id=image_id, hit_position=None, depth=1, parent_url=targets[target]))
        errors = [
            FetchError(url=url, status_code=BY_URL[url]["status_code"],
                       reason={404: "not_found", 403: "forbidden"}[BY_URL[url]["status_code"]])
            for url in requested if BY_URL[url]["status_code"] != 200
        ]
        images.append((image_id, angle, hits, requested, errors))

    # Merge by canonical URL; failed fetches keep only a search snippet, or nothing if followed.
    pages: dict[str, PageRecord] = {}
    aliases: dict[str, str] = {}
    by_key: dict[str, list[str]] = defaultdict(list)
    for url in vias:
        page = BY_URL[url]
        key = BY_ID[page["canonical_id"]]["url"] if page["canonical_id"] and page["status_code"] == 200 else url
        by_key[key].append(url)
    for key, urls in sorted(by_key.items()):
        merged = sorted({(via.image_id, via.hit_position, via.depth, via.parent_url): via
                         for url in urls for via in vias[url]}.values(),
                        key=lambda via: (via.image_id, via.depth, via.hit_position or 0))
        page = BY_URL[key]
        if page["status_code"] == 200:
            pages[key] = _html_record(key, page, sorted(urls), merged)
        elif url_hit := snippets.get(key):
            direct = [via for via in merged if via.depth == 0]
            if not direct:
                continue
            pages[key] = PageRecord(
                url=key, source_urls=[key], fetched_url=url_hit.fetch_url, status_code=page["status_code"],
                platform=platform_for(key), page_kind=page_kind(key), title=url_hit.title, text=url_hit.snippet,
                found_via=direct, extraction="snippet",
            )
        else:
            continue
        for url in urls:
            aliases[url] = key
        aliases[key] = key

    evidence = []
    for image_id, angle, hits, requested, errors in images:
        page_urls = sorted({aliases[url] for url in requested if url in aliases})
        evidence.append(ImageEvidence(
            image_id=image_id, angle=angle, backend="mock", request_id=f"mock-{image_id}", searched_at=SEARCHED_AT,
            hits=hits, page_urls=page_urls, collection_method="deterministic", errors=errors,
        ))
    return RawEvidenceBundle(run_id=run_id, num_images=len(evidence), images=evidence, pages=pages, url_aliases=aliases)
