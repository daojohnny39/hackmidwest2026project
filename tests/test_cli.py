"""End-to-end CLI checks for Section 0 (spec 0.6), run against temp folders."""

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

from pipeline.__main__ import BAD_CONFIG, NOT_BUILT, OK, REFUSED, main
from pipeline.artifacts import read_artifact
from tests.support import FIXTURE_CAPTURES, REPO_ROOT, OfflineTestCase

ALEX = FIXTURE_CAPTURES / "alex"
SERPAPI_ENV = {"SEARCH_BACKEND": "serpapi", "SERPAPI_KEY": "test-serpapi-key"}


class CliTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
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
        code, _, err = self.cli("run", "--capture", str(ALEX))
        self.assertEqual(code, NOT_BUILT)
        self.assertIn("Stage 1", err)
        [run_dir] = self.run_dirs()
        self.assertTrue((run_dir / "00_capture.json").exists())

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
        self.assertEqual(self.cli("stage", "1", "--run", str(run_dir))[0], NOT_BUILT)

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
        code, _, _ = self.cli("smoke-search", "--capture", str(ALEX), "--image-id", "img-1")
        self.assertEqual(code, REFUSED)  # mock fixtures can't be searched on SerpApi

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
