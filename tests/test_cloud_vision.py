"""Google Cloud Vision web detection client (V6's engine), offline. httpx.MockTransport stands in for Google."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from pipeline.search.base import BackendError
from pipeline.search.cloud_vision import CheckFailed, CloudVisionSearch, KeyRejected, check_key, vision_results
from pipeline.trace import tracing
from tests.support import OfflineTestCase, read_trace

KEY = "AIza-vision-test-key-not-real"
# Google's reply to a made-up key, recorded 2026-10-11 (the LocalizedMessage detail trimmed).
BAD_KEY = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                     "status": "INVALID_ARGUMENT",
                     "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID",
                                  "domain": "googleapis.com", "metadata": {"service": "vision.googleapis.com"}}]}}
DISABLED = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                      "message": "Cloud Vision API has not been used in project 123 before or it is disabled.",
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}]}}


class SearchTests(OfflineTestCase):
    def search(self, status, body, headers=None):
        def annotate(request):
            return httpx.Response(status, json=body, headers=headers)

        client = CloudVisionSearch(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(annotate)))

        async def go():
            try:
                return await client.search("img-1", b"\xff\xd8 jpeg")
            finally:
                await client.aclose()

        return asyncio.run(go())

    def assertFails(self, reason, status, body, headers=None):
        with self.assertRaises(BackendError) as caught:
            self.search(status, body, headers)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(KEY, str(caught.exception))
        return caught.exception

    def test_empty_detection_is_zero_results(self):
        self.assertEqual(self.search(200, {"responses": [{}]}), {})
        self.assertEqual(vision_results({}), {"search_metadata": {"status": "Success"}, "results": []})

    def test_errors_map_to_codes(self):
        self.assertFails("unauthorized", 400, BAD_KEY)
        self.assertFails("forbidden", 403, DISABLED)
        self.assertEqual(self.assertFails("rate_limited", 429, {"error": {"status": "RESOURCE_EXHAUSTED"}},
                                          {"Retry-After": "7"}).retry_after, 7.0)
        self.assertTrue(self.assertFails("server_error", 503, {}).retryable)
        self.assertFails("provider_error", 200, {"responses": [{"error": {"code": 3, "message": "Bad image data."}}]})
        self.assertFails("invalid_response", 200, {"responses": []})
        self.assertFails("invalid_response", 200, {"responses": [{"webDetection": []}]})

    def test_google_error_is_traced_without_the_key(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with tracing(folder, 1):
            self.assertFails("forbidden", 403, DISABLED)
        [error] = [line for line in read_trace(folder) if line.get("event") == "cloud_vision_http_error"]
        self.assertEqual((error["status"], error["reason"]), (403, "SERVICE_DISABLED"))
        self.assertIn("has not been used", error["message"])
        self.assertNotIn(KEY, str(read_trace(folder)))

    def test_results_skip_bad_urls_and_repeats(self):
        detection = {"pagesWithMatchingImages": [
            {"url": "https://a.example/p", "pageTitle": "<i>A</i>", "fullMatchingImages": [{"url": "ftp://x/a.jpg"}]},
            {"url": "https://bucket.s3.amazonaws.com/f.jpg?X-Amz-Signature=1"},
            "not a page"],
            "partialMatchingImages": [{"url": "https://img.example/b.jpg"}],
            "visuallySimilarImages": [{"url": "https://img.example/b.jpg"}, {}, {"url": 7}]}
        self.assertEqual(vision_results(detection)["results"], [
            {"link": "https://a.example/p", "title": "A", "snippet": "Cloud Vision: page showing part of the image",
             "thumbnail": None, "images": []},
            {"link": "https://img.example/b.jpg", "title": "", "snippet": "Cloud Vision: the same image, page not given",
             "thumbnail": None, "images": ["https://img.example/b.jpg"]}])


class CheckKeyTests(OfflineTestCase):
    def check(self, status, body):
        reply = httpx.Response(status, json=body, request=httpx.Request("POST", "https://vision.googleapis.com"))
        with patch("pipeline.search.cloud_vision.httpx.post", return_value=reply) as post:
            try:
                return check_key(KEY)
            finally:
                [call] = post.call_args_list
                self.assertEqual((call.kwargs["json"], call.kwargs["headers"]["X-Goog-Api-Key"]),
                                 ({"requests": []}, KEY))
                self.assertNotIn(KEY, call.args[0])

    def test_answers(self):
        self.assertEqual(self.check(200, {}), "the Cloud Vision API answered")
        self.assertEqual(self.check(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "x"}}),
                         "the Cloud Vision API answered")
        with self.assertRaisesRegex(KeyRejected, "API_KEY_INVALID"):
            self.check(400, BAD_KEY)
        with self.assertRaisesRegex(CheckFailed, "HTTP 403 SERVICE_DISABLED"):
            self.check(403, DISABLED)
        with self.assertRaisesRegex(CheckFailed, "HTTP 500"):
            self.check(500, {})

    def test_unreachable(self):
        with patch("pipeline.search.cloud_vision.httpx.post", side_effect=httpx.ConnectError("no route")):
            with self.assertRaisesRegex(CheckFailed, "ConnectError"):
                check_key(KEY)
