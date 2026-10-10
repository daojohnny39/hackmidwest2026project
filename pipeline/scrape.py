"""HTML -> the scraped fields of a PageRecord (spec 1.4).

Extracts the title, meta description, Open Graph tags, JSON-LD Person and
Article blocks, the canonical link, the first <time datetime>, outbound links
with their labels, and visible text cut to 4,000 characters. Page content is
untrusted data: every field is bounded, and every URL (links, canonical, JSON-LD
url/sameAs) goes through platforms.normalize_url, so mock pages yield original
https URLs instead of local /web/ ones. No network access.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from pipeline.contracts import LinkRef
from pipeline.platforms import normalize_url

TEXT_LIMIT = 4000
TITLE_LIMIT = 300
STRING_LIMIT = 2000
LABEL_LIMIT = 200
MAX_LINKS = 100
MAX_JSON_LD = 10
MAX_OG = 30
MAX_LIST = 20
MAX_DEPTH = 4

OG_PREFIXES = ("og:", "profile:", "article:")
ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "SocialMediaPosting", "Report", "ScholarlyArticle",
                 "TechArticle", "LiveBlogPosting"}
# JSON-LD fields kept per block type (spec 1.4), plus @type and url.
PERSON_FIELDS = ("name", "description", "jobTitle", "worksFor", "homeLocation", "sameAs", "url")
ARTICLE_FIELDS = ("headline", "author", "datePublished", "url")
URL_FIELDS = {"url", "sameAs"}
NON_TEXT_TAGS = ("script", "style", "noscript", "template", "svg", "head")

_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Scraped:
    title: str = ""
    meta_description: str = ""
    og: dict[str, str] = field(default_factory=dict)
    json_ld: list[dict] = field(default_factory=list)
    canonical_url: str | None = None
    outbound_links: list[LinkRef] = field(default_factory=list)
    published_at: str | None = None
    text: str = ""
    person: bool = False  # a top-level JSON-LD Person (or a ProfilePage's main Person)


def scrape_html(html: str, *, page_url: str, fetched_url: str, mock_origin: str | None = None) -> Scraped:
    """Scrape one page. page_url is its normalized identity; fetched_url resolves relative links."""
    soup = BeautifulSoup(html, "html.parser")
    base = fetched_url
    if (tag := soup.find("base", href=True)) is not None:
        base = _resolve(tag["href"], fetched_url) or fetched_url

    def norm(url: object) -> str | None:
        return normalize_url(url, base=base, mock_origin=mock_origin) if isinstance(url, str) else None

    json_ld, person = _json_ld(soup, norm)
    og = _open_graph(soup, norm)
    canonical = None
    for link in soup.find_all("link", href=True):
        if "canonical" in [rel.lower() for rel in _as_list(link.get("rel"))]:
            canonical = norm(link["href"])
            break
    published = None
    if (time := soup.find("time", attrs={"datetime": True})) is not None:
        published = _clip(time["datetime"], 64) or None
    if published is None:
        published = next((_clip(block["datePublished"], 64) for block in json_ld
                          if isinstance(block.get("datePublished"), str)), None) or og.get("article:published_time")

    return Scraped(
        title=_clip(soup.title.get_text() if soup.title else "", TITLE_LIMIT),
        meta_description=_clip(_meta_content(soup, "description"), STRING_LIMIT),
        og=og,
        json_ld=json_ld,
        canonical_url=canonical,
        outbound_links=_links(soup, page_url, norm),
        published_at=published,
        text=_visible_text(soup),
        person=person,
    )


def _resolve(href: str, base: str) -> str | None:
    from urllib.parse import urljoin
    try:
        return urljoin(base, href.strip())
    except ValueError:
        return None


def _clip(value: object, limit: int) -> str:
    return _SPACE.sub(" ", str(value)).strip()[:limit]


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    for meta in soup.find_all("meta", content=True):
        if str(meta.get("name", "")).lower() == name:
            return meta["content"]
    return ""


def _open_graph(soup: BeautifulSoup, norm) -> dict[str, str]:
    og: dict[str, str] = {}
    for meta in soup.find_all("meta", content=True):
        prop = str(meta.get("property", "")).strip().lower()
        if not prop.startswith(OG_PREFIXES) or prop in og or len(og) >= MAX_OG:
            continue
        value = _clip(meta["content"], STRING_LIMIT)
        if prop == "og:url":
            value = norm(value) or ""
        if value:
            og[prop] = value
    return og


def _links(soup: BeautifulSoup, page_url: str, norm) -> list[LinkRef]:
    links: dict[str, str] = {}
    root = soup.body or soup
    for anchor in root.find_all("a", href=True):
        url = norm(anchor["href"])
        if url is None or url == page_url:
            continue
        label = _clip(anchor.get_text(" "), LABEL_LIMIT)
        if url not in links:
            if len(links) >= MAX_LINKS:
                break
            links[url] = label
        elif not links[url]:
            links[url] = label
    return [LinkRef(url=url, label=label) for url, label in links.items()]


def _visible_text(soup: BeautifulSoup) -> str:
    root = soup.body or soup
    for tag in root.find_all(NON_TEXT_TAGS):
        tag.decompose()
    lines = (_SPACE.sub(" ", line).strip() for line in root.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)[:TEXT_LIMIT]


def _reject_constant(name: str):
    raise ValueError(f"JSON-LD contains {name}")


def _json_ld(soup: BeautifulSoup, norm) -> tuple[list[dict], bool]:
    """Trimmed Person and Article blocks, and whether a top-level Person exists."""
    blocks: list[dict] = []
    person = False
    for script in soup.find_all("script"):
        kind = str(script.get("type", "")).split(";")[0].strip().lower()
        if kind != "application/ld+json":
            continue
        try:
            data = json.loads(script.string or script.get_text(), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            continue
        for item in _top_level(data):
            trimmed = _trim(item, norm)
            if trimmed is None:
                continue
            person = person or "Person" in _types(trimmed)
            if len(blocks) < MAX_JSON_LD:
                blocks.append(trimmed)
    return blocks, person


def _top_level(data: object) -> list[dict]:
    """Top-level JSON-LD items: the block, list members, @graph members, and a ProfilePage's mainEntity."""
    items: list[dict] = []
    for item in _as_list(data)[:MAX_LIST]:
        if not isinstance(item, dict):
            continue
        graph = item.get("@graph")
        members = [member for member in _as_list(graph)[:MAX_LIST] if isinstance(member, dict)] if graph else [item]
        for member in members:
            items.append(member)
            if "ProfilePage" in _types(member) and isinstance(member.get("mainEntity"), dict):
                items.append(member["mainEntity"])
    return items


def _types(block: dict) -> set[str]:
    return {value for value in _as_list(block.get("@type")) if isinstance(value, str)}


def _trim(block: dict, norm) -> dict | None:
    types = _types(block)
    if "Person" in types:
        names = PERSON_FIELDS
    elif types & ARTICLE_TYPES:
        names = ARTICLE_FIELDS
    else:
        return None
    trimmed: dict = {"@type": sorted(types)[0] if len(types) == 1 else sorted(types)}
    for name in names:
        if name in block:
            value = _clean(block[name], norm, url=name in URL_FIELDS)
            if value not in (None, "", [], {}):
                trimmed[name] = value
    return trimmed


def _clean(value: object, norm, *, url: bool = False, depth: int = 0):
    """Bounded, finite, JSON-safe copy; URL fields are normalized and dropped if unusable."""
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, str):
        return norm(value) if url else _clip(value, STRING_LIMIT)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        cleaned = (_clean(item, norm, url=url, depth=depth + 1) for item in value[:MAX_LIST])
        return [item for item in cleaned if item not in (None, "", [], {})]
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:MAX_LIST]:
            if not isinstance(key, str):
                continue
            item = _clean(item, norm, url=key in URL_FIELDS, depth=depth + 1)
            if item not in (None, "", [], {}):
                out[key[:LABEL_LIMIT]] = item
        return out
    return None
