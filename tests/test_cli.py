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

from pipeline.__main__ import BAD_CONFIG, NOT_BUILT, OK, REFUSED, main
from pipeline.artifacts import read_artifact
from tests.support import FIXTURE_CAPTURES, REPO_ROOT, OfflineTestCase

ALEX = FIXTURE_CAPTURES / "alex"
LIVE_ENV = {"SERPAPI_KEY": "test-serpapi-key", "IMAGE_BUCKET": "test-bucket", "AWS_REGION": "us-east-1"}


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

    def test_serpapi_backend_needs_live_flag(self):
        os.environ.update(SEARCH_BACKEND="serpapi", **LIVE_ENV)
        code, _, _ = self.cli("run", "--capture", str(ALEX), "--until", "0")
        self.assertEqual(code, BAD_CONFIG)
        self.assertEqual(self.run_dirs(), [])

    def test_live_without_credentials_fails_before_capture(self):
        code, _, err = self.cli("run", "--capture", str(ALEX), "--until", "0", "--live")
        self.assertEqual(code, BAD_CONFIG)
        self.assertIn("SERPAPI_KEY", err)
        self.assertEqual(self.run_dirs(), [])

    def test_live_rejects_mock_fixtures(self):
        os.environ.update(LIVE_ENV)
        code, _, _ = self.cli("run", "--capture", str(ALEX), "--until", "0", "--live")
        self.assertEqual(code, REFUSED)
        self.assertEqual(self.run_dirs(), [])

    def test_live_rejects_scenario(self):
        os.environ.update(LIVE_ENV)
        code, _, _ = self.cli("run", "--capture", str(ALEX), "--until", "0", "--live", "--scenario", "ambiguous")
        self.assertEqual(code, BAD_CONFIG)

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

    def test_smoke_search_needs_live(self):
        code, _, _ = self.cli("smoke-search", "--capture", str(ALEX), "--image-id", "img-1")
        self.assertEqual(code, BAD_CONFIG)

    def test_module_entry_point(self):
        """The exact 0.6 command, as a subprocess with the offline environment."""
        result = subprocess.run(
            [sys.executable, "-m", "pipeline", "run", "--capture", "tests/fixtures/captures/alex", "--until", "0",
             "--runs-dir", str(self.runs)],
            cwd=REPO_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, OK, result.stderr)
        self.assertEqual(len(self.run_dirs()), 1)
