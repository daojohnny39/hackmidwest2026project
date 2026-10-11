"""Platform table, account/post patterns, and URL normalization (spec 1.4).

Every seeded mock page is classified here so the patterns stay in step with
mock_internet/seed.py.
"""

import unittest

from mock_internet.seed import build_world
from pipeline.platforms import (
    PLATFORM_LABELS,
    account_handle,
    account_url_for,
    is_account_url,
    is_post_url,
    normalize_url,
    page_kind,
    platform_for,
    post_author_handle,
)

MOCK = "http://127.0.0.1:8765"


class NormalizeTests(unittest.TestCase):
    def test_live_urls(self):
        cases = {
            "HTTPS://WWW.LinkedIn.com/in/Alex-Morgan/": "https://linkedin.com/in/Alex-Morgan",
            "https://x.com/alexbuilds#replies": "https://x.com/alexbuilds",
            "https://twitter.com/alexbuilds/status/101?s=20&t=abc": "https://x.com/alexbuilds/status/101",
            "https://example.com:443/a?utm_source=x&id=7&fbclid=1": "https://example.com/a?id=7",
            "http://example.com:80/": "http://example.com",
            "https://example.com:8443/a": "https://example.com:8443/a",
            "https://m.facebook.com/profile.php?id=123": "https://facebook.com/profile.php?id=123",
            "https://example.com/a%2Fb/c": "https://example.com/a%2Fb/c",
            "https://example.com/%7Euser": "https://example.com/~user",
            "https://example.com/a b": "https://example.com/a%20b",
            "https://medium.com/@writer/": "https://medium.com/@writer",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_url(raw), expected)

    def test_rejects_non_pages(self):
        for raw in ("", "   ", "mailto:a@b.test", "javascript:alert(1)", "ftp://example.com/x",
                    "https://user:pass@example.com/", "https://example.com:99999/", "//", None):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_url(raw))

    def test_relative_links_resolve_against_base(self):
        base = "https://riverbend-news.test/people/alex-morgan-transit"
        self.assertEqual(normalize_url("../about", base=base), "https://riverbend-news.test/about")
        self.assertEqual(normalize_url("#top", base=base), base)

    def test_mock_local_urls_unwrap_only_on_the_exact_origin(self):
        local = f"{MOCK}/web/fieldnotes.test/@alexbuilds"
        self.assertEqual(normalize_url(local, mock_origin=MOCK), "https://fieldnotes.test/@alexbuilds")
        # Ephemeral test ports work; another port or host is just a URL.
        self.assertEqual(normalize_url("http://127.0.0.1:50123/web/linkup.test/in/alexbuilds",
                                       mock_origin="http://127.0.0.1:50123"), "https://linkup.test/in/alexbuilds")
        self.assertEqual(normalize_url("http://127.0.0.1:9999/web/linkup.test/in/x", mock_origin=MOCK),
                         "http://127.0.0.1:9999/web/linkup.test/in/x")
        self.assertEqual(normalize_url(local), "http://127.0.0.1:8765/web/fieldnotes.test/@alexbuilds")
        # Mock chrome (home page, images, search) is not a page link.
        for chrome in (f"{MOCK}/", f"{MOCK}/images/alex-front.svg", f"{MOCK}/search.json?q=a", "/"):
            with self.subTest(chrome=chrome):
                self.assertIsNone(normalize_url(chrome, base=f"{MOCK}/web/x.test/a", mock_origin=MOCK))

    def test_normalization_is_idempotent(self):
        for raw in ("https://www.x.com/a/status/1?s=2#x", "https://example.com/a%2Fb?q=1&utm_x=2",
                    f"{MOCK}/web/chatter.test/alexbuilds"):
            once = normalize_url(raw, mock_origin=MOCK)
            self.assertEqual(normalize_url(once, mock_origin=MOCK), once)


class ClassifyTests(unittest.TestCase):
    def test_platform_table(self):
        cases = {
            "https://linkedin.com/in/a": "linkedin", "https://linkup.test/in/a": "linkedin",
            "https://x.com/a": "x", "https://chatter.test/a": "x",
            "https://instagram.com/a": "instagram", "https://photogram.test/a": "instagram",
            "https://github.com/a": "github", "https://codeforge.test/a": "github",
            "https://facebook.com/a": "facebook",
            "https://medium.com/@a": "blog", "https://a.substack.com": "blog", "https://fieldnotes.test/@a": "blog",
            "https://riverbend-news.test/p": "news", "https://syndicated.test/p": "news",
            "https://archive.test/p": "other", "https://notlinkedin.com/in/a": "other",
        }
        for url, platform in cases.items():
            with self.subTest(url=url):
                self.assertEqual(platform_for(url), platform)
                self.assertIn(platform, PLATFORM_LABELS)

    def test_account_patterns_are_anchored(self):
        accounts = {
            "https://linkup.test/in/alexbuilds": "alexbuilds",
            "https://chatter.test/alexbuilds": "alexbuilds",
            "https://fieldnotes.test/@alexbuilds": "alexbuilds",
            "https://github.com/octo-cat": "octo-cat",
            "https://instagram.com/some.name": "some.name",
            "https://facebook.com/profile.php?id=42": "42",
            "https://writer.substack.com": "writer",
        }
        for url, handle in accounts.items():
            with self.subTest(url=url):
                self.assertEqual(account_handle(url), handle)
                self.assertTrue(is_account_url(url))
        for url in ("https://fieldnotes.test/@alexbuilds/old-project", "https://photogram.test/samdraws/private-album",
                    "https://github.com/octocat/hello-world", "https://x.com/explore", "https://x.com/home",
                    "https://linkedin.com/company/acme", "https://chatter.test/alexbuilds/status/101",
                    "https://instagram.com/p/AbC123", "https://facebook.com/profile.php", "https://github.com",
                    "https://archive.test/speakers/alex-morgan-2021", "https://medium.com/some-article"):
            with self.subTest(url=url):
                self.assertIsNone(account_handle(url))

    def test_posts(self):
        self.assertTrue(is_post_url("https://chatter.test/alexbuilds/status/101"))
        self.assertEqual(post_author_handle("https://x.com/alexbuilds/status/101"), "alexbuilds")
        self.assertEqual(account_url_for("https://chatter.test/alexbuilds/status/101"), "https://chatter.test/alexbuilds")
        self.assertTrue(is_post_url("https://instagram.com/p/AbC123"))
        self.assertIsNone(post_author_handle("https://instagram.com/p/AbC123"))
        self.assertIsNone(account_url_for("https://instagram.com/p/AbC123"))
        self.assertFalse(is_post_url("https://chatter.test/alexbuilds"))
        # instagram.com/<handle>/p|reel/<id> names its author, as Instagram's og:url does.
        for url in ("https://instagram.com/some.name/reel/AbC123", "https://www.instagram.com/some.name/p/AbC123/"):
            with self.subTest(url=url):
                self.assertEqual(page_kind(url), "post")
                self.assertIsNone(account_handle(url))
                self.assertEqual(account_url_for(url), "https://instagram.com/some.name")
        # A site path in the handle's place names no one.
        self.assertIsNone(post_author_handle("https://x.com/i/status/101"))
        self.assertIsNone(account_url_for("https://instagram.com/explore/p/AbC123"))

    def test_page_kind(self):
        self.assertEqual(page_kind("https://chatter.test/alexbuilds/status/101"), "post")
        self.assertEqual(page_kind("https://chatter.test/alexbuilds"), "profile")
        self.assertEqual(page_kind("https://riverbend-news.test/people/alex"), "article")
        # A top-level Person supports an unknown site's URL, never a known platform's sub-page.
        self.assertEqual(page_kind("https://alexmorgan.dev/about", person_json_ld=True), "profile")
        self.assertEqual(page_kind("https://photogram.test/samdraws/private-album", person_json_ld=True), "article")

    def test_every_seeded_page(self):
        pages, _, _ = build_world()
        for page in pages:
            with self.subTest(url=page["url"]):
                self.assertEqual(normalize_url(page["url"]), page["url"])
                self.assertEqual(page_kind(page["url"]), page["kind"])
                expected = {"professional": "linkedin", "social": "x", "photos": "instagram",
                            "code": "github", "blog": "blog"}.get(page["platform"])
                if expected:
                    self.assertEqual(platform_for(page["url"]), expected)


if __name__ == "__main__":
    unittest.main()
