"""Section 1 deterministic collection, merging, and the stage entry point (spec 1.3-1.8, matrix in 9).

Mock tests run the real collector against a local mock internet. FakeBackend tests
use canned live-shaped responses for merge rules, budgets, and deadlines.
"""

import asyncio
import json
import tempfile
import time
from unittest.mock import patch
from datetime import datetime, timezone

from pipeline.artifacts import create_run, read_artifact, write_artifact
from pipeline.capture import load_capture
from pipeline.config import ConfigError, load_settings
from pipeline.contracts import CaptureArtifact, CaptureBundle, FetchedPage, validate_references
from pipeline.search.base import BackendError
from pipeline.search.mock_backend import MockSearchBackend
from pipeline.stage1_collect import collect_evidence, format_hits, run_stage1
from tests.support import FIXTURE_CAPTURES, MockServerTestCase, OfflineTestCase

OLD_PROJECT = "https://fieldnotes.test/@alexbuilds/old-project"
PRIVATE_ALBUM = "https://photogram.test/samdraws/private-album"
INTERVIEW = "https://riverbend-news.test/people/alex-morgan-transit"
MIRROR = "https://syndicated.test/transit/alex-morgan"
ALEX_PROFILES = {"https://linkup.test/in/alexbuilds", "https://chatter.test/alexbuilds",
                 "https://photogram.test/alexbuilds", "https://codeforge.test/alexbuilds",
                 "https://fieldnotes.test/@alexbuilds"}


def mock_capture(name="alex", scenario="default"):
    loaded = load_capture(FIXTURE_CAPTURES / name)
    return CaptureArtifact(run_id=f"{loaded.bundle.session_id}-0000abcd", capture=loaded.bundle,
                           capture_dir=str(loaded.capture_dir), backend="mock", scenario=scenario,
                           llm_mode="off", jev_mode="fake")


class TimedBackend(MockSearchBackend):
    """Records when each reverse search starts and ends."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_spans = []

    async def reverse_image(self, image):
        start = time.monotonic()
        try:
            return await super().reverse_image(image)
        finally:
            self.search_spans.append((start, time.monotonic()))


class MockCollectionTests(MockServerTestCase):
    def collect(self, name="alex", scenario="default", **backend_options):
        backend = TimedBackend(self.mock_url, scenario=scenario, **backend_options)
        capture = mock_capture(name, scenario)
        bundle, store = asyncio.run(collect_evidence(capture, backend))
        validate_references(capture=capture, raw=bundle)
        self.backend = backend
        return bundle, store

    def test_alex_default_matches_spec_1_8(self):
        bundle, store = self.collect()
        hits = [hit for image in bundle.images for hit in image.hits]
        self.assertEqual([len(image.hits) for image in bundle.images], [4, 4, 5])
        self.assertEqual(len({hit.url for hit in hits}), 10)
        # Exactly 10 fetches: the shared page store prevents duplicates.
        self.assertEqual(self.mock_count("/fetch"), 10)
        self.assertEqual(store.fetch_count, 10)
        # 9 HTML responses and one 404; the mirror merges, leaving 8 HTML + 1 snippet records.
        html = [page for page in bundle.pages.values() if page.extraction == "html"]
        self.assertEqual((len(bundle.pages), len(html)), (9, 8))
        snippet = bundle.pages[OLD_PROJECT]
        self.assertEqual((snippet.extraction, snippet.status_code, snippet.outbound_links), ("snippet", 404, []))
        errors = [(error.url, error.status_code, error.reason) for image in bundle.images for error in image.errors]
        self.assertEqual(errors, [(OLD_PROJECT, 404, "not_found")])
        # 5 profiles, each with provenance; Chatter was found by img-1 and img-2.
        profiles = {url: page for url, page in bundle.pages.items() if page.page_kind == "profile"}
        self.assertEqual(set(profiles), ALEX_PROFILES)
        self.assertTrue(all(page.found_via for page in profiles.values()))
        self.assertEqual(sorted(via.image_id for via in profiles["https://chatter.test/alexbuilds"].found_via),
                         ["img-1", "img-2"])
        self.assertTrue(all(page.relevant for page in bundle.pages.values()))
        self.assertEqual({image.collection_method for image in bundle.images}, {"deterministic"})

    def test_mirror_merges_into_its_canonical_article(self):
        bundle, _ = self.collect()
        self.assertNotIn(MIRROR, bundle.pages)
        self.assertEqual(bundle.url_aliases[MIRROR], INTERVIEW)
        self.assertEqual(bundle.page_key(MIRROR), INTERVIEW)
        interview = bundle.pages[INTERVIEW]
        self.assertEqual(interview.source_urls, [INTERVIEW, MIRROR])
        self.assertEqual(sorted(via.image_id for via in interview.found_via), ["img-1", "img-2", "img-3"])
        # Raw hit URLs stay unchanged; page_urls are rewritten through the alias map.
        img2 = bundle.images[1]
        self.assertIn(MIRROR, [hit.url for hit in img2.hits])
        self.assertIn(INTERVIEW, img2.page_urls)
        self.assertNotIn(MIRROR, img2.page_urls)

    def test_depth_1_links_and_no_mock_urls(self):
        bundle, _ = self.collect()
        blog = bundle.pages["https://fieldnotes.test/@alexbuilds"]
        img1 = next(via for via in blog.found_via if via.image_id == "img-1")
        self.assertEqual((img1.depth, img1.parent_url), (1, "https://chatter.test/alexbuilds"))
        # Only fetched_url and the raw hits keep mock-local URLs; every identity is an original URL.
        dumped = bundle.model_dump_json(exclude={"pages": {"__all__": {"fetched_url"}}, "images": {"__all__": {"hits"}}})
        self.assertNotIn(self.mock_url, dumped)

    def test_sam_follows_account_links_and_records_the_403(self):
        bundle, _ = self.collect("sam")
        self.assertEqual(len(bundle.images[0].hits), 4)
        self.assertEqual(self.mock_count("/fetch"), 6)
        for url in ("https://codeforge.test/samdraws", "https://fieldnotes.test/@samdraws"):
            [via] = bundle.pages[url].found_via
            self.assertEqual((via.depth, via.hit_position), (1, None))
            self.assertIn(via.parent_url, bundle.pages)
        self.assertEqual(sum(page.extraction == "html" for page in bundle.pages.values()), 5)
        self.assertEqual(bundle.pages[PRIVATE_ALBUM].extraction, "snippet")
        self.assertEqual(bundle.pages[PRIVATE_ALBUM].page_kind, "article")
        self.assertEqual([(e.url, e.reason) for e in bundle.images[0].errors], [(PRIVATE_ALBUM, "forbidden")])

    def test_casey_unknown_and_empty(self):
        bundle, _ = self.collect("casey")
        self.assertEqual((len(bundle.images[0].hits), self.mock_count("/fetch"), len(bundle.pages)), (2, 2, 2))
        for name, scenario in (("unknown", "default"), ("alex", "empty")):
            with self.subTest(name=name, scenario=scenario):
                bundle, _ = self.collect(name, scenario)
                self.assertEqual(sum(len(image.hits) for image in bundle.images), 0)
                self.assertEqual(bundle.pages, {})
                self.assertTrue(all(not image.errors for image in bundle.images))

    def test_matrix_scenarios(self):
        expected = {"ambiguous": ([7, 7, 8], 14, 13, 12), "stale": ([6, 6, 7], 10, 9, 8),
                    "duplicates": ([6, 6, 7], 10, 9, 8)}
        for scenario, (hits, fetches, pages, html) in expected.items():
            with self.subTest(scenario=scenario):
                self.mock_server.counts.clear()
                bundle, _ = self.collect(scenario=scenario)
                self.assertEqual([len(image.hits) for image in bundle.images], hits)
                self.assertEqual(self.mock_count("/fetch"), fetches)
                self.assertEqual(len(bundle.pages), pages)
                self.assertEqual(sum(page.extraction == "html" for page in bundle.pages.values()), html)
                if scenario == "ambiguous":
                    # The Portland Alex's CodeForge is reached only by following that namesake's profiles.
                    vias = bundle.pages["https://codeforge.test/alexframes"].found_via
                    self.assertEqual({via.depth for via in vias}, {1})
                if scenario == "duplicates":
                    # A repeated hit never multiplies provenance.
                    chatter = bundle.pages["https://chatter.test/alexbuilds"]
                    self.assertEqual(len(chatter.found_via), len({via.image_id for via in chatter.found_via}))

    def test_rate_limited_and_server_errors_retry_then_record(self):
        for scenario, status in (("rate_limited", 429), ("server_error", 503)):
            with self.subTest(scenario=scenario):
                self.mock_server.counts.clear()
                bundle, _ = self.collect(scenario=scenario)
                self.assertEqual(self.mock_count("/api/reverse-image"), 9)  # 3 attempts per image
                self.assertEqual(self.mock_count("/fetch"), 0)
                for image in bundle.images:
                    self.assertEqual(image.hits, [])
                    self.assertIsNone(image.request_id)
                    self.assertEqual([(e.url, e.status_code, e.reason) for e in image.errors],
                                     [(None, status, scenario)])

    def test_search_delay_runs_images_concurrently(self):
        self.collect(delay_ms=1000)
        spans = self.backend.search_spans
        self.assertEqual(len(spans), 3)
        self.assertLess(max(start for start, _ in spans), min(end for _, end in spans))  # all three overlap
        self.assertLess(max(end for _, end in spans) - min(start for start, _ in spans), 2.5)


class StageTests(MockServerTestCase):
    def test_run_stage1_writes_a_valid_artifact_and_a_clean_log(self):
        tmp = self.enterContext(__import__("tempfile").TemporaryDirectory())
        capture = mock_capture()
        run_id, run_dir = create_run(tmp, capture.capture.session_id)
        write_artifact(run_dir, 0, capture.model_copy(update={"run_id": run_id}))
        settings = load_settings(backend="mock", environ={"MOCK_INTERNET_URL": self.mock_url}, dotenv_path=None)
        bundle = run_stage1(run_dir, settings)
        self.assertEqual(read_artifact(run_dir, 1), bundle)
        self.assertEqual(bundle.run_id, run_id)
        log = [json.loads(line) for line in (run_dir / "run_log.jsonl").read_text().splitlines()]
        self.assertEqual({entry["stage"] for entry in log}, {1})
        self.assertEqual(sum(entry["endpoint"] == "/api/reverse-image" for entry in log), 3)
        self.assertEqual(sum(entry["endpoint"] == "/fetch" for entry in log), 10)
        self.assertNotIn("test/", (run_dir / "run_log.jsonl").read_text())  # no page URLs in the log
        view = format_hits(bundle)
        self.assertIn("img-1 (front): 4 hits", view)
        self.assertIn("Pages: 9 (8 HTML, 1 snippet-only)", view)


# Live-shaped canned backend


def page_html(*, canonical=None, links=(), person=False, title="Page"):
    head = f'<title>{title}</title>'
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    if person:
        head += '<script type="application/ld+json">{"@type": "Person", "name": "Pat"}</script>'
    anchors = "".join(f'<a href="{url}">{label}</a>' for url, label in links)
    return f"<html><head>{head}</head><body><p>{title}</p>{anchors}</body></html>"


def yandex(*links, snippet="snippet"):
    rows = [{"position": i, "title": f"Hit {i}", "link": link, "source": "example", "snippet": snippet}
            for i, link in enumerate(links, 1)]
    return {"search_metadata": {"id": "req-1", "status": "Success"}, "image_results": rows}


class FakeBackend:
    name = "serpapi"
    origin = None
    search_endpoint = "/search.json"
    fetch_endpoint = "page-fetch"

    def __init__(self, searches, pages, *, fetch_delay=0.0):
        self.searches, self.pages, self.fetch_delay = searches, pages, fetch_delay
        self.fetched, self.released = [], []

    async def reverse_image(self, image):
        result = self.searches[image.image_id]
        if isinstance(result, BackendError):
            raise result
        return result

    async def fetch(self, url):
        self.fetched.append(url)
        await asyncio.sleep(self.fetch_delay)
        page = self.pages.get(url, BackendError("not_found", status_code=404))
        if isinstance(page, BackendError):
            raise page
        return FetchedPage(requested_url=url, final_url=url, fetched_url=url, status_code=200,
                           content_type="text/html", html=page)

    async def release(self, image):
        self.released.append(image.image_id)

    async def aclose(self):
        pass


def live_capture(*image_ids):
    bundle = CaptureBundle.model_validate({
        "session_id": "live-test", "captured_at": "2026-10-10T12:00:00Z",
        "consent": {"confirmed": True, "participant": "Pat", "confirmed_at": "2026-10-10T11:59:00Z",
                    "external_processing": True},
        "images": [{"image_id": image_id, "file": f"{image_id}.jpg", "angle": "front"} for image_id in image_ids],
    })
    return CaptureArtifact(run_id="live-test-0000abcd", capture=bundle, capture_dir="/tmp/live-test",
                           backend="serpapi", scenario="default", llm_mode="off", jev_mode="fake")


class FakeBackendTests(OfflineTestCase):
    def collect(self, backend, *image_ids, **options):
        capture = live_capture(*(image_ids or ("img-1",)))
        bundle, store = asyncio.run(collect_evidence(capture, backend, **options))
        validate_references(capture=capture, raw=bundle)
        return bundle, store

    def test_canonical_tags_never_collapse_accounts(self):
        a, b = "https://github.com/pat", "https://github.com/patrick"
        backend = FakeBackend({"img-1": yandex(a, b)}, {a: page_html(canonical=b), b: page_html()})
        bundle, _ = self.collect(backend)
        self.assertEqual(set(bundle.pages), {a, b})
        self.assertEqual(bundle.url_aliases[a], a)

    def test_canonical_to_an_uncollected_page_is_ignored(self):
        a = "https://news.example/story?id=7"
        backend = FakeBackend({"img-1": yandex(a)}, {a: page_html(canonical="https://elsewhere.example/story")})
        bundle, _ = self.collect(backend)
        self.assertEqual(list(bundle.pages), [a])
        self.assertEqual(bundle.pages[a].canonical_url, "https://elsewhere.example/story")

    def test_failed_hit_without_snippet_keeps_only_the_error(self):
        url = "https://example.com/gone"
        search = yandex(url, snippet="")
        search["image_results"][0]["title"] = ""
        bundle, _ = self.collect(FakeBackend({"img-1": search}, {}))
        self.assertEqual(bundle.pages, {})
        self.assertEqual([(e.url, e.reason) for e in bundle.images[0].errors], [(url, "not_found")])

    def test_budget_counts_distinct_urls_hits_first(self):
        hits = [f"https://example.com/p{i}" for i in range(15)]
        backend = FakeBackend({"img-1": yandex(*hits)}, {url: page_html() for url in hits})
        bundle, _ = self.collect(backend)
        self.assertEqual(backend.fetched, hits[:12])
        self.assertEqual(bundle.images[0].search_extras["budget_skipped"], 3)

    def test_search_failure_is_released_and_recorded(self):
        backend = FakeBackend({"img-1": BackendError("unauthorized", status_code=401),
                               "img-2": yandex("https://example.com/a")}, {"https://example.com/a": page_html()})
        bundle, _ = self.collect(backend, "img-1", "img-2")
        self.assertEqual(sorted(backend.released), ["img-1", "img-2"])
        self.assertEqual([(e.url, e.reason) for e in bundle.images[0].errors], [(None, "unauthorized")])
        self.assertEqual(bundle.images[1].page_urls, ["https://example.com/a"])

    def test_deadline_turns_slow_fetches_into_timeouts(self):
        url = "https://example.com/slow"
        backend = FakeBackend({"img-1": yandex(url)}, {url: page_html()}, fetch_delay=5)
        started = time.monotonic()
        bundle, store = self.collect(backend, image_deadline_s=0.3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(bundle.images[0].search_extras["collection_error"], "timeout")
        self.assertEqual([(e.url, e.reason) for e in bundle.images[0].errors], [(url, "timeout")])
        self.assertEqual(store.pending(), [])  # nothing left running to write late

    def test_face_search_run_gets_a_longer_deadline_and_shows_scores(self):
        from pipeline.search.facecheck_backend import to_payload

        url = "https://github.com/pat"
        backend = FakeBackend({"img-1": to_payload("s1", {"items": [{"score": 91, "url": {"value": url}}]}, demo=False)},
                              {url: page_html(title="Pat")})
        backend.name, backend.extra_deadline_s = "facecheck", 180.0
        capture = live_capture("img-1").model_copy(update={"backend": "facecheck"})
        run_id, run_dir = create_run(self.enterContext(tempfile.TemporaryDirectory()), capture.capture.session_id)
        write_artifact(run_dir, 0, capture.model_copy(update={"run_id": run_id}))
        settings = load_settings(backend="facecheck", environ={"FACECHECK_API_KEY": "k"}, dotenv_path=None)
        with patch("pipeline.stage1_collect.collect_evidence", wraps=collect_evidence) as collect:
            bundle = run_stage1(run_dir, settings, backend=backend)
        self.assertEqual(collect.call_args.kwargs["image_deadline_s"], 35.0 + 180.0)
        self.assertEqual((bundle.images[0].backend, bundle.images[0].hits[0].face_score), ("facecheck", 91))
        self.assertIn(f"1. face  91/100  {url}", format_hits(bundle))
        with self.assertRaisesRegex(ConfigError, "searches with facecheck"):
            run_stage1(run_dir, load_settings(backend="mock", environ={}, dotenv_path=None), backend=backend)

    def test_searched_at_is_the_attempt_start(self):
        before = datetime.now(timezone.utc)
        bundle, _ = self.collect(FakeBackend({"img-1": yandex()}, {}))
        self.assertGreaterEqual(bundle.images[0].searched_at, before)
