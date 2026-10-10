"""Search response parsing, error mapping, and bounded retries (spec 1.1, 1.7)."""

import asyncio
from datetime import datetime, timezone

from pipeline.search.base import (
    BackendError, MAX_HITS, parse_image_results, parse_retry_after, status_error, with_retries,
)
from tests.support import OfflineTestCase

MOCK = "http://127.0.0.1:8765"


class ParseTests(OfflineTestCase):
    def test_missing_fields_default_and_rank_from_order(self):
        payload = {"search_metadata": {"id": "abc", "status": "Success"},
                   "image_results": [{"link": "https://a.example/x"}, {"link": "https://b.example/y", "title": "B",
                                                                        "thumbnail": "https://t.example/1.jpg"}]}
        result = parse_image_results(payload)
        self.assertEqual(result.request_id, "abc")
        self.assertEqual([(hit.position, hit.title, hit.snippet, hit.source) for hit in result.hits],
                         [(1, "", "", ""), (2, "B", "", "")])
        self.assertEqual(result.hits[1].thumbnail_url, "https://t.example/1.jpg")

    def test_provider_positions_are_kept_when_valid(self):
        rows = [{"position": 3, "link": "https://a.example"}, {"position": 7, "link": "https://b.example"}]
        self.assertEqual([hit.position for hit in parse_image_results({"image_results": rows}).hits], [3, 7])
        rows[1]["position"] = 3  # duplicates fall back to array order
        self.assertEqual([hit.position for hit in parse_image_results({"image_results": rows}).hits], [1, 2])

    def test_empty_results(self):
        for payload in ({"search_metadata": {"status": "Success"}},
                        {"search_metadata": {"status": "Success"},
                         "error": "Yandex Images hasn't returned any results for this query."}):
            with self.subTest(payload=payload):
                self.assertEqual(parse_image_results(payload).hits, [])

    def test_provider_errors_fail_even_on_http_200(self):
        for payload in ({"error": "Invalid API key."}, {"search_metadata": {"status": "Error"}},
                        {"search_metadata": {"status": "Processing"}}):
            with self.subTest(payload=payload), self.assertRaises(BackendError) as caught:
                parse_image_results(payload)
            self.assertEqual(caught.exception.reason, "provider_error")
        for payload in ([], "x", {"image_results": {"not": "a list"}}):
            with self.subTest(payload=payload), self.assertRaises(BackendError) as caught:
                parse_image_results(payload)
            self.assertEqual(caught.exception.reason, "invalid_response")

    def test_similar_images_are_not_hits_and_extras_echo_nothing(self):
        payload = {"search_metadata": {"id": "r", "status": "Success", "json_endpoint": "https://serpapi.com/x?api_key=k"},
                   "search_parameters": {"url": "https://bucket.s3.amazonaws.com/k?X-Amz-Signature=s", "api_key": "k"},
                   "search_information": {"total_results": 5},
                   "similar_images": [{"link": "https://lookalike.example"}],
                   "image_results": [{"link": "javascript:alert(1)"}, {"link": "https://a.example/p",
                                                                         "thumbnail": {"link": "https://b.s3.amazonaws.com/x?X-Amz-Signature=y"}}]}
        result = parse_image_results(payload)
        self.assertEqual([hit.url for hit in result.hits], ["https://a.example/p"])
        self.assertIsNone(result.hits[0].thumbnail_url)
        self.assertEqual(result.extras, {"status": "Success", "total_results": 5, "skipped_hits": 1})

    def test_mock_original_urls_are_identities_only_in_mock_mode(self):
        row = {"link": f"{MOCK}/web/chatter.test/alexbuilds", "mock_original_url": "https://chatter.test/alexbuilds"}
        [hit] = parse_image_results({"image_results": [row]}, mock_origin=MOCK).hits
        self.assertEqual((hit.url, hit.fetch_url), ("https://chatter.test/alexbuilds", row["link"]))
        [live] = parse_image_results({"image_results": [row]}).hits
        self.assertEqual(live.url, row["link"])

    def test_hits_are_bounded(self):
        rows = [{"link": f"https://a.example/{i}"} for i in range(MAX_HITS + 20)]
        self.assertEqual(len(parse_image_results({"image_results": rows}).hits), MAX_HITS)


class ErrorTests(OfflineTestCase):
    def test_status_mapping(self):
        cases = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 429: "rate_limited", 500: "server_error", 503: "server_error", 418: "http_error"}
        for status, reason in cases.items():
            with self.subTest(status=status):
                error = status_error(status)
                self.assertEqual((error.reason, error.status_code, str(error)), (reason, status, reason))
        self.assertIsNone(BackendError("blocked", status_code=999).status_code)  # out of HTTP range

    def test_retry_after(self):
        now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(status_error(429, {"retry-after": "3"}).retry_after, 3.0)
        self.assertEqual(parse_retry_after("Sat, 10 Oct 2026 12:00:05 GMT", now=now), 5.0)
        self.assertEqual(parse_retry_after("Sat, 10 Oct 2026 11:00:00 GMT", now=now), 0.0)
        self.assertIsNone(parse_retry_after("soon", now=now))


class RetryTests(OfflineTestCase):
    def run_retries(self, errors, *, deadline=100.0):
        attempts, sleeps = [], []
        clock = [0.0]

        async def call():
            attempts.append(clock[0])
            if errors:
                raise errors.pop(0)
            return "ok"

        async def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        async def main():
            return await with_retries(call, deadline=deadline, clock=lambda: clock[0], sleep=sleep)

        return asyncio.run(main()), attempts, sleeps

    def test_retries_honor_retry_after_then_succeed(self):
        result, attempts, sleeps = self.run_retries([BackendError("rate_limited", retry_after=1.0),
                                                     BackendError("server_error")])
        self.assertEqual(result, ("ok", 2))
        self.assertEqual(sleeps, [1.0, 1.0])  # Retry-After, then backoff 0.5 * 2

    def test_at_most_two_retries(self):
        with self.assertRaises(BackendError) as caught:
            self.run_retries([BackendError("rate_limited", retry_after=0)] * 5)
        self.assertEqual(caught.exception.retries, 2)

    def test_no_retry_for_client_errors_or_past_the_deadline(self):
        for error in (BackendError("not_found"), BackendError("forbidden"), BackendError("timeout"),
                      BackendError("unauthorized")):
            with self.subTest(reason=error.reason), self.assertRaises(BackendError) as caught:
                self.run_retries([error])
            self.assertEqual(caught.exception.retries, 0)
        with self.assertRaises(BackendError) as caught:
            self.run_retries([BackendError("rate_limited", retry_after=30)], deadline=10)
        self.assertEqual(caught.exception.retries, 0)
