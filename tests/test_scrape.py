"""HTML scraping (spec 1.4): real mock pages, then hostile and malformed HTML."""

import json

from mock_internet.client import MockInternetClient
from pipeline.scrape import MAX_LINKS, TEXT_LIMIT, scrape_html
from tests.support import MockServerTestCase, OfflineTestCase

PROFILE = "https://linkup.test/in/alexbuilds"
MIRROR = "https://syndicated.test/transit/alex-morgan"
INTERVIEW = "https://riverbend-news.test/people/alex-morgan-transit"
LIVE_PAGE = "https://example.com/people/alex"


class MockPageTests(MockServerTestCase):
    def scrape(self, url):
        payload = MockInternetClient(self.mock_url).fetch(url)
        return scrape_html(payload["html"], page_url=url, fetched_url=payload["local_url"], mock_origin=self.mock_url)

    def test_profile_page(self):
        page = self.scrape(PROFILE)
        self.assertEqual(page.title, "Alex Morgan | Linkup")
        self.assertIn("transit maps", page.meta_description)
        self.assertEqual(page.og["og:type"], "profile")
        self.assertEqual(page.canonical_url, PROFILE)
        self.assertEqual(page.published_at, "2026-10-01")
        self.assertTrue(page.person)
        [person] = page.json_ld
        self.assertEqual(person["@type"], "Person")
        self.assertEqual(person["name"], "Alex Morgan")
        self.assertEqual(person["url"], PROFILE)
        self.assertEqual(person["worksFor"], {"@type": "Organization", "name": "Prairie Atlas Labs"})
        self.assertEqual(person["homeLocation"], "Kansas City, MO")
        self.assertEqual(sorted(person["sameAs"]), ["https://chatter.test/alexbuilds", "https://codeforge.test/alexbuilds"])
        # Local /web/ links come back as original URLs; the mock's own home link is dropped.
        self.assertEqual({link.url: link.label for link in page.outbound_links},
                         {"https://chatter.test/alexbuilds": "Social profile",
                          "https://codeforge.test/alexbuilds": "Projects"})
        # One line per text node; the "<role> at <workplace>. <bio>" body is a single line.
        self.assertIn("Software engineer at Prairie Atlas Labs. I build open-source transit maps and "
                      "volunteer at the Riverbend maker club.", page.text.splitlines())
        self.assertNotIn("127.0.0.1", json.dumps(page.json_ld) + page.text)

    def test_mirror_points_at_its_canonical_article(self):
        page = self.scrape(MIRROR)
        self.assertEqual(page.canonical_url, INTERVIEW)
        self.assertFalse(page.person)
        self.assertEqual(page.json_ld[0]["@type"], "Article")
        self.assertEqual(page.json_ld[0]["headline"], "Alex Morgan helps neighbors navigate Riverbend transit")

    def test_post_has_an_author(self):
        page = self.scrape("https://chatter.test/alexbuilds/status/101")
        [article] = page.json_ld
        self.assertEqual(article["author"], {"@type": "Person", "name": "Alex M."})
        self.assertEqual(article["datePublished"], "2026-10-03")
        self.assertFalse(page.person)  # a nested author is not a top-level Person


class HostileHtmlTests(OfflineTestCase):
    def scrape(self, html, url=LIVE_PAGE):
        return scrape_html(html, page_url=url, fetched_url=url)

    def test_bounds_text_and_links(self):
        links = "".join(f'<a href="/p/{i}">p{i}</a>' for i in range(MAX_LINKS + 50))
        page = self.scrape(f"<body><p>{'word ' * 3000}</p>{links}</body>")
        self.assertEqual(len(page.text), TEXT_LIMIT)
        self.assertEqual(len(page.outbound_links), MAX_LINKS)

    def test_drops_unusable_links_and_self_links(self):
        html = ('<body><a href="javascript:alert(1)">x</a><a href="mailto:a@b.test">m</a>'
                '<a href="#top">self</a><a href="https://user:pw@evil.test/">creds</a>'
                '<a href="../team?utm_source=x">Team</a><a href="../team">again</a></body>')
        page = self.scrape(html)
        self.assertEqual([(link.url, link.label) for link in page.outbound_links], [("https://example.com/team", "Team")])

    def test_base_href_resolves_relative_links(self):
        page = self.scrape('<head><base href="https://cdn.example.org/root/"></head><body><a href="a">A</a></body>')
        self.assertEqual(page.outbound_links[0].url, "https://cdn.example.org/root/a")

    def test_scripts_and_styles_are_not_text(self):
        page = self.scrape("<body><script>var secret = 1;</script><style>p{}</style><p>Visible</p></body>")
        self.assertEqual(page.text, "Visible")

    def test_json_ld_is_trimmed_finite_and_bounded(self):
        blocks = [
            '{"@type": "Person", "name": "A", "score": NaN}',  # rejected: NaN
            "not json",
            '{"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "Org"},'
            ' {"@type": "Person", "name": "Graph Person", "sameAs": ["https://x.com/gp", "javascript:x"],'
            ' "email": "gp@example.com"}]}',
            '{"@type": "ProfilePage", "mainEntity": {"@type": "Person", "name": "Main", "jobTitle": "Engineer"}}',
            '[{"@type": "NewsArticle", "headline": "' + "h" * 5000 + '", "articleBody": "dropped"}]',
        ]
        html = "".join(f'<script type="application/ld+json">{block}</script>' for block in blocks)
        page = self.scrape(f"<head>{html}</head><body></body>")
        self.assertTrue(page.person)
        by_name = {block.get("name", block.get("@type")): block for block in page.json_ld}
        self.assertEqual(set(by_name), {"Graph Person", "Main", "NewsArticle"})
        self.assertEqual(by_name["Graph Person"]["sameAs"], ["https://x.com/gp"])
        self.assertNotIn("email", by_name["Graph Person"])
        self.assertEqual(by_name["Main"]["jobTitle"], "Engineer")
        self.assertEqual(len(by_name["NewsArticle"]["headline"]), 2000)
        self.assertNotIn("articleBody", by_name["NewsArticle"])

    def test_published_at_falls_back_to_json_ld(self):
        page = self.scrape('<script type="application/ld+json">{"@type": "BlogPosting", '
                           '"datePublished": "2025-01-02"}</script>')
        self.assertEqual(page.published_at, "2025-01-02")

    def test_empty_and_broken_html(self):
        for html in ("", "<html", "<<<>>>", "\x00\x01"):
            with self.subTest(html=html):
                page = self.scrape(html)
                self.assertEqual(page.outbound_links, [])
                self.assertEqual(page.json_ld, [])
