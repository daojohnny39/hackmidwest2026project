"""Trace face search client, offline (spec 1.1, 1.7).

httpx.MockTransport stands in for Trace's API v1, with replies shaped like its
openapi.json (scans, problem documents). The socket guard from OfflineTestCase stays on.
"""

import asyncio
import json
from unittest.mock import patch

import httpx

from pipeline.search.base import BackendError
from pipeline.search.trace_face import (
    TRACE_API, CheckFailed, KeyRejected, TraceFaceSearch, account_info, describe_account, is_preview_url,
)
from tests.support import OfflineTestCase

KEY = "trk_live_test-key-not-real"
PREVIEW = f"{TRACE_API}/scans/s1/previews/0"
MATCH = {"score": 93, "tier": "near_certain", "platform": "linkedin", "handle": "patlee",
         "url": "https://www.linkedin.com/in/patlee/", "preview_url": PREVIEW}
QUEUED = {"id": "s1", "status": "queued", "progress": 0, "locked": False, "match_count": 0, "matches": []}
RUNNING = {**QUEUED, "status": "running", "progress": 40}
DONE = {**QUEUED, "status": "done", "progress": 100, "match_count": 1, "matches": [MATCH]}
LOCKED = {**QUEUED, "status": "done", "progress": 100, "locked": True}


def problem(status, code, detail="nope"):
    return status, {"type": "about:blank", "title": "Problem", "status": status, "code": code, "detail": detail}


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TraceTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.requests = []

    def client(self, replies, *, scan_timeout=60.0):
        """replies: (method, path) -> list of (status, body); a body of bytes is sent as an image."""
        replies = {route: list(answers) for route, answers in replies.items()}

        def handler(request):
            self.requests.append(request)
            status, body = replies[(request.method, request.url.path)].pop(0)
            if isinstance(body, bytes):
                return httpx.Response(status, content=body, headers={"content-type": "image/webp"})
            content_type = "application/problem+json" if status >= 400 else "application/json"
            return httpx.Response(status, content=json.dumps(body), headers={"content-type": content_type})

        self.clock = FakeClock()
        search = TraceFaceSearch(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                 poll_interval=3.0, scan_timeout=scan_timeout, clock=self.clock, sleep=self.clock.sleep)
        return search

    def routes(self):
        return [(request.method, request.url.path) for request in self.requests]


class SearchTests(TraceTestCase):
    def test_upload_poll_and_matches(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED)],
                              ("GET", "/api/v1/scans/s1"): [(200, RUNNING), (200, DONE)]})
        scan_id, scan = asyncio.run(search.search("img-1", b"\xff\xd8\xffjpeg"))
        self.assertEqual((scan_id, scan["matches"]), ("s1", [MATCH]))
        self.assertEqual(self.routes(), [("POST", "/api/v1/scans"), ("GET", "/api/v1/scans/s1"),
                                         ("GET", "/api/v1/scans/s1")])
        upload = self.requests[0]
        self.assertEqual(upload.headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(len(upload.headers["Idempotency-Key"]), 32)
        self.assertIn(b'name="image"', upload.content)
        self.assertIn(b"\xff\xd8\xffjpeg", upload.content)
        self.assertTrue(all(KEY not in str(request.url) for request in self.requests))
        self.assertEqual(self.clock.sleeps, [3.0])

    def test_locked_scan_is_revealed(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, {**QUEUED, "locked": True})],
                              ("GET", "/api/v1/scans/s1"): [(200, LOCKED), (200, LOCKED), (200, DONE)],
                              ("POST", "/api/v1/scans/s1/reveal"): [(202, RUNNING)]})
        _, scan = asyncio.run(search.search("img-1", b"img"))
        self.assertEqual(scan["matches"], [MATCH])
        self.assertIn(("POST", "/api/v1/scans/s1/reveal"), self.routes())

    def test_no_credits(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, {**QUEUED, "locked": True})],
                              ("GET", "/api/v1/scans/s1"): [(200, LOCKED)],
                              ("POST", "/api/v1/scans/s1/reveal"): [problem(402, "insufficient_credits")]})
        with self.assertRaises(BackendError) as caught:
            asyncio.run(search.search("img-1", b"img"))
        self.assertEqual((caught.exception.reason, caught.exception.retryable), ("no_credits", False))

    def test_failed_scan_and_problem_codes(self):
        for code, reason in (("no_face", "no_face"), ("provider_unavailable", "server_error"), ("odd", "provider_error")):
            with self.subTest(code=code):
                search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED)],
                                      ("GET", "/api/v1/scans/s1"): [(200, {**QUEUED, "status": "failed",
                                                                           "error_code": code})]})
                with self.assertRaises(BackendError) as caught:
                    asyncio.run(search.search("img-1", b"img"))
                self.assertEqual(caught.exception.reason, reason)
        for status, code, reason in ((401, "unauthenticated", "unauthorized"), (422, "invalid_image", "unsupported_image"),
                                     (429, "rate_limited", "rate_limited"), (503, "", "server_error")):
            with self.subTest(status=status):
                search = self.client({("POST", "/api/v1/scans"): [problem(status, code, detail=f"bad {KEY}")]})
                with self.assertRaises(BackendError) as caught:
                    asyncio.run(search.search("img-1", b"img"))
                self.assertEqual((caught.exception.reason, caught.exception.status_code), (reason, status))
                self.assertNotIn(KEY, str(caught.exception))

    def test_retry_resumes_the_same_scan(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED)],
                              ("GET", "/api/v1/scans/s1"): [problem(503, ""), (200, DONE)]})

        async def twice():
            try:
                await search.search("img-1", b"img")
            except BackendError as error:
                self.assertTrue(error.retryable)
            return await search.search("img-1", b"img")

        self.assertEqual(asyncio.run(twice())[0], "s1")
        self.assertEqual(self.routes().count(("POST", "/api/v1/scans")), 1)  # never a second paid scan

    def test_keyed_upload_whose_scan_is_gone_starts_a_new_one(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED), (202, {**QUEUED, "id": "s2"})],
                              ("GET", "/api/v1/scans/s1"): [problem(404, "not_found")],
                              ("GET", "/api/v1/scans/s2"): [(200, {**DONE, "id": "s2"})]})
        scan_id, _ = asyncio.run(search.search("img-1", b"img", key="k" * 32))
        self.assertEqual(scan_id, "s2")
        keys = [request.headers["Idempotency-Key"] for request in self.requests if request.method == "POST"]
        self.assertEqual(keys[0], "k" * 32)
        self.assertNotEqual(keys[1], keys[0])

    def test_unfinished_scan_is_left_for_a_rerun(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED)],
                              ("GET", "/api/v1/scans/s1"): [(200, RUNNING)] * 5}, scan_timeout=9.0)

        async def run():
            with self.assertRaises(BackendError):
                await search.search("img-1", b"img", key="k" * 32)
            await search.delete("img-1")
            await search.aclose()

        asyncio.run(run())
        self.assertNotIn("DELETE", [method for method, _ in self.routes()])

    def test_timeout(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED)],
                              ("GET", "/api/v1/scans/s1"): [(200, RUNNING)] * 5}, scan_timeout=9.0)
        with self.assertRaises(BackendError) as caught:
            asyncio.run(search.search("img-1", b"img"))
        self.assertEqual(caught.exception.reason, "timeout")


class PreviewAndDeleteTests(TraceTestCase):
    def test_preview_needs_a_trace_url(self):
        search = self.client({("GET", "/api/v1/scans/s1/previews/0"): [(200, b"RIFFwebp")]})
        self.assertEqual(asyncio.run(search.preview(PREVIEW)), b"RIFFwebp")
        self.assertEqual(self.requests[0].headers["Authorization"], f"Bearer {KEY}")
        for url in ("https://evil.example/api/v1/scans/s1/previews/0", "http://traceaifacescan.app/api/v1/scans/s1/previews/0",
                    "https://traceaifacescan.app:8443/api/v1/scans/s1/previews/0",
                    "https://traceaifacescan.app/api/v1/account", None):
            with self.subTest(url=url):
                self.assertFalse(is_preview_url(url))
                with self.assertRaises(BackendError):
                    asyncio.run(search.preview(url))
        self.assertEqual(len(self.requests), 1)  # the key never went anywhere else

    def test_delete_and_aclose(self):
        search = self.client({("POST", "/api/v1/scans"): [(202, QUEUED), (202, {**QUEUED, "id": "s2"})],
                              ("GET", "/api/v1/scans/s1"): [(200, DONE)],
                              ("GET", "/api/v1/scans/s2"): [(200, {**DONE, "id": "s2"})],
                              ("DELETE", "/api/v1/scans/s1"): [problem(404, "not_found")],
                              ("DELETE", "/api/v1/scans/s2"): [(200, {})]})

        async def run():
            await search.search("img-1", b"img")
            await search.search("img-2", b"img")
            await search.delete("img-1")  # 404 is fine
            await search.delete("img-1")  # already forgotten: no request
            await search.aclose()  # deletes img-2's scan

        asyncio.run(run())
        self.assertEqual([route for route in self.routes() if route[0] == "DELETE"],
                         [("DELETE", "/api/v1/scans/s1"), ("DELETE", "/api/v1/scans/s2")])


class AccountTests(OfflineTestCase):
    def reply(self, status, body):
        return patch("pipeline.search.trace_face.httpx.get",
                     return_value=httpx.Response(status, json=body, request=httpx.Request("GET", TRACE_API)))

    def test_account_info(self):
        with self.reply(200, {"email": "pat@example.com", "credits": 5}) as get:
            info = account_info(KEY)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(describe_account(info), "credits: 5")
        self.assertEqual(describe_account({"account": {"credit_balance": 40, "name": "Pat"}}), "credit balance: 40")
        self.assertEqual(describe_account({"email": "pat@example.com"}), "no credit balance in the reply")
        with self.reply(401, {"code": "unauthenticated"}), self.assertRaises(KeyRejected):
            account_info(KEY)
        with self.reply(500, {}), self.assertRaises(CheckFailed):
            account_info(KEY)
        with patch("pipeline.search.trace_face.httpx.get", side_effect=httpx.ConnectError("down")), \
                self.assertRaises(CheckFailed):
            account_info(KEY)
