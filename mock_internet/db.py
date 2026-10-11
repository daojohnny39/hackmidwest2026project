"""Persistent SQLite corpus and deterministic keyword retrieval."""

from contextlib import closing
from pathlib import Path
import re
import shlex
import sqlite3

from .seed import PEOPLE, SEED_VERSION, build_world

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "mock_internet.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, workplace TEXT NOT NULL, location TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
    entity_id TEXT REFERENCES entities(id), platform TEXT NOT NULL, kind TEXT NOT NULL,
    display_name TEXT NOT NULL, bio TEXT NOT NULL, workplace TEXT NOT NULL,
    location TEXT NOT NULL, published_at TEXT NOT NULL, status_code INTEGER NOT NULL,
    canonical_id TEXT REFERENCES pages(id)
);
CREATE TABLE IF NOT EXISTS links (
    source_id TEXT NOT NULL REFERENCES pages(id), target_id TEXT NOT NULL REFERENCES pages(id),
    label TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
);
CREATE TABLE IF NOT EXISTS image_fixtures (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS image_matches (
    fixture_id TEXT NOT NULL REFERENCES image_fixtures(id),
    page_id TEXT NOT NULL REFERENCES pages(id), position INTEGER NOT NULL,
    PRIMARY KEY (fixture_id, position)
);
CREATE INDEX IF NOT EXISTS pages_entity ON pages(entity_id);
"""


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(path=DEFAULT_DB, *, reset=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        version = connection.execute("SELECT value FROM metadata WHERE key='seed_version'").fetchone()
        if version and not reset:
            if version[0] != SEED_VERSION:
                raise ValueError("Seed version changed. Stop the server and run: python3 -m mock_internet seed --reset")
            return
        for table in ("image_matches", "image_fixtures", "links", "pages", "entities", "metadata"):
            connection.execute(f"DELETE FROM {table}")
        connection.executemany("INSERT INTO entities VALUES (:id, :name, :workplace, :location)", PEOPLE)
        pages, links, fixtures = build_world()
        connection.executemany("""INSERT INTO pages VALUES (
            :id, :url, :title, :body, :entity_id, :platform, :kind, :display_name, :bio,
            :workplace, :location, :published_at, :status_code, :canonical_id)""", pages)
        connection.executemany("INSERT INTO links VALUES (?, ?, ?)", links)
        for fixture, page_ids in fixtures.items():
            connection.execute("INSERT INTO image_fixtures VALUES (?)", (fixture,))
            connection.executemany("INSERT INTO image_matches VALUES (?, ?, ?)",
                                   [(fixture, page_id, i) for i, page_id in enumerate(page_ids, 1)])
        connection.execute("INSERT INTO metadata VALUES ('seed_version', ?)", (SEED_VERSION,))


class Corpus:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)

    def rows(self, sql, params=()):
        with closing(connect(self.path)) as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def page(self, page_id):
        return next(iter(self.rows("SELECT * FROM pages WHERE id=?", (page_id,))), None)

    def page_by_url(self, url):
        # Fragments and tracking parameters don't change a fixture's identity.
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return None
        canonical = f"https://{parsed.hostname.lower()}{parsed.path.rstrip('/')}"
        return next(iter(self.rows("SELECT * FROM pages WHERE url=?", (canonical,))), None)

    def links(self, page_id):
        return self.rows("""SELECT p.id, p.url, l.label FROM links l JOIN pages p ON p.id=l.target_id
                            WHERE l.source_id=? ORDER BY l.target_id""", (page_id,))

    def search(self, query, *, site="", platform=""):
        try:
            tokens = shlex.split(query.lower())
        except ValueError as exc:
            raise ValueError("Unclosed quotation mark in q") from exc
        terms, excluded = [], []
        sites = [site.lower()] if site else []
        for token in tokens:
            if token.startswith("site:"):
                sites.append(token[5:])
            elif token.startswith("-") and len(token) > 1:
                excluded.append(token[1:])
            elif token:
                terms.append(token)
        from urllib.parse import urlsplit
        ranked = []
        for page in self.rows("SELECT * FROM pages ORDER BY id"):
            host = urlsplit(page["url"]).hostname
            if any(not domain or not (host == domain or host.endswith("." + domain)) for domain in sites):
                continue
            if platform and page["platform"] != platform:
                continue
            title = page["title"].lower()
            content = " ".join(str(page[key]) for key in
                               ("title", "body", "display_name", "bio", "workplace", "location", "url")).lower()
            def matches(term):
                return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", content) is not None
            if not all(matches(term) for term in terms) or any(matches(term) for term in excluded):
                continue
            score = sum(5 if term in title else 1 for term in terms) + (page["kind"] == "profile")
            ranked.append((score, page))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [page for _, page in ranked]

    def reverse(self, fixture):
        exists = self.rows("SELECT id FROM image_fixtures WHERE id=?", (fixture,))
        if not exists:
            raise ValueError(f"Unknown image fixture: {fixture}. See /fixtures for available IDs.")
        return self.rows("""SELECT p.* FROM image_matches m JOIN pages p ON p.id=m.page_id
                            WHERE m.fixture_id=? ORDER BY m.position""", (fixture,))

    def stats(self):
        return {table: self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
                for table in ("entities", "pages", "links", "image_fixtures")}
