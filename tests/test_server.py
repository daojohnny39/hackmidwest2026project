"""The website's search API (server.py): upload checks, the consent gate, and the investigation lifecycle."""

import asyncio
import dataclasses
import io
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from starlette.testclient import TestClient

from pipeline import progress
from pipeline.artifacts import read_artifact
from pipeline.config import load_settings
from pipeline.contracts import Subject
from pipeline.findings import Findings
from pipeline.server import MAX_IMAGE_BYTES, create_app, run_search, write_capture
from tests.support import MockServerTestCase, OfflineTestCase


def jpeg(size=(480, 480)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (120, 90, 60)).save(output, "JPEG")
    return output.getvalue()


AUTHORIZED = {"authorized": "true", "externalProcessing": "true"}


def wait_for(client, investigation_id, timeout=20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/search/{investigation_id}").json()
        if data["status"] in ("completed", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"investigation {investigation_id} didn't finish")


class ServerTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.captures = Path(folder.name) / "captures"
        self.runs = Path(folder.name) / "runs"
        self.searched = []
        self.findings = Findings(candidates=[{"id": "src-1", "sourceUrl": "https://example.com/p",
                                              "confidence": {"score": 97, "level": "likely", "reasons": []}}])

    def client(self, *, live=False, search=None) -> TestClient:
        settings = load_settings(backend="mock")
        if live:  # what create_app reads; the fake search never touches a backend
            settings = dataclasses.replace(settings, search_backend="rekognition", live=True)

        def fake_search(folder):
            self.searched.append(folder)
            return "web-run", self.findings

        app = create_app(settings, runs_dir=self.runs, captures_dir=self.captures, search=search or fake_search)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def post(self, client, data=AUTHORIZED, photo=None, content_type="image/jpeg"):
        files = {"image": ("reference.jpg", jpeg() if photo is None else photo, content_type)}
        return client.post("/api/search", data=data, files=files)

    def test_health_probe_does_not_start_a_search(self):
        client = self.client(live=True)
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"service": "cyberpi-search", "status": "ok", "search": "rekognition"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.searched, [])
        self.assertFalse(self.captures.exists())
        self.assertFalse(self.runs.exists())

    def test_search_runs_and_reports_findings(self):
        client = self.client(live=True)
        response = self.post(client, data={**AUTHORIZED, "firstName": "Sample", "age": "32"})
        self.assertEqual(response.status_code, 202)
        accepted = response.json()
        self.assertRegex(accepted["investigationId"], r"^inv_[0-9a-f]{16}$")
        self.assertEqual(accepted["status"], "queued")
        result = wait_for(client, accepted["investigationId"])
        self.assertEqual(result, {"investigationId": accepted["investigationId"], "status": "completed",
                                  "candidates": self.findings.candidates, "warnings": []})
        self.assertEqual(response.headers["cache-control"], "no-store")

        # The upload became a live capture folder: the photo plus a manifest with both consents.
        [folder] = self.searched
        self.assertEqual(folder.parent, self.captures)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["consent"]["external_processing"], True)
        self.assertNotIn("participant", manifest["consent"])
        # Given details are the subject; the rest stay in the JSON, empty.
        self.assertEqual(manifest["subject"], {"first_name": "Sample", "last_name": "", "city": "", "age": 32})
        self.assertEqual(manifest["images"], [{"image_id": "front", "angle": "front", "file": "front.jpg"}])
        self.assertEqual((folder / "front.jpg").read_bytes(), jpeg())

    def test_authorization_and_external_processing_are_required(self):
        client = self.client(live=True)
        for data, code in (({}, "authorization_required"), ({"authorized": "yes"}, "authorization_required"),
                           ({"authorized": "true"}, "external_processing_required")):
            response = self.post(client, data=data)
            self.assertEqual((response.status_code, response.json()), (403, {"error": code}))
        self.assertFalse(self.captures.exists())

    def test_mock_search_needs_no_external_consent_and_keeps_no_photo(self):
        client = self.client()
        response = self.post(client, data={"authorized": "true"})
        self.assertEqual(response.status_code, 202)
        wait_for(client, response.json()["investigationId"])
        [folder] = self.searched
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["manifest.json"])
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["images"], [{"image_id": "front", "angle": "front", "mock_fixture": "alex-front"}])
        self.assertEqual(manifest["subject"], {"first_name": "", "last_name": "", "city": "", "age": None})

    def test_subject_fields_are_trimmed_into_the_manifest(self):
        client = self.client(live=True)
        details = {"firstName": "  Sample ", "lastName": "Person", "city": " Kansas  City ", "age": " 7 "}
        wait_for(client, self.post(client, data={**AUTHORIZED, **details}).json()["investigationId"])
        manifest = json.loads((self.searched[0] / "manifest.json").read_text())
        self.assertEqual(manifest["subject"],
                         {"first_name": "Sample", "last_name": "Person", "city": "Kansas City", "age": 7})

    def test_bad_uploads_are_refused(self):
        client = self.client(live=True)
        cases = [
            (dict(photo=b"GIF89a not a photo", content_type="image/gif"), 400, "unsupported_photo"),
            (dict(photo=b"\xff\xd8\xff" + b"\x00" * 100), 400, "unsupported_photo"),  # JPEG magic, corrupt body
            (dict(photo=b"\xff\xd8\xff" + b"\x00" * MAX_IMAGE_BYTES), 413, "photo_too_large"),
            (dict(data={**AUTHORIZED, "age": "-1"}), 400, "invalid_field"),
            (dict(data={**AUTHORIZED, "age": "151"}), 400, "invalid_field"),
            (dict(data={**AUTHORIZED, "age": "3.5"}), 400, "invalid_field"),
            (dict(data={**AUTHORIZED, "city": "x" * 121}), 400, "invalid_field"),
        ]
        for options, status, code in cases:
            response = self.post(client, **options)
            self.assertEqual((response.status_code, response.json()), (status, {"error": code}), options.keys())
        missing = client.post("/api/search", data=AUTHORIZED, files={"other": ("x.txt", b"x", "text/plain")})
        self.assertEqual((missing.status_code, missing.json()), (400, {"error": "photo_required"}))
        self.assertEqual(self.searched, [])
        self.assertEqual(list(self.captures.iterdir()) if self.captures.exists() else [], [])

    def test_failures_never_look_like_zero_findings(self):
        def broken(_folder):
            progress.report("comparing")
            raise RuntimeError("stage 1 crashed")

        client = self.client(search=broken)
        result = wait_for(client, self.post(client).json()["investigationId"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("candidates", result)
        self.assertNotIn("stage", result)
        self.assertNotIn("crashed", json.dumps(result))

        self.findings = Findings(candidates=[], failed=True, warnings=["search_failed"])
        client = self.client()
        result = wait_for(client, self.post(client).json()["investigationId"])
        self.assertEqual(result["status"], "failed")

    def test_running_search_reports_live_stages_from_async_work(self):
        stages = ("preparing", "searching", "comparing", "reviewing", "checking", "finalizing")
        ready = [threading.Event() for _ in stages]
        advance = [threading.Event() for _ in stages]

        async def pipeline():
            for stage, started, release in zip(stages, ready, advance):
                # Stage 1 runs async tasks and moves blocking work into threads.
                await asyncio.to_thread(progress.report, stage)
                started.set()
                await asyncio.to_thread(release.wait, 10)

        def search(_folder):
            asyncio.run(pipeline())
            return "web-run", self.findings

        client = self.client(search=search)
        accepted = self.post(client).json()
        self.assertNotIn("stage", accepted)
        try:
            for stage, started, release in zip(stages, ready, advance):
                self.assertTrue(started.wait(5))
                response = client.get(f"/api/search/{accepted['investigationId']}").json()
                self.assertEqual((response["status"], response["stage"]), ("running", stage))
                release.set()
            result = wait_for(client, accepted["investigationId"])
            self.assertEqual(result["status"], "completed")
            self.assertNotIn("stage", result)
            # The worker's reporter must not leak into this request's context.
            progress.report("searching")
            self.assertEqual(client.get(f"/api/search/{accepted['investigationId']}").json(), result)
        finally:
            for release in advance:
                release.set()

    def test_unknown_investigations(self):
        client = self.client()
        for investigation_id in ("inv_0000000000000000", "inv_x", "INV_0000000000000000"):
            response = client.get(f"/api/search/{investigation_id}")
            self.assertEqual((response.status_code, response.json()), (404, {"error": "not_found"}))

    def test_one_search_at_a_time_and_a_bounded_queue(self):
        release = threading.Event()
        running = []

        def slow(folder):
            running.append(folder)
            release.wait(10)
            return "web-run", self.findings

        client = self.client(search=slow)
        first = self.post(client).json()["investigationId"]
        deadline = time.monotonic() + 5
        while not running and time.monotonic() < deadline:
            time.sleep(0.01)
        queued = [self.post(client).json()["investigationId"] for _ in range(3)]
        self.assertEqual(client.get(f"/api/search/{first}").json()["status"], "running")
        self.assertEqual({client.get(f"/api/search/{i}").json()["status"] for i in queued}, {"queued"})
        busy = self.post(client)
        self.assertEqual((busy.status_code, busy.json()), (429, {"error": "busy"}))
        release.set()
        self.assertEqual({wait_for(client, i)["status"] for i in [first, *queued]}, {"completed"})


class MockRunTests(MockServerTestCase):
    def test_upload_runs_stages_0_and_1_on_the_mock_internet(self):
        os.environ["MOCK_INTERNET_URL"] = self.mock_url
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        runs = Path(folder.name) / "runs"
        app = create_app(load_settings(backend="mock"), runs_dir=runs, captures_dir=Path(folder.name) / "captures")
        with TestClient(app) as client, patch("pipeline.progress.report", wraps=progress.report) as report:
            response = client.post("/api/search", data={"authorized": "true", "firstName": "Alex", "lastName": "Morgan"},
                                   files={"image": ("reference.jpg", jpeg(), "image/jpeg")})
            result = wait_for(client, response.json()["investigationId"])
        self.assertEqual([call.args[0] for call in report.call_args_list], ["searching", "reviewing", "finalizing"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["candidates"]), 4)  # alex-front's four search hits
        self.assertEqual({candidate["confidence"]["level"] for candidate in result["candidates"]}, {"unknown"})
        # The form's name reached the findings. Chatter's profile only says "Alex M.".
        names = {candidate["sourceUrl"]: candidate["confidence"]["reasons"][-1] for candidate in result["candidates"]}
        self.assertEqual(names["https://linkup.test/in/alexbuilds"], "Your name appears on this page.")
        self.assertEqual(names["https://chatter.test/alexbuilds"], "Your name doesn't appear on this page.")
        [run_dir] = runs.iterdir()
        self.assertTrue(run_dir.name.startswith("web-"))
        raw = read_artifact(run_dir, 1)
        self.assertEqual([image.image_id for image in raw.images], ["front"])
        self.assertEqual(self.mock_count("/api/reverse-image"), 1)

    def test_run_search_ends_with_the_threat_check(self):
        os.environ["MOCK_INTERNET_URL"] = self.mock_url
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        capture = write_capture(root / "captures", jpeg(), Subject(), external=False, mock=True)
        checked = []

        class Checker:  # server.ThreatCheck
            def check(self, findings):
                checked.append([candidate["sourceUrl"] for candidate in findings.candidates])
                for candidate in findings.candidates:
                    candidate["threat"] = {"score": 0, "level": "low", "indicators": []}

        with patch("pipeline.progress.report", wraps=progress.report) as report:
            _, findings = run_search(capture, settings=load_settings(backend="mock"), runs_dir=root / "runs",
                                     threats=Checker())
        self.assertEqual([call.args[0] for call in report.call_args_list],
                         ["searching", "reviewing", "checking", "finalizing"])
        self.assertEqual(checked, [[candidate["sourceUrl"] for candidate in findings.candidates]])
        self.assertEqual(len(checked[0]), 4)
        self.assertEqual({candidate["threat"]["level"] for candidate in findings.candidates}, {"low"})
