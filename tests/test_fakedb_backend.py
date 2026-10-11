"""Dev mode's fake image database backend (search/fakedb_backend.py), offline.

httpx.MockTransport stands in for Zach's database (GET /api/images and /img/<file>) and FakeRekognition
(from the V3 tests) for boto3's Rekognition client. Photos are blank JPEGs told apart by their width:
101 matches the user's face at 99.62, 102 is a stranger at 61.
"""

import asyncio
import dataclasses
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from starlette.testclient import TestClient

from pipeline.config import DEFAULT_FAKE_DB_URL, ConfigError, load_settings
from pipeline.contracts import CaptureImage, Subject, validate_references
from pipeline.findings import build_findings
from pipeline.search.base import BackendError, parse_image_results
from pipeline.search.fakedb_backend import (
    FRAUD_SITE, FakeDbBackend, FakeDbThreats, Row, check_database, fake_threat, page_html, parse_rows, source_url,
)
from pipeline.server import create_app
from pipeline.stage1_collect import collect_evidence
from tests.support import OfflineTestCase
from tests.test_rekognition_backend import FakeRekognition, jpeg
from tests.test_stage1 import live_capture

DB = "http://100.64.0.7:8000"
IMAGE = CaptureImage(image_id="img-1", file="img-1.jpg", angle="front")
TOTAL = 1200


def celeb(number, site="instagram.com"):
    return {"file_name": f"{number:06d}.png", "first_name": "Jack", "middle_name": None, "last_name": "Rogers",
            "foundOn": site}


ME = {"file_name": "johnny_fake.png", "first_name": "Mark", "middle_name": None, "last_name": "Smith",
      "foundOn": ["anonfacez.icu", "www.instagram.com", FRAUD_SITE]}
STRANGER = {"file_name": "stranger.png", "first_name": "Sam", "middle_name": "", "last_name": "Lee",
            "foundOn": "linkedin.com"}
# offset -> the rows of that page. Uploads sort after the CelebA block; one also sits on the first page.
PAGES = {0: [celeb(1), celeb(2), {**STRANGER, "foundOn": ["linkedin.com"]}],
         TOTAL - 500: [celeb(202598), celeb(202599, "freepicleaks.top"), ME, STRANGER]}
PHOTOS = {"/img/johnny_fake.png": 101, "/img/stranger.png": 102}


class DatabaseTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.folder / "img-1.jpg").write_bytes(jpeg(1000, 800))
        self.requests = []

    def database(self, request):
        self.requests.append(str(request.url))
        path = request.url.path
        if path == "/api/images":
            params = {name: values[0] for name, values in parse_qs(urlsplit(str(request.url)).query).items()}
            offset = int(params["offset"])
            return httpx.Response(200, json={"total": TOTAL, "all_total": TOTAL, "offset": offset,
                                             "items": PAGES.get(offset, [])})
        if path in PHOTOS:
            return httpx.Response(200, content=jpeg(PHOTOS[path]), headers={"content-type": "image/png"})
        return httpx.Response(404, json={"error": "not found"})

    def backend(self, handler=None, rekognition=None):
        self.rekognition = rekognition or FakeRekognition()
        backend = FakeDbBackend(db_url=DB + "/", capture_dir=self.folder, rekognition=self.rekognition,
                                min_similarity=80.0,
                                client=httpx.AsyncClient(transport=httpx.MockTransport(handler or self.database)))
        self.addCleanup(lambda: asyncio.run(backend.aclose()))
        return backend


class RowTests(OfflineTestCase):
    def test_rows_keep_safe_files_and_valid_sites(self):
        rows = parse_rows({"items": [
            ME,
            {"file_name": "../etc/passwd", "foundOn": "x.com"},
            {"file_name": "no-site.png", "foundOn": ["not a site", 7]},
            {"file_name": "plain.jpg", "first_name": "  Ana ", "middle_name": None, "last_name": "Diaz",
             "foundOn": "X.com"},
            "junk",
        ]})
        self.assertEqual(rows, [Row("johnny_fake.png", "Mark Smith", ("anonfacez.icu", "instagram.com", FRAUD_SITE)),
                                Row("plain.jpg", "Ana Diaz", ("x.com",))])
        with self.assertRaises(BackendError):
            parse_rows({"error": "bad"})

    def test_source_urls_are_accounts_on_social_sites(self):
        row = Row("johnny_fake.png", "Mark Smith", ())
        self.assertEqual(source_url(row, "instagram.com"), "https://instagram.com/marksmith_johnny_fake")
        self.assertEqual(source_url(row, "linkedin.com"), "https://linkedin.com/in/marksmith_johnny_fake")
        self.assertEqual(source_url(row, "anonfacez.icu"), "https://anonfacez.icu/photos/johnny_fake")
        self.assertEqual(source_url(row, FRAUD_SITE), f"https://{FRAUD_SITE}/members/marksmith_johnny_fake")
        self.assertEqual(source_url(Row("a.png", "", ()), "x.com"), "https://x.com/user_a")


class SearchTests(DatabaseTestCase):
    def test_uploads_are_compared_and_matches_returned(self):
        backend = self.backend()
        result = parse_image_results(asyncio.run(backend.reverse_image(IMAGE)))

        # The first and last pages were read; the name or the photo never went to the database.
        self.assertEqual([url for url in self.requests if "/api/images" in url],
                         [f"{DB}/api/images?offset=0&limit=500", f"{DB}/api/images?offset=700&limit=500"])
        # Only the two uploads were downloaded and compared, once per site; CelebA photos never were.
        self.assertEqual(sorted({url for url in self.requests if "/img/" in url}),
                         [f"{DB}/img/johnny_fake.png", f"{DB}/img/stranger.png"])
        self.assertEqual(len(self.rekognition.compare_calls), 4)
        self.assertEqual([(hit.url, hit.face_score, hit.thumbnail_url) for hit in result.hits], [
            ("https://anonfacez.icu/photos/johnny_fake", 99, f"{DB}/img/johnny_fake.png"),
            ("https://instagram.com/marksmith_johnny_fake", 99, f"{DB}/img/johnny_fake.png"),
            (f"https://{FRAUD_SITE}/members/marksmith_johnny_fake", 99, f"{DB}/img/johnny_fake.png"),
        ])
        self.assertEqual(result.extras["outcomes"], {"matched": 3, "below_threshold": 1})

        page = asyncio.run(backend.fetch("https://www.instagram.com/marksmith_johnny_fake/"))
        self.assertEqual((page.status_code, page.final_url), (200, "https://instagram.com/marksmith_johnny_fake"))
        self.assertIn("<title>Mark Smith on instagram.com</title>", page.html)
        self.assertIn(f'content="{DB}/img/johnny_fake.png"', page.html)
        with self.assertRaises(BackendError) as caught:
            asyncio.run(backend.fetch("https://instagram.com/someone_else"))
        self.assertEqual(caught.exception.reason, "not_found")

    def test_only_database_photos_are_downloaded(self):
        backend = self.backend()
        with self.assertRaises(BackendError) as caught:
            asyncio.run(backend._download("https://example.com/img/x.png"))
        self.assertEqual(caught.exception.reason, "blocked_destination")
        self.assertEqual(self.requests, [])

    def test_database_failures_are_backend_errors(self):
        for handler, reason in ((lambda request: httpx.Response(503), "server_error"),
                                (lambda request: httpx.Response(200, text="<html>"), "invalid_response"),
                                (lambda request: httpx.Response(200, json={"items": []}), "invalid_response")):
            with self.subTest(reason=reason):
                with self.assertRaises(BackendError) as caught:
                    asyncio.run(self.backend(handler).reverse_image(IMAGE))
                self.assertEqual(caught.exception.reason, reason)

        def unreachable(request):
            raise httpx.ConnectError("no route")

        with self.assertRaises(BackendError) as caught:
            asyncio.run(self.backend(unreachable).reverse_image(IMAGE))
        self.assertEqual(caught.exception.reason, "connection_error")

    def test_end_to_end_findings_with_dev_mode_threats(self):
        backend = self.backend()
        capture = live_capture("img-1").model_copy(update={"backend": "fakedb", "capture_dir": str(self.folder)})
        bundle, _ = asyncio.run(collect_evidence(capture, backend))
        validate_references(capture=capture, raw=bundle)
        findings = build_findings(bundle, Subject(first_name="Johnny", last_name="Dao"))
        FakeDbThreats().check(findings)

        self.assertEqual([(c["sourceUrl"], c["confidence"]["score"], c["threat"]["level"]) for c in findings.candidates],
                         [("https://anonfacez.icu/photos/johnny_fake", 99, "low"),
                          ("https://instagram.com/marksmith_johnny_fake", 99, "low"),
                          (f"https://{FRAUD_SITE}/members/marksmith_johnny_fake", 99, "high")])
        instagram, fraud = findings.candidates[1:]
        self.assertEqual(instagram["sourceType"], "Instagram profile")
        self.assertIn("Your name doesn't appear on this page.", instagram["confidence"]["reasons"])
        self.assertEqual((fraud["sourceType"], fraud["pageTitle"]), ("Web profile", "Mark Smith | Member profile"))
        self.assertEqual(fraud["threat"]["score"], 100)
        self.assertEqual(findings.warnings, [])


class ThreatTests(OfflineTestCase):
    def test_only_the_fraud_site_is_a_threat(self):
        fraud = fake_threat(f"https://www.{FRAUD_SITE}/members/x")
        self.assertEqual((fraud["score"], fraud["level"]), (100, "high"))
        self.assertIn("malicious fraud site", fraud["indicators"][0])
        for url in ("https://anonfacez.icu/photos/x", "https://instagram.com/someone", "not a url"):
            with self.subTest(url=url):
                threat = fake_threat(url)
                self.assertEqual((threat["score"], threat["level"]), (0, "low"))
        fraud["indicators"].append("changed")  # each threat is a copy
        self.assertEqual(len(fake_threat(f"https://{FRAUD_SITE}/members/y")["indicators"]), 2)

    def test_fraud_page_is_a_fake_profile(self):
        page = page_html(Row("a.png", "Mark </script> Smith", (FRAUD_SITE,)), FRAUD_SITE, f"{DB}/img/a.png")
        self.assertIn('"@type": "Person"', page)
        self.assertIn("Guaranteed 300% returns", page)
        self.assertEqual(page.count("</script>"), 1)  # the name can't close the JSON-LD block


class SettingsTests(OfflineTestCase):
    def test_fakedb_settings(self):
        settings = load_settings(backend="fakedb", environ={"AWS_REGION": "us-east-2"}, dotenv_path=None)
        self.assertEqual((settings.search_backend, settings.live, settings.fake_db_url), ("fakedb", True, DEFAULT_FAKE_DB_URL))
        self.assertFalse(settings.needs_hosting)
        self.assertIsNone(load_settings(backend="mock", environ={}, dotenv_path=None).fake_db_url)
        custom = load_settings(backend="fakedb", environ={"AWS_REGION": "us-east-2", "FAKE_DB_URL": DB + "/"},
                               dotenv_path=None)
        self.assertEqual(custom.fake_db_url, DB)

    def test_fakedb_needs_aws_region_and_an_origin(self):
        with self.assertRaisesRegex(ConfigError, "AWS_REGION"):
            load_settings(backend="fakedb", environ={}, dotenv_path=None)
        for bad in ("ftp://db.example", "http://user:secret@db.example", "http://db.example/api", "http://db.example:99999"):
            with self.subTest(url=bad), self.assertRaises(ConfigError) as caught:
                load_settings(backend="fakedb", environ={"AWS_REGION": "us-east-2", "FAKE_DB_URL": bad}, dotenv_path=None)
            self.assertNotIn("secret", str(caught.exception))
        # Only dev mode reads it, so a bad value doesn't break normal mode.
        load_settings(backend="mock", environ={"FAKE_DB_URL": "nonsense"}, dotenv_path=None)

    def test_check_database(self):
        with patch("pipeline.search.fakedb_backend.httpx.get",
                   return_value=httpx.Response(200, json={"total": 3, "all_total": 3, "items": []})) as get:
            self.assertEqual(check_database(DB), 3)
        self.assertEqual(get.call_args.args, (f"{DB}/api/images",))
        with patch("pipeline.search.fakedb_backend.httpx.get", side_effect=httpx.ConnectError("no route")):
            with self.assertRaisesRegex(ConfigError, "tailnet"):
                check_database(DB)

    def test_server_health_names_dev_mode(self):
        settings = dataclasses.replace(load_settings(backend="mock"), search_backend="fakedb", live=True)
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(
                settings, runs_dir=Path(folder) / "runs", captures_dir=Path(folder) / "captures",
                search=lambda capture: ("run", None))) as client:
            self.assertEqual(client.get("/api/health").json(),
                             {"service": "cyberpi-search", "status": "ok", "search": "fakedb"})
