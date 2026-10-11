"""Domain -> platform table, account and post URL patterns, and URL normalization (spec 1.4).

Every URL the pipeline compares is a normalize_url() result: an absolute http(s)
URL with a lowercased host, no "www.", no default port, fragment, tracking
parameters or trailing slash. Paths keep their case. Mock pages are served from
<mock origin>/web/<host>/<path>; normalize_url unwraps those only when the origin
matches exactly, port included, so a live URL is never rewritten.

All functions here are pure: no network, no DNS.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

PageKind = Literal["profile", "post", "article"]

# Host (or parent domain) -> platform. Live and mock domains share one table.
PLATFORM_DOMAINS: dict[str, str] = {
    "linkedin.com": "linkedin", "linkup.test": "linkedin",
    "x.com": "x", "twitter.com": "x", "chatter.test": "x",
    "instagram.com": "instagram", "photogram.test": "instagram",
    "github.com": "github", "codeforge.test": "github",
    "facebook.com": "facebook",
    "medium.com": "blog", "substack.com": "blog", "fieldnotes.test": "blog",
    "riverbend-news.test": "news", "syndicated.test": "news",
}

PLATFORM_LABELS: dict[str, str] = {
    "linkedin": "LinkedIn", "x": "X", "instagram": "Instagram", "github": "GitHub",
    "facebook": "Facebook", "blog": "Blog", "news": "News", "other": "Web",
}

# Same site under another name; normalize_url rewrites these hosts.
HOST_ALIASES = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "mobile.x.com": "x.com",
                "m.facebook.com": "facebook.com", "mobile.facebook.com": "facebook.com",
                "m.linkedin.com": "linkedin.com", "m.github.com": "github.com"}

_HANDLE = r"(?P<handle>[A-Za-z0-9_%][A-Za-z0-9_.%-]{0,99})"  # % allows percent-encoded names
_SEGMENT = r"[^/]+"
_PATH_SAFE = "@:+!$&'()*,;=-._~"

# Single-segment paths on handle-style sites that are site pages, not accounts.
_RESERVED = {
    "x": {"home", "explore", "search", "i", "settings", "hashtag", "intent", "share", "login", "logout",
          "messages", "notifications", "compose", "tos", "privacy", "signup", "about", "jobs"},
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "direct", "about",
                  "developer", "legal", "web"},
    "github": {"features", "topics", "explore", "marketplace", "settings", "login", "logout", "about",
               "pricing", "collections", "sponsors", "apps", "orgs", "organizations", "search",
               "notifications", "pulls", "issues", "codespaces", "enterprise", "join", "signup", "trending"},
    "facebook": {"pages", "groups", "events", "watch", "marketplace", "login", "logout", "sharer",
                 "share", "photo", "photo.php", "story.php", "permalink.php", "people", "help",
                 "policies", "privacy", "gaming", "reel", "reels", "hashtag", "settings"},
}

# (domains, anchored account-path pattern). Patterns match the whole path, so
# "/@alexbuilds/old-project" and "/samdraws/private-album" are not accounts.
_ACCOUNT_RULES: list[tuple[tuple[str, ...], re.Pattern[str]]] = [
    (("linkedin.com", "linkup.test"), re.compile(rf"/in/{_HANDLE}")),
    (("x.com", "chatter.test"), re.compile(rf"/{_HANDLE}")),
    (("instagram.com", "photogram.test"), re.compile(rf"/{_HANDLE}")),
    (("github.com", "codeforge.test"), re.compile(rf"/{_HANDLE}")),
    (("facebook.com",), re.compile(rf"/{_HANDLE}")),
    (("medium.com", "substack.com", "fieldnotes.test"), re.compile(rf"/@{_HANDLE}")),
]

# Post patterns. A named "handle" group, when present, is the author.
_POST_RULES: list[tuple[tuple[str, ...], re.Pattern[str]]] = [
    (("x.com", "chatter.test"), re.compile(rf"/{_HANDLE}/status/\d+")),
    (("instagram.com", "photogram.test"), re.compile(r"/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+")),
    (("linkedin.com", "linkup.test"), re.compile(rf"/posts/{_SEGMENT}|/feed/update/{_SEGMENT}")),
    (("facebook.com",), re.compile(rf"/{_HANDLE}/posts/{_SEGMENT}|/permalink\.php|/story\.php")),
]

# Query parameters that only track a click. Others are kept: they can change the page.
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "igsh",
                    "_ga", "_gl", "ref_src", "ref_url", "trk", "trkinfo", "originalsubdomain", "si"}
_TRACKING_PREFIXES = ("utm_",)
_SHARE_PARAMS = {"x.com": {"s", "t"}}  # x.com/<handle>/status/<id>?s=20


def _host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return HOST_ALIASES.get(host.removeprefix("www."), host.removeprefix("www."))


def _on(host: str, domains: tuple[str, ...] | str) -> str | None:
    """The domain in `domains` that host is, or is a subdomain of."""
    for domain in (domains,) if isinstance(domains, str) else domains:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def _path(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    return path.rstrip("/") or "/"


def platform_for(url: str) -> str:
    """linkedin, x, instagram, github, facebook, blog, news, or other."""
    domain = _on(_host(url), tuple(PLATFORM_DOMAINS))
    return PLATFORM_DOMAINS[domain] if domain else "other"


def _match(rules: list[tuple[tuple[str, ...], re.Pattern[str]]], url: str) -> tuple[str, re.Match[str]] | None:
    host, path = _host(url), _path(url)
    for domains, pattern in rules:
        domain = _on(host, domains)
        if domain and (found := pattern.fullmatch(path)):
            return domain, found
    return None


def account_handle(url: str) -> str | None:
    """The handle when url is an account (profile) URL on a known platform, else None."""
    if is_post_url(url):
        return None
    host = _host(url)
    if _on(host, "substack.com") and host != "substack.com" and _path(url) == "/":
        return host.removesuffix(".substack.com")  # <name>.substack.com is the writer's home page
    if _on(host, "facebook.com") and _path(url) == "/profile.php":
        ids = [value for name, value in parse_qsl(urlsplit(url).query) if name == "id" and value.isdigit()]
        return ids[0] if ids else None
    matched = _match(_ACCOUNT_RULES, url)
    if matched is None:
        return None
    _, found = matched
    handle = found.group("handle")
    if handle.lower() in _RESERVED.get(platform_for(url), set()):
        return None
    return handle


def is_account_url(url: str) -> bool:
    return account_handle(url) is not None


def is_post_url(url: str) -> bool:
    return _match(_POST_RULES, url) is not None


def post_author_handle(url: str) -> str | None:
    """The author handle a post URL carries (chatter.test/<handle>/status/<id>), else None."""
    matched = _match(_POST_RULES, url)
    if matched is None or "handle" not in matched[1].re.groupindex:
        return None
    return matched[1].group("handle")


def account_url_for(post_url: str) -> str | None:
    """The normalized account URL of a post's author, when the post URL names one."""
    handle = post_author_handle(post_url)
    if handle is None:
        return None
    parts = urlsplit(post_url)
    return normalize_url(urlunsplit((parts.scheme, parts.netloc, f"/{handle}", "", "")))


def has_account_rules(url: str) -> bool:
    """True when the platform has account patterns, so they alone decide what is an account."""
    host = _host(url)
    return any(_on(host, domains) for domains, _ in _ACCOUNT_RULES)


def page_kind(url: str, *, person_json_ld: bool = False) -> PageKind:
    """post if the path matches a post pattern, profile if it matches an account
    pattern, else article. A top-level JSON-LD Person makes an otherwise unknown
    URL a profile, but never overrides a known platform's patterns."""
    if is_post_url(url):
        return "post"
    if is_account_url(url):
        return "profile"
    if person_json_ld and not has_account_rules(url):
        return "profile"
    return "article"


def normalize_url(url: str, *, base: str | None = None, mock_origin: str | None = None) -> str | None:
    """The pipeline's identity for a page URL, or None if url is not a usable http(s) page link.

    `base` resolves relative links. With `mock_origin`, local mock links
    (<mock_origin>/web/<host>/<path>) become https://<host>/<path>, and other
    mock-server links (home page, images, search) return None.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None

    if mock_origin is not None:
        origin = f"{scheme}://{parts.netloc.lower()}"
        if origin == mock_origin.rstrip("/").lower():
            if not parts.path.startswith("/web/"):
                return None
            host, _, path = unquote(parts.path[len("/web/"):]).partition("/")
            if not host:
                return None
            return normalize_url(f"https://{host}/{path}")

    host = parts.hostname.lower().rstrip(".")
    host = host.removeprefix("www.")
    host = HOST_ALIASES.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != {"http": 80, "https": 443}[scheme]:
        host = f"{host}:{port}"

    # Re-quote each segment so equivalent encodings compare equal; an encoded "/" stays encoded.
    path = "/".join(quote(unquote(segment), safe=_PATH_SAFE) for segment in parts.path.split("/")).rstrip("/")

    share = _SHARE_PARAMS.get(host, set())
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
             if name.lower() not in _TRACKING_PARAMS and name not in share
             and not name.lower().startswith(_TRACKING_PREFIXES)]
    return urlunsplit((scheme, host, path, urlencode(query, quote_via=quote, safe=":/@"), ""))
