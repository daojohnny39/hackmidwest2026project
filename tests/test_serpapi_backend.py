"""SerpApi backend and guarded live fetches, offline (spec 1.1, 1.2, 1.7).

httpx.MockTransport stands in for SerpApi and web pages, and a fake host stands
in for S3. The socket guard from OfflineTestCase stays on throughout.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from pipeline.contracts import CaptureImage, UploadRecord
from pipeline.search.base import BackendError, parse_image_results
from pipeline.search.serpapi_backend import (
    MAX_HTML_BYTES, SerpApiBackend, check_url, page_client, sanitize,
)
from tests.support import OfflineTestCase

KEY = "serpapi-test-key-not-real"
SIGNED = "https://faces.s3.amazonaws.com/k.jpg?X-Amz-Signature=deadbeef"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
IMAGE = CaptureImage(image_id="img-1", file="img-1.jpg", angle="front")
RESPONSE = {
    "search_metadata": {"id": "serp-1", "status": "Success", "json_endpoint": f"https://serpapi.com/x?api_key={KEY}"},
    "search_parameters": {"engine": "yandex_images", "url": SIGNED, "api_key": KEY},
    "image_results": [{"position": 1, "title": "Pat | LinkedIn", "link": "https://www.linkedin.com/in/pat/",
                       "source": "linkedin.com", "snippet": "Engineer", "thumbnail": "https://t.example/1.jpg",
                       "original_image": {"link": SIGNED}}],
    "similar_images": [{"link": "https://lookalike.example"}],
}


class FakeHost:
    def __init__(self, fail=False):
        self.fail, self.uploads, self.deleted = fail, 0, []

    def upload(self, data):
        if self.fail:
            raise RuntimeError(f"boom {SIGNED}")
        self.uploads += 1
        return SIGNED, UploadRecord(bucket="faces", key=f"k{self.uploads}.jpg")

    def delete(self, record):
        self.deleted.append(record.key)

    def close(self):
        self.closed = True


class SearchTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.folder / "img-1.jpg").write_bytes(JPEG)
        self.requests = []

    def backend(self, responses, *, host=None, refresh=False):
        responses = list(responses)

        def handler(request):
            self.requests.append(request)
            status, body, headers = responses.pop(0)
            return httpx.Response(status, json=body, headers=headers)

        self.host = host or FakeHost()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return SerpApiBackend(api_key=KEY, capture_dir=self.folder, host=self.host, cache_dir=self.folder / "cache",
                              refresh=refresh, search_client=client)

    def test_search_request_and_sanitized_response(self):
        backend = self.backend([(200, RESPONSE, {})])
        payload = asyncio.run(backend.reverse_image(IMAGE))
        params = parse_qs(urlsplit(str(self.requests[0].url)).query)
        self.assertEqual({name: values[0] for name, values in params.items()},
                         {"engine": "yandex_images", "url": SIGNED, "tab": "about", "api_key": KEY})
        dumped = json.dumps(payload)
        self.assertNotIn(KEY, dumped)
        self.assertNotIn("X-Amz", dumped)
        self.assertNotIn("similar_images", payload)
        [hit] = parse_image_results(payload).hits
        self.assertEqual((hit.url, hit.position), ("https://linkedin.com/in/pat", 1))
        cached = list((self.folder / "cache").iterdir())
        self.assertEqual(len(cached), 1)
        self.assertNotIn(KEY, cached[0].read_text())

    def test_cache_is_reused_unless_refresh(self):
        asyncio.run(self.backend([(200, RESPONSE, {})]).reverse_image(IMAGE))
        backend = self.backend([])
        asyncio.run(backend.reverse_image(IMAGE))
        self.assertEqual((len(self.requests), self.host.uploads), (1, 0))  # no search, no upload
        self.assertEqual(backend.cache_hits, {"img-1"})
        asyncio.run(self.backend([(200, RESPONSE, {})], refresh=True).reverse_image(IMAGE))
        self.assertEqual(len(self.requests), 2)

    def test_errors_are_codes_and_never_cached(self):
        cases = [(401, "unauthorized", None), (429, "rate_limited", 7.0), (503, "server_error", None),
                 (400, "invalid_request", None)]
        for status, reason, retry_after in cases:
            with self.subTest(status=status):
                headers = {"Retry-After": "7"} if retry_after else {}
                backend = self.backend([(status, {"error": f"bad key {KEY}"}, headers)])
                with self.assertRaises(BackendError) as caught:
                    asyncio.run(backend.reverse_image(IMAGE))
                self.assertEqual((caught.exception.reason, caught.exception.retry_after), (reason, retry_after))
                self.assertNotIn(KEY, str(caught.exception))
        self.assertFalse((self.folder / "cache").exists())
        # A provider error on HTTP 200 is sanitized, uncached, and fails parsing.
        payload = asyncio.run(self.backend([(200, {"error": "Invalid API key."}, {})]).reverse_image(IMAGE))
        with self.assertRaises(BackendError):
            parse_image_results(payload)
        self.assertFalse((self.folder / "cache").exists())

    def test_connection_errors_never_show_the_request_url(self):
        def handler(request):
            raise httpx.ConnectError(f"failed {request.url}")

        backend = SerpApiBackend(api_key=KEY, capture_dir=self.folder, host=FakeHost(),
                                 search_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with self.assertRaises(BackendError) as caught:
            asyncio.run(backend.reverse_image(IMAGE))
        self.assertEqual(str(caught.exception), "connection_error")
        self.assertIsNone(caught.exception.__cause__)

    def test_one_upload_across_retries_then_release_deletes_it(self):
        backend = self.backend([(503, {}, {}), (200, RESPONSE, {})])

        async def main():
            for attempt in range(2):
                try:
                    await backend.reverse_image(IMAGE)
                except BackendError:
                    pass
            await backend.release(IMAGE)
            await backend.release(IMAGE)  # idempotent

        asyncio.run(main())
        self.assertEqual((self.host.uploads, self.host.deleted), (1, ["k1.jpg"]))

    def test_hosting_failure_is_a_code(self):
        backend = self.backend([], host=FakeHost(fail=True))
        with self.assertRaises(BackendError) as caught:
            asyncio.run(backend.reverse_image(IMAGE))
        self.assertEqual(str(caught.exception), "hosting_error")
        self.assertEqual(self.requests, [])

    def test_aclose_deletes_leftover_uploads(self):
        backend = self.backend([(200, RESPONSE, {})])

        async def main():
            await backend.reverse_image(IMAGE)
            await backend.aclose()

        asyncio.run(main())
        self.assertEqual(self.host.deleted, ["k1.jpg"])
        self.assertTrue(self.host.closed)  # the tunnel host stops its tunnel here

    def test_sanitize_keeps_only_allowlisted_fields(self):
        clean = sanitize(RESPONSE)
        self.assertEqual(set(clean), {"search_metadata", "image_results"})
        self.assertEqual(clean["search_metadata"], {"id": "serp-1", "status": "Success"})
        self.assertNotIn("original_image", clean["image_results"][0])


def html_response(body="<html><title>T</title><body>Hi</body></html>", status=200, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


class FetchTests(OfflineTestCase):
    def fetch(self, routes, url="https://example.com/a"):
        def handler(request):
            route = routes[str(request.url)]
            return route() if callable(route) else route

        pages = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        backend = SerpApiBackend(api_key=KEY, capture_dir=Path("."), host=FakeHost(), pages=pages,
                                 search_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)))
        return asyncio.run(backend.fetch(url))

    def assert_fails(self, routes, reason, url="https://example.com/a"):
        with self.assertRaises(BackendError) as caught:
            self.fetch(routes, url)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_follows_up_to_three_redirects(self):
        routes = {"https://example.com/a": httpx.Response(301, headers={"location": "/b"}),
                  "https://example.com/b": httpx.Response(302, headers={"location": "https://www.example.com/c#x"}),
                  "https://www.example.com/c": html_response()}
        page = self.fetch(routes)
        self.assertEqual((page.requested_url, page.final_url, page.status_code), ("https://example.com/a",
                                                                                  "https://example.com/c", 200))
        loop = {f"https://example.com/{i}": httpx.Response(302, headers={"location": f"/{i + 1}"}) for i in range(6)}
        self.assert_fails(loop, "too_many_redirects", "https://example.com/0")

    def test_redirects_to_private_destinations_are_blocked(self):
        for target in ("http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "http://localhost/",
                       "http://10.0.0.8/", "http://[::1]/", "https://user:pw@example.com/", "ftp://example.com/",
                       "http://example.com:8080/", "http://100.82.86.83:11434/"):
            with self.subTest(target=target):
                routes = {"https://example.com/a": httpx.Response(302, headers={"location": target})}
                self.assert_fails(routes, "blocked_destination")

    def test_login_walls_errors_and_content_types(self):
        wall = {"https://example.com/a": httpx.Response(302, headers={"location": "https://example.com/authwall?x=1"}),
                "https://example.com/authwall?x=1": html_response()}
        self.assertEqual(self.assert_fails(wall, "login_wall").status_code, 200)
        self.assert_fails({"https://example.com/a": html_response(status=403)}, "forbidden")
        self.assert_fails({"https://example.com/a": html_response(status=999)}, "forbidden")
        error = self.assert_fails({"https://example.com/a": html_response(status=429, **{"retry-after": "2"})},
                                  "rate_limited")
        self.assertEqual(error.retry_after, 2.0)
        pdf = httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        self.assert_fails({"https://example.com/a": pdf}, "unsupported_content")

    def test_html_is_capped(self):
        body = "<html><body>" + "x" * (MAX_HTML_BYTES * 2) + "</body></html>"
        page = self.fetch({"https://example.com/a": html_response(body)})
        self.assertLessEqual(len(page.html.encode()), MAX_HTML_BYTES)

    def test_connection_level_check_blocks_rebinding(self):
        async def private(host, port):
            return ["93.184.216.34", "192.168.1.10"]  # one private answer is enough to refuse

        async def main():
            async with page_client(private) as client:
                backend = SerpApiBackend(api_key=KEY, capture_dir=Path("."), host=FakeHost(), pages=client,
                                         search_client=httpx.AsyncClient())
                return await backend.fetch("https://rebind.example/")

        with self.assertRaises(BackendError) as caught:
            asyncio.run(main())
        self.assertEqual(caught.exception.reason, "blocked_destination")

    def test_check_url(self):
        for url in ("https://example.com/", "http://example.com:80/x", "https://93.184.216.34/"):
            check_url(url)
        for url in ("https://192.168.0.1/", "https://[fe80::1]/", "https://printer.local/", "https://a.internal/",
                    "file:///etc/passwd", "https://example.com:22/"):
            with self.subTest(url=url), self.assertRaises(BackendError):
                check_url(url)
