from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from html.parser import HTMLParser
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from mock_internet.client import MockHTTPError, MockInternetClient
from mock_internet.db import Corpus, connect, initialize
from mock_internet.server import create_server


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src", "action"):
                self.hrefs.append(value)


class DatabaseTests(unittest.TestCase):
    def test_seed_persists_and_reset_restores_world(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "world.sqlite3"
            initialize(path)
            original = Corpus(path).page("alex-dev-professional")["body"]
            with closing(connect(path)) as connection, connection:
                connection.execute("UPDATE pages SET body='Locally edited' WHERE id='alex-dev-professional'")
            initialize(path)
            self.assertEqual(Corpus(path).page("alex-dev-professional")["body"], "Locally edited")
            initialize(path, reset=True)
            self.assertEqual(Corpus(path).page("alex-dev-professional")["body"], original)
            with closing(connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The entire integration suite rejects non-loopback socket connections.
        original_connect = socket.socket.connect

        def local_connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                if not ipaddress.ip_address(address[0]).is_loopback:
                    raise AssertionError(f"Test attempted an external connection: {address}")
            return original_connect(sock, address)

        cls.guard = patch.object(socket.socket, "connect", local_connect)
        cls.guard.start()
        cls.folder = tempfile.TemporaryDirectory()
        cls.path = Path(cls.folder.name) / "test.sqlite3"
        cls.server = create_server(port=0, db_path=cls.path)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = MockInternetClient(cls.server.base_url)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.folder.cleanup()
        cls.guard.stop()

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_health_and_browser_entrypoint(self):
        status, _, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["database"]["pages"], 47)
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("A small, fictional internet", body)

    def test_query_retrieval_namesakes_and_filters(self):
        results = self.client.search('"Alex Morgan"', num=100)["organic_results"]
        text = json.dumps(results)
        self.assertIn("Prairie Atlas Labs", text)
        self.assertIn("Cedar Light Studio", text)
        self.assertNotIn("Garden coordinator", text)
        filtered = self.client.search('"Alex Morgan" -photographer', platform="professional")["organic_results"]
        self.assertEqual(len(filtered), 1)
        self.assertIn("alexbuilds", filtered[0]["link"])
        for query, options in (("site:chatter.test community", {}), ("community", {"site": "chatter.test"})):
            rows = self.client.search(query, **options)["organic_results"]
            self.assertTrue(rows)
            self.assertTrue(all(row["source"] == "chatter.test" for row in rows))
        self.assertEqual(self.client.search("site:atter.test community")["organic_results"], [])

    def test_unknown_query_and_empty_scenario(self):
        self.assertEqual(self.client.search("nonexistentword123")["organic_results"], [])
        self.assertEqual(self.client.search("Alex Morgan", scenario="empty")["organic_results"], [])
        self.assertEqual(self.client.reverse_image("unknown")["image_results"], [])

    def test_pagination_is_stable_and_followable(self):
        first = self.client.search("Alex Morgan", num=2)
        again = self.client.search("Alex Morgan", num=2)
        self.assertEqual(first, again)
        parsed = urlsplit(first["serpapi_pagination"]["next"])
        status, _, raw = self.request(parsed.path + "?" + parsed.query)
        self.assertEqual(status, 200)
        second = json.loads(raw)
        self.assertEqual(second["organic_results"][0]["position"], 3)
        self.assertFalse(set(row["link"] for row in first["organic_results"]) &
                         set(row["link"] for row in second["organic_results"]))
        tail = self.client.search("Alex Morgan", start=1000)
        self.assertEqual(tail["organic_results"], [])
        self.assertNotIn("serpapi_pagination", tail)

    def test_serpapi_contract_and_credentials_are_not_echoed(self):
        status, _, raw = self.request("/search.json?" + urlencode({
            "engine": "yandex_images", "url": "mock://alex-front", "api_key": "secret-test-key"}))
        self.assertEqual(status, 200)
        self.assertNotIn("secret-test-key", raw)
        payload = json.loads(raw)
        self.assertEqual(payload["search_metadata"]["status"], "Success")
        self.assertEqual(len(payload["image_results"]), 4)
        result = payload["image_results"][0]
        self.assertEqual(result["source"], "linkup.test")
        self.assertIsInstance(result["thumbnail"]["link"], str)
        self.assertTrue(result["link"].startswith(self.server.base_url))

    def test_every_image_fixture_url_works_without_external_images(self):
        for fixture in self.client.fixtures()["image_fixtures"]:
            status, headers, _ = self.request(urlsplit(fixture["url"]).path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/svg+xml")
            status, _, raw = self.request("/search?" + urlencode({"engine": "yandex_images", "url": fixture["url"]}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["image_results"], self.client.reverse_image(fixture["id"])["image_results"])

    def test_multi_image_search_scrape_and_link_discovery(self):
        hits = []
        for fixture in ("alex-front", "alex-left", "alex-right"):
            hits += self.client.reverse_image(fixture)["image_results"]
        unique = {hit["link"] for hit in hits}
        self.assertLess(len(unique), len(hits))
        self.assertEqual(len(unique), 10)
        scraped, failures = [], []
        for url in unique:
            try:
                scraped.append(self.client.fetch(url))
            except MockHTTPError as exc:
                failures.append(exc.status)
        self.assertEqual(len(scraped), 9)
        self.assertEqual(failures, [404])
        self.assertTrue(any(page["links"] for page in scraped))
        self.assertEqual(self.client.stats()["external_requests"], 0)

    def test_follow_every_link_in_the_corpus(self):
        corpus = Corpus(self.path)
        for page in corpus.rows("SELECT * FROM pages"):
            try:
                payload = self.client.fetch(page["url"])
                status = 200
            except MockHTTPError as exc:
                payload, status = exc.payload, exc.status
            self.assertEqual(status, page["status_code"])
            status, _, html = self.request(urlsplit(payload["local_url"]).path)
            self.assertEqual(status, page["status_code"])
            self.assertEqual(html, payload["html"])
            parser = Links()
            parser.feed(html)
            for link in parser.hrefs:
                self.assertTrue(link.startswith("/") or link.startswith(self.server.base_url + "/"), link)
            for link in payload["links"]:
                self.assertIsNotNone(corpus.page_by_url(link["url"]))
                target_status, _, _ = self.request(urlsplit(link["local_url"]).path)
                self.assertIn(target_status, (200, 403, 404))

    def test_search_and_html_do_not_leak_ground_truth(self):
        search = self.client.search("Alex Morgan", num=100)
        raw = json.dumps(search)
        self.assertNotIn("entity_id", raw)
        self.assertNotIn("alex-dev", raw)
        for hit in search["organic_results"]:
            status, _, body = self.request(urlsplit(hit["link"]).path)
            self.assertIn(status, (200, 404))
            self.assertNotIn("entity_id", body)
            self.assertNotIn("alex-dev", body)
        profile = self.client.fetch("https://linkup.test/in/alexbuilds")
        self.assertIn('application/ld+json', profile["html"])
        self.assertIn('"sameAs"', profile["html"])

    def test_ambiguous_duplicate_and_stale_scenarios(self):
        ambiguous = self.client.reverse_image("alex-front", scenario="ambiguous")["image_results"]
        self.assertTrue(any("alexframes" in row["link"] for row in ambiguous))
        duplicate = self.client.reverse_image("alex-front", scenario="duplicates")["image_results"]
        self.assertEqual(duplicate[0]["link"], duplicate[1]["link"])
        mirror = self.client.fetch("https://syndicated.test/transit/alex-morgan")
        self.assertIn('rel="canonical" href="' + self.server.base_url + '/web/riverbend-news.test/', mirror["html"])
        stale = self.client.reverse_image("alex-front", scenario="stale")["image_results"]
        self.assertTrue(any(row["date"] == "2021-06-12" for row in stale))
        self.assertTrue(any("old-project" in row["link"] for row in stale))

    def test_failures_are_real_http_statuses(self):
        for scenario, expected in (("rate_limited", 429), ("server_error", 503)):
            for action in (lambda: self.client.search("Alex", scenario=scenario),
                           lambda: self.client.fetch("https://linkup.test/in/alexbuilds", scenario=scenario)):
                with self.assertRaises(MockHTTPError) as ctx:
                    action()
                self.assertEqual(ctx.exception.status, expected)
                self.assertEqual(ctx.exception.headers["Retry-After"], "1")
        with self.assertRaises(MockHTTPError) as ctx:
            self.client.fetch("https://photogram.test/samdraws/private-album")
        self.assertEqual(ctx.exception.status, 403)

    def test_delay_and_client_timeout(self):
        before = time.monotonic()
        self.client.search("Alex", delay_ms=30)
        self.assertGreaterEqual(time.monotonic() - before, 0.025)
        with self.assertRaises(TimeoutError):
            MockInternetClient(self.server.base_url, timeout=0.01).search("Alex", delay_ms=100)

    def test_parallel_requests(self):
        def run(index):
            fixture = ("alex-front", "sam-front")[index % 2]
            return self.client.reverse_image(fixture)["image_results"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(16)))
        self.assertTrue(all(len(rows) == 4 for rows in results))

    def test_invalid_parameters_return_actionable_errors(self):
        for query in ({}, {"q": "Alex", "num": 0}, {"q": "Alex", "num": "x"},
                      {"q": "Alex", "num": 101}, {"q": "Alex", "start": -1},
                      {"q": "Alex", "scenario": "typo"}, {"q": "Alex", "engine": "unknown"},
                      {"q": "Alex", "platform": "typo"}, {"q": '"Alex'},
                      {"q": "Alex", "delay_ms": 5001}, {"engine": "yandex_images"},
                      {"engine": "yandex_images", "image_id": "misspelled"},
                      {"engine": "yandex_images", "url": "https://external.test/images/alex-front.svg"}):
            status, _, body = self.request("/search?" + urlencode(query))
            self.assertEqual(status, 400, query)
            self.assertIn("error", json.loads(body))

    def test_unknown_urls_never_fall_back_to_real_internet(self):
        for url in ("https://www.example.com/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
            with self.assertRaises(MockHTTPError) as ctx:
                self.client.fetch(url)
            self.assertEqual(ctx.exception.status, 404)
            self.assertIn("no external request", ctx.exception.payload["error"])

    def test_scrape_url_normalization(self):
        original = self.client.fetch("https://linkup.test/in/alexbuilds")
        normalized = self.client.fetch("http://LINKUP.test/in/alexbuilds/?utm_source=test#bio")
        self.assertEqual(original, normalized)

    def test_client_ignores_proxy_settings(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://external.test:8888", "ALL_PROXY": "http://external.test:8888", "NO_PROXY": ""}):
            self.assertTrue(self.client.search("Alex")["organic_results"])

    def test_client_rejects_nonlocal_endpoints(self):
        for base in ("https://serpapi.com", "http://example.com", "http://localhost.evil.test:8765",
                     "http://127.0.0.1@evil.test", "http://127.0.0.1:8765/path",
                     "http://127.0.0.1:8765?x=1", "http://127.0.0.1:8765#x", "http://127.0.0.1:0"):
            with self.assertRaises(ValueError, msg=base):
                MockInternetClient(base)

    def test_client_does_not_follow_redirects(self):
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "https://external.test")
                self.send_header("X-Mock-Internet", "true")
                self.end_headers()
                self.wfile.write(b'{"error":"redirect"}')

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(MockHTTPError) as ctx:
                MockInternetClient(f"http://127.0.0.1:{server.server_port}").search("Alex")
            self.assertEqual(ctx.exception.status, 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
