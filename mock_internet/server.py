"""Local HTTP search + pages. This module never makes outbound requests."""

from collections import Counter
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .db import Corpus, DEFAULT_DB, initialize

SCENARIOS = {
    "default": "Seeded results; text search naturally includes namesakes and outdated pages.",
    "empty": "No search results.",
    "ambiguous": "Image fixtures also return a namesake's unrelated profiles.",
    "duplicates": "Repeated search hits and, for Alex's image fixtures, a syndicated article.",
    "stale": "Alex's image fixtures also return an old biography and deleted page.",
    "rate_limited": "HTTP 429 with Retry-After: 1.",
    "server_error": "HTTP 503 with Retry-After: 1.",
}


def page_url(base, page):
    parsed = urlsplit(page["url"])
    return f"{base}/web/{parsed.netloc}{quote(parsed.path, safe='/@')}"


STYLE = """
body{font:17px/1.65 system-ui,sans-serif;color:#203143;background:#f3f6fa;margin:0}
main{max-width:850px;margin:50px auto;padding:32px;background:white;border-radius:16px}
a{color:#195da8}nav,small,time{color:#65758a}article{border-top:1px solid #dbe3ed;padding:20px 0}
h1{line-height:1.2}input,button{font:inherit;padding:10px;border:1px solid #b8c7d8;border-radius:6px}
input{width:65%}button{background:#195da8;color:white;cursor:pointer}code{background:#eef2f6;padding:2px 5px}
.badge{font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#61758a}
"""


def layout(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style>{STYLE}</style></head>'
            f'<body><main>{body}</main></body></html>')


def render_page(corpus, page, base):
    links = corpus.links(page["id"])
    canonical = corpus.page(page["canonical_id"]) if page["canonical_id"] else page
    local_links = [{"url": page_url(base, link), "label": link["label"]} for link in links]
    name = page["display_name"]
    structured = {
        "@context": "https://schema.org", "@type": "Person" if page["kind"] == "profile" else "Article",
        "name": name or page["title"], "description": page["bio"] or page["body"],
        "url": page_url(base, page),
    }
    if page["kind"] == "profile":
        structured.update(worksFor={"@type": "Organization", "name": page["workplace"]},
                          homeLocation=page["location"],
                          sameAs=[page_url(base, link) for link in links
                                  if corpus.page(link["id"])["kind"] == "profile"])
    else:
        structured.update(headline=page["title"], datePublished=page["published_at"])
        if name:
            structured["author"] = {"@type": "Person", "name": name}
    metadata = (f'<link rel="canonical" href="{escape(page_url(base, canonical), quote=True)}">'
                f'<meta name="description" content="{escape(page["bio"] or page["body"], quote=True)}">'
                f'<meta property="og:title" content="{escape(page["title"], quote=True)}">'
                f'<meta property="og:type" content="{"profile" if page["kind"] == "profile" else "article"}">'
                '<script type="application/ld+json">'
                + json.dumps(structured).replace("<", "\\u003c") + '</script>')
    details = "".join(f'<p class="{field}">{escape(page[field])}</p>'
                      for field in ("workplace", "location") if page[field])
    navigation = "".join(f'<li><a href="{escape(link["url"], quote=True)}">{escape(link["label"])}</a></li>'
                         for link in local_links)
    body = (f'<nav><a href="/">Mock internet</a> / {escape(urlsplit(page["url"]).hostname)}</nav>'
            f'<p class="badge">{escape(page["platform"])} · {escape(page["kind"])}</p>'
            f'<article class="{escape(page["kind"])}"><h1>{escape(page["title"])}</h1>'
            f'<p class="display-name">{escape(name)}</p>{details}'
            f'<div class="content">{escape(page["body"])}</div>'
            f'<p><time datetime="{page["published_at"]}">Updated {page["published_at"]}</time></p>'
            f'</article><nav aria-label="Related links"><ul>{navigation}</ul></nav>'
            '<small>Fictional content for local testing.</small>')
    return layout(page["title"], body).replace("</head>", metadata + "</head>")


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path):
        self.corpus = Corpus(db_path)
        self.counts = Counter()
        self.counts_lock = threading.Lock()
        super().__init__(address, Handler)
        host, port = self.server_address[:2]
        self.base_url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"

    def record(self, route, status):
        with self.counts_lock:
            self.counts["requests"] += 1
            self.counts[f"route:{route}"] += 1
            self.counts[f"status:{status}"] += 1


class Handler(BaseHTTPRequestHandler):
    server_version = "MockInternet/1.0"

    def log_message(self, format, *args):
        # Do not log raw query strings: drop-in clients may supply an API key.
        pass

    def respond(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, dict) else payload.encode()
        self.server.record(urlsplit(self.path).path, status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Mock-Internet", "true")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route()
        except (ValueError, KeyError) as exc:
            self.respond(400, {"error": str(exc), "mock": True})
        except (BrokenPipeError, ConnectionResetError):
            pass  # A timeout scenario can intentionally disconnect the client.

    @staticmethod
    def integer(params, name, default, minimum, maximum):
        try:
            value = int(params.get(name, str(default)))
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value

    def route(self):
        parsed = urlsplit(self.path)
        if len(self.path) > 8192:
            self.respond(414, {"error": "Request URL too long"})
            return
        params = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        path, corpus, base = parsed.path, self.server.corpus, self.server.base_url

        if path == "/health":
            self.respond(200, {"status": "ok", "mock": True, "database": corpus.stats()})
        elif path == "/stats":
            with self.server.counts_lock:
                counts = dict(self.server.counts)
            self.respond(200, {"mock": True, "requests": counts, "database": corpus.stats(), "external_requests": 0})
        elif path == "/fixtures":
            fixtures = [row["id"] for row in corpus.rows("SELECT id FROM image_fixtures ORDER BY id")]
            self.respond(200, {"image_fixtures": [{"id": key, "url": f"{base}/images/{key}.svg"} for key in fixtures],
                               "scenarios": SCENARIOS, "platforms": ["professional", "social", "photos", "code", "blog", "web"]})
        elif path.startswith("/images/") and path.endswith(".svg"):
            fixture = unquote(path.removeprefix("/images/").removesuffix(".svg"))
            corpus.reverse(fixture)  # Validate against stored fixture IDs.
            self.respond(200, '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">'
                         '<rect width="320" height="200" fill="#e5edf7"/>'
                         '<text x="20" y="90" font-family="sans-serif" font-size="22">Synthetic fixture</text>'
                         f'<text x="20" y="125" font-family="sans-serif">{escape(fixture)}</text></svg>',
                         "image/svg+xml")
        elif path in ("/search", "/search.json", "/api/search", "/api/reverse-image", "/browse"):
            self.search(params, reverse=path == "/api/reverse-image", html=path == "/browse")
        elif path == "/fetch":
            self.fetch(params)
        elif path.startswith("/web/"):
            if self.fault(params):
                return
            page = corpus.page_by_url("https://" + unquote(path[len("/web/"):]))
            if not page:
                self.respond(404, layout("Not found", "<h1>Page not found</h1><p>This URL is not in the local corpus.</p>"), "text/html; charset=utf-8")
                return
            self.respond(page["status_code"], render_page(corpus, page, base), "text/html; charset=utf-8")
        elif path == "/":
            self.home()
        else:
            self.respond(404, {"error": "Unknown local route", "mock": True})

    def fault(self, params):
        scenario = params.get("scenario", "default")
        if scenario not in SCENARIOS:
            raise ValueError("Unknown scenario. See /fixtures for supported scenarios.")
        delay = self.integer(params, "delay_ms", 0, 0, 5000)
        if delay:
            time.sleep(delay / 1000)
        status = {"rate_limited": 429, "server_error": 503}.get(scenario)
        if status:
            self.respond(status, {"error": "Simulated rate limit" if status == 429 else "Simulated upstream failure",
                                  "mock": True}, headers={"Retry-After": "1"})
            return True
        return False

    def image_fixture(self, params):
        fixture = params.get("image_id")
        if fixture:
            return fixture
        image_url = params.get("url", "")
        if image_url.startswith("mock://"):
            return image_url[len("mock://"):]
        parsed = urlsplit(image_url)
        # Only our exact fixture URLs are accepted, never a remote image with the same filename.
        if f"{parsed.scheme}://{parsed.netloc}" == self.server.base_url and parsed.path.startswith("/images/") and parsed.path.endswith(".svg"):
            return unquote(parsed.path[len("/images/"):-4])
        raise ValueError("Use image_id, url=mock://alex-front, or an image URL returned by /fixtures")

    def search(self, params, *, reverse=False, html=False):
        if self.fault(params):
            return
        engine = params.get("engine", "yandex_images" if reverse else "google")
        if engine not in ("google", "yandex", "yandex_images"):
            raise ValueError("Supported engines: google, yandex, yandex_images")
        reverse = reverse or engine == "yandex_images"
        limit = self.integer(params, "num", 10, 1, 100)
        start = self.integer(params, "start", 0, 0, 10000)
        corpus, base = self.server.corpus, self.server.base_url
        fixture = self.image_fixture(params) if reverse else None
        query = params.get("q", params.get("text", "")).strip()
        platform = params.get("platform", "")
        if platform and platform not in ("professional", "social", "photos", "code", "blog", "web"):
            raise ValueError("Unknown platform. See /fixtures for supported platforms.")
        if not reverse and not query and not params.get("site"):
            raise ValueError("Supply q (text query) or site (fixture hostname)")
        pages = corpus.reverse(fixture) if reverse else corpus.search(query, site=params.get("site", ""), platform=platform)
        scenario = params.get("scenario", "default")
        if scenario == "ambiguous" and reverse and fixture.startswith("alex-"):
            pages += [corpus.page(key) for key in ("alex-photo-professional", "alex-photo-social", "alex-photo-blog")]
        if scenario == "stale" and reverse and fixture.startswith("alex-"):
            pages += [corpus.page(key) for key in ("alex-old-bio", "alex-deleted")]
        if scenario == "duplicates" and pages:
            pages = [pages[0], pages[0]] + pages[1:]
            if reverse and fixture.startswith("alex-"):
                pages.append(corpus.page("alex-mirror"))
        if scenario == "empty":
            pages = []
        results = []
        for position, page in enumerate(pages[start:start + limit], start + 1):
            result = {"position": position, "title": page["title"], "link": page_url(base, page),
                      "snippet": page["body"][:240], "source": urlsplit(page["url"]).hostname,
                      "date": page["published_at"], "mock_original_url": page["url"]}
            if reverse:
                image = {"link": f"{base}/images/{fixture}.svg", "width": 320, "height": 200}
                result.update(thumbnail=image, original_image=image)
            results.append(result)
        # Never echo API credentials, even if a SerpApi client supplies them.
        echoed = {key: value for key, value in params.items() if key in
                  ("q", "text", "engine", "url", "image_id", "num", "start", "site", "platform", "scenario", "delay_ms")}
        echoed["engine"] = "yandex_images" if reverse else engine
        request_id = hashlib.sha256(json.dumps(echoed, sort_keys=True).encode()).hexdigest()[:16]
        result_key = "image_results" if reverse else "organic_results"
        payload = {"search_metadata": {"id": f"mock-{request_id}", "status": "Success", "mock": True},
                   "search_parameters": echoed, "search_information": {"total_results": len(pages)},
                   result_key: results}
        if start + limit < len(pages):
            next_params = dict(echoed, start=start + limit)
            payload["serpapi_pagination"] = {"next": base + "/search.json?" + urlencode(next_params)}
        if html:
            items = "".join(f'<article><a href="{escape(row["link"], quote=True)}">{escape(row["title"])}</a>'
                            f'<p>{escape(row["snippet"])}</p><small>{escape(row["source"])}</small></article>' for row in results)
            self.respond(200, layout("Search results", '<a href="/">Mock internet</a>'
                         f'<h1>Results for {escape(query or fixture)}</h1><p>{len(pages)} results</p>{items}'),
                         "text/html; charset=utf-8")
        else:
            self.respond(200, payload)

    def resolve_page(self, raw_url):
        parsed = urlsplit(raw_url)
        base = self.server.base_url
        if f"{parsed.scheme}://{parsed.netloc}" == base:
            if not parsed.path.startswith("/web/"):
                return None
            raw_url = "https://" + unquote(parsed.path[len("/web/"):])
        return self.server.corpus.page_by_url(raw_url)

    def fetch(self, params):
        if self.fault(params):
            return
        if not params.get("url"):
            raise ValueError("Supply url to fetch a seeded page")
        page = self.resolve_page(params["url"])
        if not page:
            self.respond(404, {"error": "URL is not in the mock corpus; no external request was made", "mock": True})
            return
        base, corpus = self.server.base_url, self.server.corpus
        payload = {"url": page["url"], "local_url": page_url(base, page), "status_code": page["status_code"],
                   "content_type": "text/html", "title": page["title"], "text": page["body"],
                   "html": render_page(corpus, page, base),
                   "links": [{"url": link["url"], "local_url": page_url(base, link), "label": link["label"]}
                             for link in corpus.links(page["id"])], "mock": True}
        self.respond(page["status_code"], payload)

    def home(self):
        corpus, base = self.server.corpus, self.server.base_url
        stats = corpus.stats()
        cards = "".join(f'<article><a href="{escape(page_url(base, page), quote=True)}">{escape(page["title"])}</a>'
                        f'<p>{escape(page["body"])}</p></article>' for page in corpus.rows(
                            "SELECT * FROM pages WHERE platform='professional' ORDER BY id"))
        body = ('<p class="badge">Local test environment</p><h1>A small, fictional internet.</h1>'
                f'<p>{stats["pages"]} pages · {stats["entities"]} people · {stats["links"]} links. '
                'Search profiles, posts, and articles, then follow their links.</p>'
                '<form action="/browse"><label for="q">Search the mock internet</label><br>'
                '<input id="q" name="q" value="Alex Morgan" required> <button>Search</button></form>'
                '<p>Try <code>"Alex Morgan"</code>, <code>transit</code>, or '
                '<code>site:chatter.test community</code>.</p>'
                '<p><a href="/fixtures">Image fixtures and failure scenarios</a> · '
                '<a href="/stats">Request statistics</a></p>' + cards)
        self.respond(200, layout("Mock internet", body), "text/html; charset=utf-8")


def create_server(host="127.0.0.1", port=8765, db_path=DEFAULT_DB):
    initialize(db_path)
    return MockServer((host, port), db_path)
