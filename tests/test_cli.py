"""End-to-end CLI checks (spec 0.6, 5.1), run against temp folders and a per-class mock internet."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from PIL import Image

from unittest.mock import patch

from pipeline.__main__ import BAD_CONFIG, NOT_BUILT, OK, REFUSED, STAGE_RUNNERS, main
from pipeline.artifacts import UPLOADS_FILE, read_artifact, write_uploads
from pipeline.contracts import UploadRecord, UploadsManifest
from tests.support import FIXTURE_CAPTURES, REPO_ROOT, MockServerTestCase

ALEX = FIXTURE_CAPTURES / "alex"
SERPAPI_ENV = {"SEARCH_BACKEND": "serpapi", "SERPAPI_KEY": "test-serpapi-key"}
HOSTING_ENV = {"IMAGE_BUCKET": "test-bucket", "AWS_REGION": "us-east-1"}


class CliTests(MockServerTestCase):
    def setUp(self):
        super().setUp()
        os.environ["MOCK_INTERNET_URL"] = self.mock_url  # stage 1 searches this class's mock server
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.runs = self.tmp / "runs"

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, "--runs-dir", str(self.runs)])
        return code, out.getvalue(), err.getvalue()

    def run_dirs(self):
        return sorted(self.runs.iterdir()) if self.runs.exists() else []

    def capture_copy(self, edit=None):
        target = self.tmp / "capture"
        shutil.copytree(ALEX, target)
        manifest = json.loads((target / "manifest.json").read_text())
        if edit:
            edit(manifest)
        (target / "manifest.json").write_text(json.dumps(manifest))
        return target

    def real_image_capture(self):
        """The alex consent block with one real PNG instead of mock fixtures."""
        def use_file(manifest):
            manifest["images"] = [{"image_id": "img-1", "angle": "front", "file": "img-1.png"}]
        target = self.capture_copy(use_file)
        Image.new("RGB", (640, 480), (200, 120, 40)).save(target / "img-1.png")
        return target

    def test_until_0_writes_capture_artifact(self):
        code, out, _ = self.cli("run", "--capture", str(ALEX), "--until", "0")
        self.assertEqual(code, OK)
        [run_dir] = self.run_dirs()
        artifact = read_artifact(run_dir, 0)
        self.assertEqual(artifact.run_id, run_dir.name)
        self.assertTrue(artifact.run_id.startswith(artifact.capture.session_id + "-"))
        self.assertEqual([image.mock_fixture for image in artifact.capture.images],
                         ["alex-front", "alex-left", "alex-right"])
        self.assertEqual((artifact.backend, artifact.scenario, artifact.jev_mode, artifact.llm_mode),
                         ("mock", "default", "fake", "off"))
        self.assertIn("Stage 0 done", out)
        log = [json.loads(line) for line in (run_dir / "run_log.jsonl").read_text().splitlines()]
        self.assertEqual(log[0]["stage"], 0)

    def test_full_run_stops_after_built_stages(self):
        code, out, err = self.cli("run", "--capture", str(ALEX))
        missing = next((number for number in range(1, 5) if number not in STAGE_RUNNERS), None)
        self.assertEqual(code, OK if missing is None else NOT_BUILT)
        if missing is not None:
            self.assertIn(f"Stage {missing}", err)
        [run_dir] = self.run_dirs()
        self.assertTrue((run_dir / "00_capture.json").exists())
        self.assertTrue((run_dir / "01_raw_evidence.json").exists())
        self.assertIn("Stage 1 done.", out)

    def test_until_1_collects_evidence_and_prints_hits(self):
        code, out, _ = self.cli("run", "--capture", str(ALEX), "--until", "1")
        self.assertEqual(code, OK)
        [run_dir] = self.run_dirs()
        bundle = read_artifact(run_dir, 1)
        self.assertEqual((bundle.num_images, len(bundle.pages)), (3, 9))
        self.assertIn("img-1 (front): 4 hits", out)
        self.assertFalse((run_dir / "02_profiles.json").exists())

    def test_stage_1_rerun_invalidates_later_outputs(self):
        self.assertEqual(self.cli("run", "--capture", str(ALEX), "--until", "2")[0], OK)
        [run_dir] = self.run_dirs()
        self.assertTrue((run_dir / "02_profiles.json").exists())
        code, out, _ = self.cli("stage", "1", "--run", str(run_dir))
        self.assertEqual(code, OK)
        self.assertTrue((run_dir / "01_raw_evidence.json").exists())
        self.assertFalse((run_dir / "02_profiles.json").exists())

    def test_serpapi_run_needs_hosting_before_creating_a_run(self):
        os.environ.update(SERPAPI_ENV)
        code, _, err = self.cli("run", "--capture", str(self.real_image_capture()), "--until", "1")
        self.assertEqual(code, BAD_CONFIG)
        self.assertIn("IMAGE_BUCKET", err)
        self.assertEqual(self.run_dirs(), [])

    def test_scenario_is_recorded(self):
        code, _, _ = self.cli("run", "--capture", str(ALEX), "--until", "0", "--scenario", "ambiguous")
        self.assertEqual(code, OK)
        self.assertEqual(read_artifact(self.run_dirs()[0], 0).scenario, "ambiguous")

    def test_refuses_unconfirmed_consent(self):
        capture = self.capture_copy(lambda m: m["consent"].update(confirmed=False))
        code, _, err = self.cli("run", "--capture", str(capture), "--until", "0")
        self.assertEqual(code, REFUSED)
        self.assertIn("consent", err.lower())
        self.assertEqual(self.run_dirs(), [])

    def test_refuses_missing_consent_block(self):
        capture = self.capture_copy(lambda m: m.pop("consent"))
        code, _, _ = self.cli("run", "--capture", str(capture), "--until", "0")
        self.assertEqual(code, REFUSED)
        self.assertEqual(self.run_dirs(), [])

    def test_models_need_external_processing_consent(self):
        capture = self.capture_copy(lambda m: m["consent"].update(external_processing=False))
        code, _, _ = self.cli("run", "--capture", str(capture), "--until", "0", "--models")
        self.assertEqual(code, REFUSED)
        self.assertEqual(self.run_dirs(), [])

    def test_models_flag_with_offline_modes_prints_notice(self):
        code, out, _ = self.cli("run", "--capture", str(ALEX), "--until", "0", "--models")
        self.assertEqual(code, OK)
        self.assertIn("no real model calls", out)

    def test_real_model_mode_needs_models_flag(self):
        os.environ.update(LLM_MODE="anthropic", ANTHROPIC_API_KEY="test-anthropic-key")
        code, _, err = self.cli("run", "--capture", str(ALEX), "--until", "0")
        self.assertEqual(code, BAD_CONFIG)
        self.assertNotIn("test-anthropic-key", err)
        self.assertEqual(self.run_dirs(), [])

    def test_serpapi_is_the_default_and_needs_a_key(self):
        del os.environ["SEARCH_BACKEND"]
        code, _, err = self.cli("run", "--capture", str(ALEX), "--until", "0")
        self.assertEqual(code, BAD_CONFIG)
        self.assertIn("SERPAPI_KEY", err)
        self.assertIn("set-key serpapi", err)
        self.assertEqual(self.run_dirs(), [])

    def test_mock_flag_overrides_serpapi_setting(self):
        os.environ.update(SEARCH_BACKEND="serpapi")
        code, _, _ = self.cli("run", "--capture", str(ALEX), "--until", "0", "--mock")
        self.assertEqual(code, OK)
        self.assertEqual(read_artifact(self.run_dirs()[0], 0).backend, "mock")

    def test_serpapi_rejects_mock_fixtures(self):
        os.environ.update(SERPAPI_ENV)
        code, _, err = self.cli("run", "--capture", str(ALEX), "--until", "0")
        self.assertEqual(code, REFUSED)
        self.assertIn("--mock", err)
        self.assertEqual(self.run_dirs(), [])

    def test_serpapi_rejects_scenario(self):
        os.environ.update(SERPAPI_ENV)
        code, _, _ = self.cli("run", "--capture", str(ALEX), "--until", "0", "--scenario", "ambiguous")
        self.assertEqual(code, BAD_CONFIG)

    def test_serpapi_run_with_real_image(self):
        os.environ.update(SERPAPI_ENV)
        capture = self.real_image_capture()
        code, out, err = self.cli("run", "--capture", str(capture), "--until", "0")
        self.assertEqual(code, OK, err)
        self.assertIn("search: serpapi", out)
        self.assertNotIn("test-serpapi-key", out + err)
        [run_dir] = self.run_dirs()
        artifact = read_artifact(run_dir, 0)
        self.assertEqual((artifact.backend, artifact.scenario), ("serpapi", "default"))
        self.assertNotIn("test-serpapi-key", "".join(path.read_text() for path in run_dir.iterdir()))

    def test_stage_of_serpapi_run_needs_external_consent(self):
        os.environ.update(SERPAPI_ENV)
        capture = self.real_image_capture()
        self.assertEqual(self.cli("run", "--capture", str(capture), "--until", "0")[0], OK)
        [run_dir] = self.run_dirs()
        manifest = json.loads((capture / "manifest.json").read_text())
        manifest["consent"]["external_processing"] = False
        (capture / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.cli("stage", "1", "--run", str(run_dir))[0], REFUSED)

    def test_stage_rechecks_consent(self):
        capture = self.capture_copy()
        self.assertEqual(self.cli("run", "--capture", str(capture), "--until", "0")[0], OK)
        [run_dir] = self.run_dirs()
        self.assertEqual(self.cli("stage", "1", "--run", str(run_dir))[0], OK)

        manifest = json.loads((capture / "manifest.json").read_text())
        manifest["consent"]["confirmed"] = False
        (capture / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.cli("stage", "1", "--run", str(run_dir))[0], REFUSED)

    def test_stage_rejects_run_outside_runs_dir(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        self.assertEqual(self.cli("stage", "2", "--run", str(outside))[0], REFUSED)

    def test_purge_works_after_consent_withdrawal(self):
        capture = self.capture_copy()
        self.assertEqual(self.cli("run", "--capture", str(capture), "--until", "0")[0], OK)
        [run_dir] = self.run_dirs()
        (capture / "manifest.json").unlink()
        code, out, _ = self.cli("purge", "--run", str(run_dir))
        self.assertEqual(code, OK)
        self.assertFalse(run_dir.exists())
        self.assertTrue(capture.exists(), "plain purge must not delete the source capture")

    def test_smoke_search_always_uses_serpapi(self):
        code, _, err = self.cli("smoke-search", "--capture", str(ALEX), "--image-id", "img-1")
        self.assertEqual(code, BAD_CONFIG)  # SEARCH_BACKEND=mock doesn't apply; the key is missing
        self.assertIn("SERPAPI_KEY", err)
        os.environ.update(SERPAPI_KEY="test-serpapi-key")
        code, _, err = self.cli("smoke-search", "--capture", str(ALEX), "--image-id", "img-1")
        self.assertEqual(code, BAD_CONFIG)  # image hosting isn't configured
        self.assertIn("IMAGE_BUCKET", err)
        os.environ.update(HOSTING_ENV)
        code, _, _ = self.cli("smoke-search", "--capture", str(ALEX), "--image-id", "img-1")
        self.assertEqual(code, REFUSED)  # mock fixtures can't be searched on SerpApi

    def test_smoke_search_runs_one_image_without_models(self):
        from tests.test_stage1 import FakeBackend, page_html, yandex

        os.environ.update({**SERPAPI_ENV, **HOSTING_ENV})
        capture = self.real_image_capture()
        url = "https://example.com/pat"
        backend = FakeBackend({"img-1": yandex(url)}, {url: page_html(title="Pat")})
        with patch("pipeline.stage1_collect.make_backend", return_value=backend) as make_backend:
            code, out, _ = self.cli("smoke-search", "--capture", str(capture), "--image-id", "img-1")
        self.assertEqual(code, OK)
        self.assertEqual(make_backend.call_args.kwargs, {"refresh": False})
        [run_dir] = self.run_dirs()
        artifact, bundle = read_artifact(run_dir, 0), read_artifact(run_dir, 1)
        self.assertEqual(([image.image_id for image in artifact.capture.images], artifact.llm_mode), (["img-1"], "off"))
        self.assertEqual((bundle.num_images, bundle.images[0].collection_method), (1, "deterministic"))
        self.assertEqual(backend.released, ["img-1"])
        self.assertIn("purge --run", out)

    def test_purge_deletes_uploaded_images(self):
        self.assertEqual(self.cli("run", "--capture", str(ALEX), "--until", "0")[0], OK)
        [run_dir] = self.run_dirs()
        record = UploadRecord(bucket="test-bucket", key="hackmidwest-faces/abc.jpg")
        write_uploads(run_dir, UploadsManifest(run_id=run_dir.name, objects=[record]))
        deleted = []
        os.environ.update(AWS_REGION="us-east-1")
        with patch("pipeline.__main__.remote_deleter", return_value=deleted.append) as deleter:
            code, _, _ = self.cli("purge", "--run", str(run_dir))
        self.assertEqual(code, OK)
        self.assertEqual(deleter.call_args.args, ("us-east-1",))
        self.assertEqual([item.key for item in deleted], [record.key])
        self.assertFalse(run_dir.exists())

    def test_purge_keeps_the_cleanup_record_without_a_region(self):
        self.assertEqual(self.cli("run", "--capture", str(ALEX), "--until", "0")[0], OK)
        [run_dir] = self.run_dirs()
        write_uploads(run_dir, UploadsManifest(run_id=run_dir.name, objects=[UploadRecord(bucket="b", key="k.jpg")]))
        code, _, _ = self.cli("purge", "--run", str(run_dir))
        self.assertEqual(code, REFUSED)
        self.assertEqual([path.name for path in run_dir.iterdir()], [UPLOADS_FILE])

    def test_module_entry_point(self):
        """The 0.6 command in a subprocess. Patches don't cross processes, so the child starts
        the offline harness itself: no repo .env, loopback-only sockets."""
        bootstrap = (
            "import runpy\n"
            "from tests.support import OfflineTestCase\n"
            "case = OfflineTestCase()\n"
            "case.setUp()\n"
            "try:\n"
            "    runpy.run_module('pipeline', run_name='__main__')\n"
            "finally:\n"
            "    case.doCleanups()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", bootstrap, "run", "--capture", "tests/fixtures/captures/alex", "--until", "0",
             "--mock", "--runs-dir", str(self.runs)],
            cwd=REPO_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, OK, result.stderr)
        self.assertEqual(len(self.run_dirs()), 1)
