"""FaceCheck backend, offline (spec 1.1, 1.7).

httpx.MockTransport stands in for FaceCheck, with replies shaped like the live API's
(errors arrive on HTTP 200 with error and code). The socket guard from
OfflineTestCase stays on throughout.
"""

import asyncio
import hashlib
import io
import json
import math
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from pipeline.config import ConfigError, load_settings
from pipeline.contracts import CaptureArtifact, CaptureBundle, CaptureImage
from pipeline.search.base import BackendError, parse_image_results, with_retries
from pipeline.search.facecheck_backend import (
    FACECHECK_URL, FaceCheckBackend, InfoCheckFailed, TokenRejected, account_info, build_facecheck_backend,
    describe_info, score_band, to_payload,
)
from tests.support import OfflineTestCase

KEY = "facecheck-test-token-not-real"
IMAGE = CaptureImage(image_id="img-1", file="img-1.jpg", angle="front")
THUMB = "data:image/webp;base64,UklGRlIAAABXRUJQ"
UPLOADED = {"id_search": "srch-1", "message": "Image uploaded", "error": None, "code": None, "output": None}
WAITING = {"id_search": "srch-1", "message": "Searching...", "progress": 40, "error": None, "output": None}
DONE = {"id_search": "srch-1", "message": "Done", "progress": 100, "error": None, "output": {
    "items": [
        {"guid": "g1", "score": 61, "url": {"value": "https://example.com/lookalike"}, "base64": THUMB, "index": 2},
        {"guid": "g2", "score": 95, "url": {"value": "https://www.linkedin.com/in/pat/"}, "base64": THUMB, "index": 0},
        {"guid": "g3", "score": 88, "url": {"value": "https://github.com/pat"}, "base64": THUMB, "index": 1},
    ],
    "demo": False, "searchedFaces": 212417831, "tookSeconds": 21.5, "max_score": 95,
}}
TOKEN_ERROR = {"error": "Invalid API token! Get your token at https://facecheck.id/en/Face-Search/API",
               "code": "EXCEPTION", "output": None}


def jpeg_with_exif(size=(2000, 1500)) -> bytes:
    image = Image.new("RGB", size, (120, 90, 60))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FaceCheckTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.folder / "img-1.jpg").write_bytes(jpeg_with_exif())
        self.requests = []

    def backend(self, replies, *, demo=False, refresh=False, search_timeout=60.0, cache=True):
        replies = list(replies)

        def handler(request):
            self.requests.append(request)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            status, body = reply if isinstance(reply, tuple) else (200, reply)
            return httpx.Response(status, json=body)

        self.clock = FakeClock()
        backend = FaceCheckBackend(api_key=KEY, capture_dir=self.folder, demo=demo, refresh=refresh,
                                   cache_dir=self.folder / "cache" if cache else None,
                                   client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                   pages=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                   poll_interval=2.0, search_timeout=search_timeout, clock=self.clock,
                                   sleep=self.clock.sleep)
        self.addCleanup(lambda: asyncio.run(backend.aclose()))
        return backend

    def paths(self):
        return [request.url.path for request in self.requests]

    def cached(self):
        folder = self.folder / "cache"
        return sorted(folder.iterdir()) if folder.exists() else []


class SearchTests(FaceCheckTestCase):
    def test_upload_poll_and_convert(self):
        backend = self.backend([UPLOADED, WAITING, DONE])
        payload = asyncio.run(backend.reverse_image(IMAGE))

        self.assertEqual(self.paths(), ["/api/upload_pic", "/api/search", "/api/search"])
        for request in self.requests:
            self.assertEqual(request.headers["Authorization"], KEY)
            self.assertNotIn(KEY, str(request.url))
            self.assertEqual(f"{request.url.scheme}://{request.url.host}", FACECHECK_URL)
        self.assertEqual(json.loads(self.requests[1].content),
                         {"id_search": "srch-1", "with_progress": True, "status_only": False, "demo": False})
        self.assertEqual(self.clock.sleeps, [2.0])

        hits = parse_image_results(payload).hits
        self.assertEqual([(hit.position, hit.url, hit.face_score) for hit in hits],
                         [(1, "https://linkedin.com/in/pat", 95), (2, "https://github.com/pat", 88),
                          (3, "https://example.com/lookalike", 61)])
        self.assertEqual((hits[0].title, hits[0].source), ("", "linkedin.com"))
        self.assertEqual(hits[0].snippet, "FaceCheck face match 95/100 (certain match)")
        self.assertIsNone(hits[0].thumbnail_url)
        result = parse_image_results(payload)
        self.assertEqual((result.request_id, result.extras["demo"], result.extras["searched_faces"]),
                         ("srch-1", False, 212417831))

        [cache] = self.cached()
        self.assertTrue(cache.name.endswith("-facecheck.json"))
        for text in (json.dumps(payload), cache.read_text()):
            self.assertNotIn("base64", text)  # thumbnails of other people's faces are never kept
            self.assertNotIn(KEY, text)

    def test_upload_is_a_small_jpeg_without_metadata(self):
        backend = self.backend([UPLOADED, DONE])
        asyncio.run(backend.reverse_image(IMAGE))
        body = self.requests[0].content
        self.assertIn(b'name="images"', body)
        start = body.index(b"\xff\xd8\xff")
        with Image.open(io.BytesIO(body[start:])) as uploaded:
            self.assertEqual(uploaded.size, (1600, 1200))
            self.assertEqual(dict(uploaded.getexif()), {})
        self.assertNotIn(b"PhoneMaker", body)

    def test_demo_mode_is_sent_and_cached_separately(self):
        demo_done = {**DONE, "output": {**DONE["output"], "demo": True}}
        payload = asyncio.run(self.backend([UPLOADED, demo_done], demo=True).reverse_image(IMAGE))
        self.assertTrue(json.loads(self.requests[1].content)["demo"])
        self.assertTrue(parse_image_results(payload).extras["demo"])
        [cache] = self.cached()
        self.assertTrue(cache.name.endswith("-facecheck-demo.json"))
        # A real search never reuses demo results.
        self.assertTrue(self.backend([]).needs_search([IMAGE]))

    def test_cache_is_reused_unless_refresh(self):
        asyncio.run(self.backend([UPLOADED, DONE]).reverse_image(IMAGE))
        backend = self.backend([])
        self.assertFalse(backend.needs_search([IMAGE]))
        asyncio.run(backend.reverse_image(IMAGE))
        self.assertEqual(len(self.requests), 2)  # no new upload or search
        self.assertEqual(backend.cache_hits, {"img-1"})
        refreshed = self.backend([UPLOADED, DONE], refresh=True)
        self.assertTrue(refreshed.needs_search([IMAGE]))
        asyncio.run(refreshed.reverse_image(IMAGE))
        self.assertEqual(len(self.requests), 4)

    def test_provider_errors_are_codes_and_never_cached(self):
        cases = [
            (TOKEN_ERROR, "unauthorized"),
            ({"error": "Missing API Token.", "code": "INVALID_TOKEN"}, "unauthorized"),
            ({"error": "Not enough credits to search", "code": "NO_CREDITS"}, "no_credits"),
            ({"error": f"No face found ({KEY})", "code": "NO_FACE"}, "provider_error"),
        ]
        for reply, reason in cases:
            with self.subTest(reason=reason, code=reply["code"]):
                with self.assertRaises(BackendError) as caught:
                    asyncio.run(self.backend([reply]).reverse_image(IMAGE))
                self.assertEqual(caught.exception.reason, reason)
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn(KEY, str(caught.exception))
        # An error while polling is a code too.
        with self.assertRaises(BackendError) as caught:
            asyncio.run(self.backend([UPLOADED, {"error": "Search failed", "code": "SEARCH_ERROR"}]).reverse_image(IMAGE))
        self.assertEqual(caught.exception.reason, "provider_error")
        self.assertEqual(self.cached(), [])

    def test_http_errors_and_bad_replies(self):
        for reply, reason in (((503, {}), "server_error"), ((401, {}), "unauthorized"), ((429, {}), "rate_limited"),
                              ((200, ["not", "a", "dict"]), "invalid_response"),
                              ({**UPLOADED, "id_search": None}, "invalid_response")):
            with self.subTest(reason=reason):
                with self.assertRaises(BackendError) as caught:
                    asyncio.run(self.backend([reply]).reverse_image(IMAGE))
                self.assertEqual(caught.exception.reason, reason)

    def test_connection_errors_never_show_the_token(self):
        error = httpx.ConnectError(f"failed with Authorization: {KEY}")
        with self.assertRaises(BackendError) as caught:
            asyncio.run(self.backend([error]).reverse_image(IMAGE))
        self.assertEqual(caught.exception.reason, "connection_error")
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(BackendError) as caught:
            asyncio.run(self.backend([httpx.ReadTimeout("slow")]).reverse_image(IMAGE))
        self.assertEqual(caught.exception.reason, "timeout")

    def test_a_retry_resumes_the_same_search_instead_of_paying_again(self):
        backend = self.backend([UPLOADED, (503, {}), WAITING, DONE])

        async def search():
            return await with_retries(lambda: backend.reverse_image(IMAGE), deadline=math.inf, sleep=self.clock.sleep)

        payload, retries = asyncio.run(search())
        self.assertEqual(retries, 1)
        self.assertEqual(self.paths(), ["/api/upload_pic", "/api/search", "/api/search", "/api/search"])
        self.assertEqual({json.loads(request.content)["id_search"] for request in self.requests[1:]}, {"srch-1"})
        self.assertEqual(len(parse_image_results(payload).hits), 3)

    def test_search_times_out_without_a_second_upload(self):
        backend = self.backend([UPLOADED] + [WAITING] * 10, search_timeout=5.0)
        with self.assertRaises(BackendError) as caught:
            asyncio.run(backend.reverse_image(IMAGE))
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.paths().count("/api/upload_pic"), 1)
        self.assertEqual(self.clock.sleeps, [2.0, 2.0, 2.0])

    def test_empty_results_are_zero_hits(self):
        done = {**DONE, "output": {"items": [], "demo": False}}
        payload = asyncio.run(self.backend([UPLOADED, done]).reverse_image(IMAGE))
        self.assertEqual(parse_image_results(payload).hits, [])

    def test_release_does_nothing(self):
        backend = self.backend([])
        asyncio.run(backend.release(IMAGE))
        self.assertEqual(self.requests, [])


class PayloadTests(OfflineTestCase):
    def test_unusable_items_are_skipped(self):
        output = {"items": [
            {"score": 90, "url": {"value": "https://ok.example/a"}},
            {"score": True, "url": {"value": "https://bool.example"}},
            {"score": 80},
            {"score": 70, "url": {"value": None}},
            {"score": 150, "url": "https://plain.example/b"},
            "not an item",
        ]}
        payload = to_payload("s", output, demo=False)
        rows = [(row["link"], row["face_score"], row["position"]) for row in payload["image_results"]]
        self.assertEqual(rows, [("https://plain.example/b", 100, 1), ("https://ok.example/a", 90, 2)])

    def test_score_bands(self):
        self.assertEqual([score_band(score) for score in (100, 90, 89, 83, 82, 70, 69, 50, 49, 0)],
                         ["certain match", "certain match", "confident match", "confident match", "uncertain match",
                          "uncertain match", "weak match", "weak match", "unlikely match", "unlikely match"])


class AccountInfoTests(OfflineTestCase):
    def respond(self, status=200, body=None, error=None):
        def fake_post(url, *, timeout, headers):
            self.assertEqual(url, "https://facecheck.id/api/info")
            self.assertEqual(headers["Authorization"], KEY)
            if error:
                raise error
            request = httpx.Request("POST", url)
            if isinstance(body, str):
                return httpx.Response(status, text=body, request=request)
            return httpx.Response(status, json=body, request=request)
        return patch("pipeline.search.facecheck_backend.httpx.post", side_effect=fake_post)

    def test_keeps_only_info_fields(self):
        body = {"faces": 212417831, "is_online": True, "remaining_credits": 12, "has_credits_to_search": True,
                "id_search": None, "error": None}
        with self.respond(body=body):
            info = account_info(KEY)
        self.assertEqual(info, {"faces": 212417831, "is_online": True, "remaining_credits": 12,
                                "has_credits_to_search": True})
        self.assertEqual(describe_info(info),
                         "12 credits left (4 searches at 3 credits, $0.30 each), 212,417,831 faces indexed")

    def test_rejections(self):
        for status, body in ((200, TOKEN_ERROR), (200, {"error": "Missing API Token.", "code": "INVALID_TOKEN"}),
                             (401, {}), (403, {})):
            with self.subTest(status=status, body=body), self.respond(status, body):
                with self.assertRaises(TokenRejected) as caught:
                    account_info(KEY)
                self.assertNotIn(KEY, str(caught.exception))

    def test_failures_do_not_reject_the_token(self):
        for case in ({"error": httpx.ConnectError(f"boom {KEY}")}, {"status": 500, "body": {}}, {"body": "<html>"},
                     {"body": ["list"]}, {"body": {"error": "Engine busy", "code": "BUSY"}}):
            with self.subTest(case=case), self.respond(**case):
                with self.assertRaises(InfoCheckFailed) as caught:
                    account_info(KEY)
                self.assertNotIn(KEY, str(caught.exception))


class BuildTests(FaceCheckTestCase):
    def build(self, *, info=None, demo=False):
        env = {"FACECHECK_API_KEY": KEY, "FACECHECK_DEMO": "true" if demo else "false"}
        settings = load_settings(backend="facecheck", environ=env, dotenv_path=None)
        bundle = CaptureBundle.model_validate({
            "session_id": "fc-test", "captured_at": "2026-10-10T12:00:00Z",
            "consent": {"confirmed": True, "confirmed_at": "2026-10-10T11:59:00Z",
                        "external_processing": True},
            "images": [IMAGE.model_dump()],
        })
        capture = CaptureArtifact(run_id="fc-test-0000abcd", capture=bundle, capture_dir=str(self.folder),
                                  backend="facecheck", scenario="default", llm_mode="off", jev_mode="fake")
        with patch("pipeline.search.facecheck_backend.account_info",
                   side_effect=info if isinstance(info, BaseException) else None, return_value=info) as checked:
            backend = build_facecheck_backend(settings, capture, self.folder / "run", refresh=False)
        self.addCleanup(lambda: asyncio.run(backend.aclose()))
        return backend, checked

    def test_checks_credits_before_a_paid_search(self):
        backend, checked = self.build(info={"remaining_credits": 9, "has_credits_to_search": True})
        checked.assert_called_once_with(KEY)
        self.assertEqual((backend.demo, backend.cache_dir), (False, self.folder / "run" / "cache" / "facecheck"))
        self.assertEqual(backend.extra_deadline_s, 180.0)

    def test_no_credits_stops_before_any_upload(self):
        with self.assertRaisesRegex(ConfigError, "no search credits.*FACECHECK_DEMO=true"):
            self.build(info={"remaining_credits": 0, "has_credits_to_search": False})

    def test_rejected_token_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "rejected FACECHECK_API_KEY.*set-key facecheck") as caught:
            self.build(info=TokenRejected("INVALID_TOKEN"))
        self.assertNotIn(KEY, str(caught.exception))

    def test_unreachable_info_still_builds(self):
        backend, checked = self.build(info=InfoCheckFailed("ConnectError"))
        checked.assert_called_once()

    def test_demo_and_cached_runs_skip_the_check(self):
        _, checked = self.build(demo=True)
        checked.assert_not_called()
        cache = self.folder / "run" / "cache" / "facecheck"
        cache.mkdir(parents=True)
        digest = hashlib.sha256((self.folder / "img-1.jpg").read_bytes()).hexdigest()
        (cache / f"{digest}-facecheck.json").write_text("{}")
        _, checked = self.build()
        checked.assert_not_called()

    def test_missing_key_is_a_config_error(self):
        settings = load_settings(backend="mock", environ={}, dotenv_path=None)
        with self.assertRaisesRegex(ConfigError, "FACECHECK_API_KEY"):
            build_facecheck_backend(settings, None, self.folder, refresh=False)
