"""Backend V3 (Trace and SerpApi search + Rekognition face matching), offline (spec 1.1, 1.7).

httpx.MockTransport stands in for SerpApi and the hosts of result images, FakeHost for
image hosting, FakeTrace for the Trace client (tests/test_trace_face.py covers its HTTP),
and FakeRekognition for boto3's Rekognition client. Result images are
blank JPEGs told apart by their width, which survives the backend's re-encoding. The
socket guard from OfflineTestCase stays on throughout.
"""

import asyncio
import io
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError
from PIL import Image

from pipeline.config import ConfigError, load_settings
from pipeline.contracts import CaptureImage, FetchedPage, UploadRecord, validate_references
from pipeline.image_agent import system_prompt
from pipeline.search.base import BackendError, parse_image_results
from pipeline.search.rekognition_backend import (
    TRACE_EXTRA_DEADLINE_S,
    RekognitionBackend, aws_error, best_similarity, build_rekognition_backend, face_crop, merge, require_rekognition,
    sanitize_engine, score_band,
)
from pipeline.stage1_collect import collect_evidence, format_hits
from tests.support import OfflineTestCase
from tests.test_stage1 import live_capture, page_html

KEY = "serpapi-test-key-not-real"
HOSTED = "https://tunnel123.lhr.life/0123456789abcdef.jpg"
IMAGE = CaptureImage(image_id="img-1", file="img-1.jpg", angle="front")
FACE = {"BoundingBox": {"Left": 0.4, "Top": 0.3, "Width": 0.2, "Height": 0.25}, "Confidence": 99.9}
LINKEDIN, GITHUB, LOOKALIKE = "https://linkedin.com/in/patlee", "https://github.com/patlee", "https://lookalike.example/a"
LANDSCAPE, BROKEN = "https://example.com/landscape", "https://broken.example/p"

YANDEX = {
    "search_metadata": {"id": "y-1", "status": "Success", "json_endpoint": f"https://serpapi.com/x?api_key={KEY}"},
    "search_parameters": {"engine": "yandex_images", "url": HOSTED, "api_key": KEY},
    "image_results": [
        {"title": "Pat Lee | LinkedIn", "link": "https://www.linkedin.com/in/patlee/", "source": "linkedin.com",
         "snippet": "Engineer at Atlas", "thumbnail": {"link": "https://avatars.example/t1.jpg"},
         "original_image": {"link": "https://img.example/linkedin.jpg"}},
        {"title": "Someone else", "link": LOOKALIKE, "source": "lookalike.example",
         "original_image": {"link": "https://img.example/lookalike.jpg"}},
        {"title": "A field", "link": LANDSCAPE, "source": "example.com",
         "original_image": {"link": "https://img.example/landscape.jpg"}},
    ],
    "similar_images": [{"link": "https://similar.example"}],
}
LENS = {
    "search_metadata": {"id": "l-1", "status": "Success"},
    "search_parameters": {"engine": "google_lens", "url": HOSTED},
    "visual_matches": [
        {"position": 1, "title": "patlee (Pat Lee) - GitHub", "link": GITHUB, "source": "GitHub",
         "thumbnail": "https://tbn.example/t3.jpg", "image": "https://img.example/github.png"},
        {"position": 2, "title": "Pat Lee - Atlas | LinkedIn", "link": "https://linkedin.com/in/patlee",
         "source": "LinkedIn", "thumbnail": "https://tbn.example/t5.jpg", "image": "https://img.example/linkedin-2.jpg"},
        {"position": 3, "title": "Pat at the meetup", "link": BROKEN, "source": "broken.example",
         "thumbnail": "https://tbn.example/t4.jpg", "image": "https://img.example/missing.jpg"},
    ],
}
# Result image path -> width; width -> CompareFaces reply (or exception).
WIDTHS = {"/linkedin.jpg": 101, "/lookalike.jpg": 102, "/landscape.jpg": 103, "/github.png": 104, "/t4.jpg": 105,
          "/linkedin-2.jpg": 106, "/t1.jpg": 107, "/t3.jpg": 108, "/t5.jpg": 109}
REPLIES = {
    101: {"FaceMatches": [{"Similarity": 99.62}], "UnmatchedFaces": []},
    102: {"FaceMatches": [{"Similarity": 61.0}], "UnmatchedFaces": []},
    103: {"FaceMatches": [], "UnmatchedFaces": []},
    104: {"FaceMatches": [{"Similarity": 12.5}, {"Similarity": 93.2}], "UnmatchedFaces": [{}]},
    105: {"FaceMatches": [{"Similarity": 85.9}], "UnmatchedFaces": []},
}


def client_error(code, operation="CompareFaces", status=400):
    return ClientError({"Error": {"Code": code, "Message": "test"}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       operation)


def jpeg(width, height=None, fmt="JPEG"):
    out = io.BytesIO()
    Image.new("RGB", (width, height or width), (200, 180, 160)).save(out, fmt)
    return out.getvalue()


def width_of(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size[0]


class FakeRekognition:
    def __init__(self, faces=(FACE,), replies=None, delay=0.0):
        self.faces, self.replies, self.delay = faces, dict(REPLIES if replies is None else replies), delay
        self.detect_calls, self.compare_calls = [], []

    def detect_faces(self, **params):
        self.detect_calls.append(params)
        if isinstance(self.faces, Exception):
            raise self.faces
        return {"FaceDetails": list(self.faces)}

    def compare_faces(self, **params):
        self.compare_calls.append(params)
        time.sleep(self.delay)
        reply = self.replies.get(width_of(params["TargetImage"]["Bytes"]), client_error("InvalidParameterException"))
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeHost:
    def __init__(self):
        self.uploads, self.deleted = [], []

    def upload(self, data):
        self.uploads.append(data)
        return HOSTED, UploadRecord(bucket="localhost-run-tunnel", key=f"k{len(self.uploads)}.jpg")

    def delete(self, record):
        self.deleted.append(record.key)


class V3TestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.folder / "img-1.jpg").write_bytes(jpeg(1000, 800))
        self.serp, self.downloads = [], []

    def backend(self, serp=None, *, rekognition=None, images=None, pages=None, refresh=False, hosted=True,
                **options):
        """serp: engine -> list of (status, body) replies, used in order. hosted=False builds it
        without a SerpApi key or image host, as for REKOGNITION_ENGINES=trace."""
        serp = {engine: list(replies) for engine, replies in (serp or {"yandex_images": [(200, YANDEX)],
                                                                       "google_lens": [(200, LENS)]}).items()}
        images = images if images is not None else {}

        def search(request):
            params = {name: values[0] for name, values in parse_qs(urlsplit(str(request.url)).query).items()}
            self.serp.append(params)
            status, body = serp[params["engine"]].pop(0)
            return httpx.Response(status, json=body)

        def image(request):
            self.downloads.append(str(request.url))
            path = request.url.path
            if path in images:
                return images[path]
            if path in WIDTHS:
                return httpx.Response(200, content=jpeg(WIDTHS[path]), headers={"content-type": "image/jpeg"})
            return httpx.Response(404, text="gone")

        self.host = FakeHost()
        self.rekognition = rekognition or FakeRekognition()
        backend = RekognitionBackend(
            api_key=KEY if hosted else None, capture_dir=self.folder, host=self.host if hosted else None,
            rekognition=self.rekognition,
            cache_dir=self.folder / "cache", refresh=refresh,
            search_client=httpx.AsyncClient(transport=httpx.MockTransport(search)),
            images=httpx.AsyncClient(transport=httpx.MockTransport(image)),
            pages=pages or httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
            **options)
        self.addCleanup(lambda: asyncio.run(backend.aclose()))
        return backend

    def search(self, backend):
        return asyncio.run(backend.reverse_image(IMAGE))

    def cached(self):
        folder = self.folder / "cache"
        return sorted(path.name.split("-", 1)[1] for path in folder.iterdir()) if folder.exists() else []


class SearchTests(V3TestCase):
    def test_search_compare_and_convert(self):
        backend = self.backend()
        payload = self.search(backend)

        # DetectFaces saw an upright JPEG copy; only the padded face crop was hosted and compared.
        [detect] = self.rekognition.detect_calls
        self.assertEqual((width_of(detect["Image"]["Bytes"]), detect["Attributes"]), (1000, ["DEFAULT"]))
        [crop] = self.host.uploads
        with Image.open(io.BytesIO(crop)) as opened:
            self.assertEqual((opened.format, opened.size), ("JPEG", (400, 400)))
        self.assertTrue(all(call["SourceImage"]["Bytes"] == crop and call["SimilarityThreshold"] == 0.0
                            for call in self.rekognition.compare_calls))

        # One SerpApi search per engine, both on the hosted crop.
        self.assertEqual(sorted(self.serp, key=lambda params: params["engine"]), [
            {"engine": "google_lens", "url": HOSTED, "api_key": KEY},
            {"engine": "yandex_images", "url": HOSTED, "tab": "about", "api_key": KEY}])

        result = parse_image_results(payload)
        self.assertEqual([(hit.position, hit.url, hit.face_score, hit.title) for hit in result.hits], [
            (1, LINKEDIN, 99, "Pat Lee | LinkedIn"),
            (2, GITHUB, 93, "patlee (Pat Lee) - GitHub"),
            (3, BROKEN, 85, "Pat at the meetup")])
        self.assertEqual(result.hits[0].snippet, "Engineer at Atlas")
        self.assertEqual(result.request_id, "y-1,l-1")
        self.assertEqual(result.extras, {
            "status": "Success", "total_results": 3, "engines": ["yandex_images", "google_lens"],
            "failed_engines": [], "min_similarity": 80.0, "candidates": 5,
            "outcomes": {"matched": 3, "below_threshold": 1, "no_face": 1}})
        # The broken page's full-size image 404'd, so its thumbnail was compared instead.
        self.assertIn("https://img.example/missing.jpg", self.downloads)
        self.assertIn("https://tbn.example/t4.jpg", self.downloads)
        self.assertNotIn("https://img.example/linkedin-2.jpg", self.downloads)  # the first image was enough

        for text in [json.dumps(payload)] + [path.read_text() for path in (self.folder / "cache").iterdir()]:
            self.assertNotIn(KEY, text)
            self.assertNotIn("lhr.life", text)
        self.assertEqual(self.cached(), ["google_lens.json", "v3-yandex_images+google_lens-80.json",
                                         "yandex_images.json"])
        asyncio.run(backend.release(IMAGE))
        self.assertEqual(self.host.deleted, ["k1.jpg"])

    def test_capture_without_a_face_fails_before_searching(self):
        backend = self.backend(rekognition=FakeRekognition(faces=()))
        with self.assertRaises(BackendError) as caught:
            self.search(backend)
        self.assertEqual(caught.exception.reason, "no_face")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual((self.serp, self.host.uploads, self.cached()), ([], [], []))

    def test_largest_face_is_the_participant(self):
        small = {"BoundingBox": {"Left": 0.05, "Top": 0.05, "Width": 0.05, "Height": 0.05}}
        backend = self.backend(rekognition=FakeRekognition(faces=(small, FACE)))
        self.search(backend)
        self.assertEqual(Image.open(io.BytesIO(self.host.uploads[0])).size, (400, 400))

    def test_one_engine_failing_keeps_the_other(self):
        backend = self.backend({"yandex_images": [(200, YANDEX)], "google_lens": [(503, {"error": "busy"})]})
        result = parse_image_results(self.search(backend))
        self.assertEqual([hit.url for hit in result.hits], [LINKEDIN])
        self.assertEqual((result.extras["engines"], result.extras["failed_engines"]), (["yandex_images"], ["google_lens"]))
        self.assertEqual(self.cached(), ["yandex_images.json"])  # an incomplete result isn't cached

        # A later run searches only the engine that failed.
        self.serp.clear()
        rerun = self.backend({"google_lens": [(200, LENS)]})
        self.assertEqual(len(parse_image_results(self.search(rerun)).hits), 3)
        self.assertEqual([params["engine"] for params in self.serp], ["google_lens"])
        self.assertIn("v3-yandex_images+google_lens-80.json", self.cached())

    def test_all_engines_failing_is_retryable_and_resumes(self):
        backend = self.backend({"yandex_images": [(503, {}), (200, YANDEX)], "google_lens": [(429, {}), (200, LENS)]})
        with self.assertRaises(BackendError) as caught:
            self.search(backend)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(parse_image_results(self.search(backend)).hits), 3)
        self.assertEqual((len(self.rekognition.detect_calls), len(self.host.uploads)), (1, 1))  # face found once

    def test_no_results_is_an_answer_and_errors_fail_the_engine(self):
        empty = {"search_metadata": {"id": "y-2", "status": "Success"},
                 "error": "Yandex Images hasn't returned any results for this query."}
        failed = {"search_metadata": {"id": "l-2", "status": "Error"}, "error": f"Invalid API key {KEY}"}
        backend = self.backend({"yandex_images": [(200, empty)], "google_lens": [(200, failed)]})
        result = parse_image_results(self.search(backend))
        self.assertEqual((result.hits, result.extras["engines"], result.extras["failed_engines"]),
                         ([], ["yandex_images"], ["google_lens"]))
        self.assertEqual(self.cached(), ["yandex_images.json"])  # zero results is an answer

        alone = self.backend({"google_lens": [(200, failed)]}, engines=("google_lens",), refresh=True)
        with self.assertRaises(BackendError) as caught:
            self.search(alone)
        self.assertEqual(caught.exception.reason, "provider_error")
        self.assertNotIn(KEY, str(caught.exception))

    def test_cache_reuse_threshold_change_and_refresh(self):
        self.search(self.backend())
        self.serp.clear()

        again = self.backend()
        self.search(again)
        self.assertEqual((self.serp, self.rekognition.detect_calls), ([], []))
        self.assertEqual(again.cache_hits, {"img-1"})

        # A new threshold compares again without searching again.
        looser = self.backend(min_similarity=60)
        hits = parse_image_results(self.search(looser)).hits
        self.assertEqual(self.serp, [])
        self.assertEqual([hit.url for hit in hits], [LINKEDIN, GITHUB, BROKEN, LOOKALIKE])
        self.assertIn("lower is weak", looser.face_score_note)
        self.assertIn("under 60 were already dropped", looser.face_score_note)

        self.search(self.backend(refresh=True))
        self.assertEqual(len(self.serp), 2)

    def test_rekognition_refusal_stops_the_search(self):
        replies = {**REPLIES, 104: client_error("AccessDeniedException", status=403)}
        backend = self.backend(rekognition=FakeRekognition(replies=replies))
        with self.assertRaises(BackendError) as caught:
            self.search(backend)
        self.assertEqual((caught.exception.reason, caught.exception.status_code), ("unauthorized", 403))
        self.assertNotIn("v3-yandex_images+google_lens-80.json", self.cached())

    def test_detect_faces_errors_are_codes(self):
        for error, reason in ((NoCredentialsError(), "unauthorized"), (client_error("ThrottlingException"), "rate_limited")):
            with self.subTest(reason=reason):
                with self.assertRaises(BackendError) as caught:
                    self.search(self.backend(rekognition=FakeRekognition(faces=error)))
                self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.serp, [])

    def test_compare_timeout_counts_not_compared(self):
        backend = self.backend(rekognition=FakeRekognition(delay=0.3), compare_timeout=0.05)
        result = parse_image_results(self.search(backend))
        self.assertEqual(result.hits, [])
        self.assertEqual(result.extras["outcomes"], {"not_compared": 5})
        self.assertNotIn("v3-yandex_images+google_lens-80.json", self.cached())

    def test_result_images_are_guarded(self):
        lens = {"search_metadata": {"status": "Success"}, "visual_matches": [
            {"link": "https://a.example/p", "image": "http://127.0.0.1/a.jpg", "thumbnail": "http://10.0.0.8/a.jpg"},
            {"link": "https://b.example/p", "image": "https://img.example/page.html",
             "thumbnail": "https://img.example/github.png"},
            {"link": "https://c.example/p", "image": "https://img.example/huge.jpg"},
            {"link": "https://d.example/p"},
        ]}
        images = {"/page.html": httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}),
                  "/huge.jpg": httpx.Response(200, content=b"\xff" * (9 * 1024 * 1024),
                                              headers={"content-type": "image/jpeg"})}
        backend = self.backend({"google_lens": [(200, lens)]}, engines=("google_lens",), images=images)
        result = parse_image_results(self.search(backend))
        self.assertEqual([(hit.url, hit.face_score) for hit in result.hits], [("https://b.example/p", 93)])
        self.assertEqual(result.extras["outcomes"], {"image_unavailable": 2, "matched": 1, "no_image": 1})
        self.assertFalse([url for url in self.downloads if "127.0.0.1" in url or "10.0.0.8" in url])

    def test_an_image_without_a_face_moves_on_to_the_next(self):
        lens = {"search_metadata": {"status": "Success"}, "visual_matches": [
            # CompareFaces finds no face in the full-size image; the thumbnail matches.
            {"link": "https://a.example/p", "image": "https://img.example/landscape.jpg",
             "thumbnail": "https://tbn.example/t4.jpg"},
            # No face in either: CompareFaces refuses the first and finds none in the second.
            {"link": "https://b.example/p", "image": "https://tbn.example/t1.jpg",
             "thumbnail": "https://img.example/landscape.jpg"},
            # No face, then a thumbnail that won't download: still no_face, not image_unavailable.
            {"link": "https://c.example/p", "image": "https://img.example/landscape.jpg",
             "thumbnail": "https://img.example/missing.jpg"},
        ]}
        backend = self.backend({"google_lens": [(200, lens)]}, engines=("google_lens",))
        result = parse_image_results(self.search(backend))
        self.assertEqual([(hit.url, hit.face_score) for hit in result.hits], [("https://a.example/p", 85)])
        self.assertEqual(result.extras["outcomes"], {"matched": 1, "no_face": 2})
        self.assertEqual(len(self.rekognition.compare_calls), 5)


TRACE_MATCHES = [
    {"score": 71, "tier": "possible", "platform": "instagram", "handle": "someone_else",
     "url": "https://www.instagram.com/someone_else/", "preview_url": "https://traceaifacescan.app/api/v1/scans/scan-1/previews/1"},
    {"score": 96, "tier": "near_certain", "platform": "linkedin", "handle": "patlee",
     "url": "https://www.linkedin.com/in/patlee/", "preview_url": "https://traceaifacescan.app/api/v1/scans/scan-1/previews/0"},
    {"score": 74, "tier": "possible", "platform": "web", "handle": None,
     "url": "https://blog.example/pat-at-the-meetup", "preview_url": "https://evil.example/steal-the-key"},
    {"score": 70, "tier": "possible", "platform": "x", "handle": "nopreview", "url": "https://x.com/nopreview",
     "preview_url": "https://traceaifacescan.app/api/v1/scans/scan-1/previews/9"},
]
# Preview index -> width, picking a CompareFaces reply from REPLIES: LinkedIn 99.62, Instagram 61.0, and
# the blog 85.9 (its preview URL isn't Trace's, so the backend uses Trace's path for its index, 2).
PREVIEW_WIDTHS = {0: 101, 1: 102, 2: 105}


class FakeTrace:
    def __init__(self, matches=TRACE_MATCHES, error=None):
        self.matches, self.error = matches, error
        self.uploads, self.keys, self.previews, self.deleted, self.closed = [], [], [], [], False

    async def search(self, image_id, image, *, key=None):
        self.uploads.append(image)
        self.keys.append(key)
        if self.error:
            raise self.error
        return "scan-1", {"id": "scan-1", "status": "done", "locked": False, "matches": self.matches}

    async def preview(self, url):
        self.previews.append(url)
        width = PREVIEW_WIDTHS.get(int(url.rsplit("/", 1)[1]))
        if width is None:
            raise BackendError("not_found", status_code=404)
        return jpeg(width)

    async def delete(self, image_id):
        self.deleted.append(image_id)

    async def aclose(self):
        self.closed = True


class TraceEngineTests(V3TestCase):
    def test_trace_only_needs_no_serpapi_or_hosting(self):
        fake = FakeTrace()
        backend = self.backend({}, engines=("trace",), trace_search=fake, hosted=False)
        self.assertEqual(backend.extra_deadline_s, TRACE_EXTRA_DEADLINE_S)
        result = parse_image_results(self.search(backend))

        self.assertEqual((self.serp, self.host.uploads), ([], []))
        [upload] = fake.uploads
        self.assertEqual(Image.open(io.BytesIO(upload)).size, (400, 400))  # the face crop
        self.assertEqual([(hit.url, hit.face_score, hit.title, hit.snippet) for hit in result.hits], [
            (LINKEDIN, 99, "patlee on linkedin", "Trace face match 96/100 (near certain)"),
            ("https://blog.example/pat-at-the-meetup", 85, "web", "Trace face match 74/100 (possible)")])
        self.assertEqual(result.extras["outcomes"], {"matched": 2, "below_threshold": 1, "image_unavailable": 1})
        self.assertEqual(result.request_id, "scan-1")
        # A preview URL off Trace's API is replaced by Trace's own path, so the key never leaves Trace.
        self.assertTrue(all(url.startswith("https://traceaifacescan.app/api/v1/scans/scan-1/previews/")
                            for url in fake.previews))
        self.assertEqual(self.cached(), ["v3-trace-80.json"])  # Trace replies themselves aren't cached
        asyncio.run(backend.release(IMAGE))
        self.assertEqual(fake.deleted, ["img-1"])

        # The Idempotency-Key comes from the photo, so another run of the same photo gets the same scan.
        again = FakeTrace()
        self.search(self.backend({}, engines=("trace",), trace_search=again, hosted=False, refresh=True))
        self.assertEqual(len(fake.keys[0]), 32)
        self.assertEqual(again.keys, fake.keys)

    def test_every_trace_match_is_compared_and_the_cap_limits_the_rest(self):
        fake = FakeTrace()
        backend = self.backend(engines=("trace", "yandex_images", "google_lens"), trace_search=fake)
        with patch("pipeline.search.rekognition_backend.MAX_CANDIDATES", 2):
            result = parse_image_results(self.search(backend))
        # All 4 Trace matches, then the first 2 SerpApi-only pages (GitHub, the lookalike). LinkedIn,
        # also found by Yandex and Lens, is compared through Trace's preview, not a download.
        self.assertEqual([hit.url for hit in result.hits],
                         [LINKEDIN, GITHUB, "https://blog.example/pat-at-the-meetup"])
        self.assertEqual(result.extras["candidates"], 8)
        self.assertEqual(result.extras["outcomes"]["over_limit"], 2)
        self.assertEqual(len(fake.previews), 4)
        self.assertNotIn("https://img.example/linkedin.jpg", self.downloads)
        self.assertEqual(len(self.serp), 2)

    def test_platform_limit_drops_other_sites_before_comparing(self):
        # V3_1: of the 8 merged pages only the LinkedIn profile is kept, compared through Trace's preview.
        fake = FakeTrace()
        backend = self.backend(engines=("trace", "yandex_images", "google_lens"), trace_search=fake,
                               platforms=("linkedin", "facebook"))
        result = parse_image_results(self.search(backend))
        self.assertEqual([(hit.url, hit.face_score) for hit in result.hits], [(LINKEDIN, 99)])
        self.assertEqual((result.extras["candidates"], result.extras["outcomes"]),
                         (8, {"out_of_scope": 7, "matched": 1}))
        self.assertEqual(result.extras["platforms"], ["linkedin", "facebook"])
        self.assertEqual((len(self.rekognition.compare_calls), self.downloads), (1, []))
        self.assertIn("v3-trace+yandex_images+google_lens-80-linkedin+facebook.json", self.cached())

    def test_the_cap_counts_only_kept_platforms(self):
        backend = self.backend(platforms=("github",))
        with patch("pipeline.search.rekognition_backend.MAX_CANDIDATES", 1):
            result = parse_image_results(self.search(backend))
        # Without the limit the cap of 1 would compare LinkedIn, the first merged page.
        self.assertEqual([hit.url for hit in result.hits], [GITHUB])
        self.assertEqual(result.extras["outcomes"], {"out_of_scope": 4, "matched": 1})

    def test_small_face_sends_the_whole_photo_to_trace(self):
        small = {"BoundingBox": {"Left": 0.45, "Top": 0.45, "Width": 0.05, "Height": 0.05}}
        fake = FakeTrace(matches=[])
        self.search(self.backend({}, engines=("trace",), trace_search=fake, hosted=False,
                                 rekognition=FakeRekognition(faces=(small,))))
        self.assertEqual(Image.open(io.BytesIO(fake.uploads[0])).size, (1000, 800))

    def test_trace_failure_keeps_the_serpapi_results(self):
        fake = FakeTrace(error=BackendError("no_credits"))
        backend = self.backend(engines=("trace", "yandex_images", "google_lens"), trace_search=fake)
        result = parse_image_results(self.search(backend))
        self.assertEqual([hit.url for hit in result.hits], [LINKEDIN, GITHUB, BROKEN])
        self.assertEqual((result.extras["engines"], result.extras["failed_engines"]),
                         (["yandex_images", "google_lens"], ["trace"]))
        self.assertNotIn("v3-trace+yandex_images+google_lens-80.json", self.cached())

    def test_engines_need_their_clients(self):
        with self.assertRaisesRegex(ValueError, "TraceFaceSearch"):
            self.backend(engines=("trace",))
        with self.assertRaisesRegex(ValueError, "SerpApi key"):
            self.backend(engines=("google_lens",), hosted=False)


class HelperTests(OfflineTestCase):
    def test_face_crop_pads_and_clips(self):
        source = jpeg(1000, 800)
        edge = {"Left": -0.05, "Top": 0.9, "Width": 0.2, "Height": 0.2}
        with Image.open(io.BytesIO(face_crop(source, edge))) as crop:
            self.assertEqual(crop.size, (250, 160))  # x from 0 to 250, y from 640 to 800
        with self.assertRaises(BackendError):
            face_crop(source, {"Left": 2.0, "Top": 0.1, "Width": 0.1, "Height": 0.1})

    def test_best_similarity(self):
        self.assertEqual(best_similarity({"FaceMatches": [{"Similarity": 40}, {"Similarity": 97.5}]}), 97.5)
        self.assertEqual(best_similarity({"FaceMatches": [], "UnmatchedFaces": [{}]}), 0.0)
        self.assertIsNone(best_similarity({"FaceMatches": [], "UnmatchedFaces": []}))
        self.assertIsNone(best_similarity({"FaceMatches": [{"Similarity": True}]}))

    def test_aws_errors_map_to_codes(self):
        cases = [(client_error("InvalidParameterException"), "no_face"),
                 (client_error("InvalidImageFormatException"), "unsupported_image"),
                 (client_error("AccessDeniedException"), "unauthorized"),
                 (client_error("ProvisionedThroughputExceededException"), "rate_limited"),
                 (client_error("InternalServerError", status=500), "server_error"),
                 (client_error("SomethingNew"), "provider_error"),
                 (NoCredentialsError(), "unauthorized"),
                 (ReadTimeoutError(endpoint_url="https://rekognition.example"), "timeout"),
                 (EndpointConnectionError(endpoint_url="https://rekognition.example"), "connection_error")]
        for exc, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(aws_error(exc).reason, reason)
        self.assertIsNone(aws_error(ValueError("a bug")))

    def test_sanitize_engine_keeps_only_what_v3_uses(self):
        clean = sanitize_engine("yandex_images", YANDEX)
        dumped = json.dumps(clean)
        self.assertNotIn(KEY, dumped)
        self.assertNotIn(HOSTED, dumped)
        self.assertNotIn("similar_images", dumped)
        self.assertEqual(clean["results"][0], {
            "title": "Pat Lee | LinkedIn", "source": "linkedin.com", "snippet": "Engineer at Atlas",
            "link": "https://www.linkedin.com/in/patlee/", "thumbnail": "https://avatars.example/t1.jpg",
            "images": ["https://img.example/linkedin.jpg", "https://avatars.example/t1.jpg"]})
        signed = {"visual_matches": [{"link": "https://a.example", "image": "https://s3.example/a.jpg?X-Amz-Signature=1"}]}
        self.assertEqual(sanitize_engine("google_lens", signed)["results"][0]["images"], [])

    def test_merge_alternates_engines_and_puts_full_images_first(self):
        results = {"yandex_images": sanitize_engine("yandex_images", YANDEX),
                   "google_lens": sanitize_engine("google_lens", LENS)}
        candidates = merge(results)
        self.assertEqual([candidate.url for candidate in candidates], [LINKEDIN, GITHUB, LOOKALIKE, LANDSCAPE, BROKEN])
        self.assertEqual(candidates[0].engines, ["yandex_images", "google_lens"])
        self.assertEqual(candidates[0].images, ["https://img.example/linkedin.jpg", "https://img.example/linkedin-2.jpg",
                                                "https://avatars.example/t1.jpg", "https://tbn.example/t5.jpg"])

    def test_score_bands(self):
        self.assertEqual([score_band(score) for score in (100, 99, 96, 90, 85, 40)],
                         ["strong match", "strong match", "likely match", "possible match", "weak match",
                          "unlikely match"])


class BuildTests(OfflineTestCase):
    def settings(self, **env):
        return load_settings(backend="rekognition", dotenv_path=None,
                             environ={"SERPAPI_KEY": KEY, "TRACE_API_KEY": "trk_live_test", "AWS_REGION": "us-east-1",
                                      **env})

    def test_missing_credentials_stop_before_the_tunnel(self):
        capture = live_capture("img-1")
        with patch("pipeline.search.rekognition_backend.open_host") as open_host:
            with self.assertRaisesRegex(ConfigError, "set-key aws"):
                build_rekognition_backend(self.settings(), capture, Path("/tmp/run"), refresh=False)
        open_host.assert_not_called()

    def test_trace_only_build_opens_no_tunnel(self):
        os.environ.update(AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE", AWS_SECRET_ACCESS_KEY="x" * 40)
        settings = load_settings(backend="rekognition", dotenv_path=None, environ={
            "TRACE_API_KEY": "trk_live_test", "AWS_REGION": "us-east-2", "REKOGNITION_ENGINES": "trace"})
        with patch("pipeline.search.rekognition_backend.open_host") as open_host, \
                patch("pipeline.search.rekognition_backend.rekognition_client"):
            backend = build_rekognition_backend(settings, live_capture("img-1"), Path("/tmp/run"), refresh=False)
        self.addCleanup(lambda: asyncio.run(backend.aclose()))
        open_host.assert_not_called()
        self.assertIsNone(backend.host)
        self.assertEqual((backend.engines, backend.trace_search.api_key), (("trace",), "trk_live_test"))

    def test_build_uses_the_settings(self):
        os.environ.update(AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE", AWS_SECRET_ACCESS_KEY="x" * 40)
        require_rekognition(self.settings())
        settings = self.settings(REKOGNITION_ENGINES="google_lens", REKOGNITION_MIN_SIMILARITY="90",
                                 REKOGNITION_PLATFORMS="linkedin,facebook")
        with patch("pipeline.search.rekognition_backend.open_host", return_value=FakeHost()), \
                patch("pipeline.search.rekognition_backend.rekognition_client") as client:
            backend = build_rekognition_backend(settings, live_capture("img-1"), Path("/tmp/run"), refresh=True)
        self.addCleanup(lambda: asyncio.run(backend.aclose()))
        client.assert_called_once_with("us-east-1")
        self.assertEqual((backend.engines, backend.min_similarity, backend.refresh), (("google_lens",), 90.0, True))
        self.assertEqual(backend.platforms, ("linkedin", "facebook"))
        self.assertEqual(backend.cache_dir, Path("/tmp/run/cache/rekognition"))
        self.assertIn("face_score", system_prompt(backend))
        self.assertIn("under 90 were already dropped", system_prompt(backend))


class CollectTests(V3TestCase):
    def test_stage_1_fetches_matches_by_face_score(self):
        pages = {LINKEDIN: page_html(title="Pat Lee", person=True), GITHUB: page_html(title="patlee")}

        def page(request):
            html = pages.get(str(request.url).rstrip("/"))
            if html is None:
                return httpx.Response(404)
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        backend = self.backend(pages=httpx.AsyncClient(transport=httpx.MockTransport(page)))
        capture = live_capture("img-1").model_copy(update={"backend": "rekognition", "capture_dir": str(self.folder)})
        bundle, _ = asyncio.run(collect_evidence(capture, backend))
        validate_references(capture=capture, raw=bundle)
        [image] = bundle.images
        self.assertEqual((image.backend, [hit.face_score for hit in image.hits]), ("rekognition", [99, 93, 85]))
        self.assertEqual(image.search_extras["outcomes"]["matched"], 3)
        self.assertEqual({url: page.extraction for url, page in bundle.pages.items()},
                         {LINKEDIN: "html", GITHUB: "html", BROKEN: "snippet"})
        self.assertIn(f"1. face  99/100 Pat Lee | LinkedIn  {LINKEDIN}", format_hits(bundle))
        self.assertEqual(self.host.deleted, ["k1.jpg"])  # the crop stops being served after the search
