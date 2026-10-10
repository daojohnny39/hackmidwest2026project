"""Run folders, atomic artifacts, sanitized logs, uploads.json, and purge (spec 0.5, 1.2, 5.1)."""

import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from unittest.mock import patch

from pipeline import artifacts
from pipeline.artifacts import (
    ArtifactError,
    create_run,
    invalidate_from,
    log_event,
    new_run_id,
    purge_run,
    read_artifact,
    read_uploads,
    resolve_run_dir,
    write_artifact,
    write_uploads,
)
from pipeline.contracts import (
    STAGE_FILES,
    CaptureArtifact,
    CaptureBundle,
    CaptureImage,
    Consent,
    UploadRecord,
    UploadsManifest,
)
from tests.support import OfflineTestCase

SESSION = "2026-10-10T14-02-11-alex"
WHEN = datetime(2026, 10, 10, 14, 2, 11, tzinfo=timezone.utc)


def capture_artifact(run_id: str, capture_dir: Path | str = "/nonexistent/capture") -> CaptureArtifact:
    return CaptureArtifact(
        run_id=run_id,
        capture=CaptureBundle(
            session_id=SESSION,
            captured_at=WHEN,
            consent=Consent(confirmed=True, participant="Alex Test", confirmed_at=WHEN, external_processing=True),
            images=[CaptureImage(image_id="img-1", angle="front", mock_fixture="alex-front")],
        ),
        capture_dir=str(capture_dir),
        backend="mock",
        scenario="default",
        llm_mode="off",
        jev_mode="fake",
    )


class ArtifactTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = self.tmp / "runs"

    def new_run(self, capture_dir: Path | str = "/nonexistent/capture") -> Path:
        run_id, run_dir = create_run(self.runs, SESSION)
        write_artifact(run_dir, 0, capture_artifact(run_id, capture_dir))
        return run_dir

    def symlink(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")


class RunCreationTests(ArtifactTestCase):
    def test_run_id_format_and_uniqueness(self):
        ids = {new_run_id(SESSION) for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for run_id in ids:
            self.assertRegex(run_id, rf"^{re.escape(SESSION)}-[0-9a-f]{{8}}$")

    def test_create_run_makes_runs_dir_and_exclusive_folder(self):
        run_id, run_dir = create_run(self.runs, SESSION)
        self.assertTrue(run_dir.is_dir())
        self.assertEqual(run_dir, self.runs / run_id)
        self.assertEqual(run_dir.parent, self.runs)

    def test_create_run_retries_on_collision(self):
        (self.runs / f"{SESSION}-aaaaaaaa").mkdir(parents=True)
        ids = [f"{SESSION}-aaaaaaaa", f"{SESSION}-bbbbbbbb"]
        with patch.object(artifacts, "new_run_id", side_effect=ids):
            run_id, run_dir = create_run(self.runs, SESSION)
        self.assertEqual(run_id, f"{SESSION}-bbbbbbbb")
        self.assertTrue(run_dir.is_dir())

    def test_create_run_gives_up_after_attempts(self):
        (self.runs / f"{SESSION}-aaaaaaaa").mkdir(parents=True)
        with patch.object(artifacts, "new_run_id", return_value=f"{SESSION}-aaaaaaaa") as generator:
            with self.assertRaises(ArtifactError):
                create_run(self.runs, SESSION, attempts=3)
        self.assertEqual(generator.call_count, 3)

    def test_create_run_rejects_unsafe_session_id(self):
        for session in ("../escape", "a/b", ""):
            with self.subTest(session=session), self.assertRaises(ArtifactError):
                create_run(self.runs, session)
        self.assertEqual(list(self.runs.iterdir()), [])


class ArtifactFileTests(ArtifactTestCase):
    def test_stage0_round_trip(self):
        run_id, run_dir = create_run(self.runs, SESSION)
        artifact = capture_artifact(run_id)
        path = write_artifact(run_dir, 0, artifact)
        self.assertEqual(path, run_dir / "00_capture.json")
        self.assertEqual(read_artifact(run_dir, 0), artifact)

    def test_write_is_atomic(self):
        run_dir = self.new_run()
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()), ["00_capture.json"])
        before = (run_dir / "00_capture.json").read_bytes()
        changed = capture_artifact(run_dir.name).model_copy(update={"scenario": "ambiguous"})
        with patch.object(artifacts.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_artifact(run_dir, 0, changed)
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()), ["00_capture.json"])
        self.assertEqual((run_dir / "00_capture.json").read_bytes(), before)

    def test_write_rejects_mismatched_run_id(self):
        _, run_dir = create_run(self.runs, SESSION)
        with self.assertRaisesRegex(ArtifactError, "run_id"):
            write_artifact(run_dir, 0, capture_artifact(f"{SESSION}-00000000"))
        self.assertEqual(list(run_dir.iterdir()), [])

    def test_write_rejects_invalid_artifact(self):
        run_id, run_dir = create_run(self.runs, SESSION)
        broken = capture_artifact(run_id).model_copy(update={"backend": "google"})
        with self.assertRaises(ArtifactError):
            write_artifact(run_dir, 0, broken)
        with self.assertRaises(ArtifactError):
            write_artifact(run_dir, 9, capture_artifact(run_id))
        self.assertEqual(list(run_dir.iterdir()), [])

    def test_read_rejects_mismatched_run_id(self):
        run_dir = self.new_run()
        _, other = create_run(self.runs, SESSION)
        shutil.copy(run_dir / "00_capture.json", other / "00_capture.json")
        with self.assertRaisesRegex(ArtifactError, "run_id"):
            read_artifact(other, 0)

    def test_read_rejects_malformed_json(self):
        _, run_dir = create_run(self.runs, SESSION)
        (run_dir / "00_capture.json").write_text("{not json")
        with self.assertRaises(ArtifactError):
            read_artifact(run_dir, 0)

    def test_read_rejects_schema_invalid_json(self):
        run_dir = self.new_run()
        data = json.loads((run_dir / "00_capture.json").read_text())
        data["capture"]["consent"]["confirmed"] = "yes"
        (run_dir / "00_capture.json").write_text(json.dumps(data))
        with self.assertRaises(ArtifactError):
            read_artifact(run_dir, 0)
        (run_dir / "00_capture.json").write_text(json.dumps({"run_id": run_dir.name}))
        with self.assertRaises(ArtifactError):
            read_artifact(run_dir, 0)

    def test_read_names_missing_prerequisite(self):
        _, run_dir = create_run(self.runs, SESSION)
        with self.assertRaisesRegex(ArtifactError, "00_capture.json"):
            read_artifact(run_dir, 0)

    def test_unknown_stage(self):
        run_dir = self.new_run()
        for stage in (-1, 9, "0"):
            with self.subTest(stage=stage), self.assertRaisesRegex(ArtifactError, "unknown stage"):
                read_artifact(run_dir, stage)

    def test_invalidate_from_deletes_this_and_later_stages(self):
        run_dir = self.new_run()
        for stage in (1, 2, 4):  # stage 3 is absent on purpose
            (run_dir / STAGE_FILES[stage]).write_text("{}")
        (run_dir / "run_log.jsonl").write_text("")
        deleted = invalidate_from(run_dir, 2)
        self.assertEqual(deleted, [run_dir / STAGE_FILES[2], run_dir / STAGE_FILES[4]])
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()),
                         ["00_capture.json", "01_raw_evidence.json", "run_log.jsonl"])
        self.assertEqual(invalidate_from(run_dir, 2), [])
        with self.assertRaises(ArtifactError):
            invalidate_from(run_dir, 7)


class ResolveRunDirTests(ArtifactTestCase):
    def setUp(self):
        super().setUp()
        _, self.run_dir = create_run(self.runs, SESSION)

    def test_accepts_absolute_and_cwd_relative_paths(self):
        self.assertEqual(resolve_run_dir(self.runs, self.run_dir), self.run_dir)
        with contextlib.chdir(self.tmp):
            self.assertEqual(resolve_run_dir(Path("runs"), f"runs/{self.run_dir.name}"), self.run_dir)
            self.assertEqual(resolve_run_dir(Path("runs"), f"./runs/{self.run_dir.name}/"), self.run_dir)

    def test_rejects_dotdot_escape(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        for path in (self.runs / self.run_dir.name / ".." / ".." / "outside", self.runs / "..", self.runs):
            with self.subTest(path=path), self.assertRaises(ArtifactError):
                resolve_run_dir(self.runs, path)

    def test_rejects_sibling_folder_outside_runs_dir(self):
        sibling = self.tmp / "runs-other" / self.run_dir.name
        sibling.mkdir(parents=True)
        with self.assertRaises(ArtifactError):
            resolve_run_dir(self.runs, sibling)

    def test_rejects_nested_missing_and_file_paths(self):
        nested = self.run_dir / "cache"
        nested.mkdir()
        (self.runs / "file.txt").write_text("x")
        for path in (nested, self.runs / "missing", self.runs / "file.txt"):
            with self.subTest(path=path), self.assertRaises(ArtifactError):
                resolve_run_dir(self.runs, path)

    def test_rejects_symlinks(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.symlink(self.runs / "to-outside", outside)
        self.symlink(self.runs / "to-run", self.run_dir)
        for link in ("to-outside", "to-run"):
            with self.subTest(link=link), self.assertRaisesRegex(ArtifactError, "symlink"):
                resolve_run_dir(self.runs, self.runs / link)


class LogTests(ArtifactTestCase):
    SECRETS = (
        "SERPKEY123", "PLAINKEY456", "tok.en-789", "sk-ant-api03-abcdefghijklmnop", "sk-or-v1-0123456789abcdef",
        "AKIAABCDEFGHIJKLMNOP", "deadbeefsig", "SESSIONTOKEN", "SIGV2VALUE", "hunter2", "nested-bearer",
        "nestedplain", "engine=yandex_images", "frag",
    )

    def test_log_line_has_timestamp_and_only_given_fields(self):
        _, run_dir = create_run(self.runs, SESSION)
        log_event(run_dir, stage=0, backend="mock", status="ok")
        log_event(run_dir, stage=1, endpoint="/search.json", status=200, latency_ms=12.5, retries=2, code="http_429")
        first, second = [json.loads(line) for line in (run_dir / "run_log.jsonl").read_text().splitlines()]
        self.assertEqual(set(first), {"ts", "stage", "backend", "status", "retries"})
        self.assertEqual(datetime.fromisoformat(first["ts"]).utcoffset().total_seconds(), 0)
        self.assertEqual(second["latency_ms"], 12.5)
        self.assertEqual(second["retries"], 2)
        self.assertEqual(second["code"], "http_429")

    def test_log_is_sanitized(self):
        _, run_dir = create_run(self.runs, SESSION)
        presigned = ("https://bucket.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                     "&X-Amz-Credential=AKIAABCDEFGHIJKLMNOP%2F20261010&X-Amz-Signature=deadbeefsig"
                     "&X-Amz-Security-Token=SESSIONTOKEN")
        log_event(
            run_dir,
            stage=1,
            backend="serpapi",
            endpoint="https://user:hunter2@serpapi.com/search.json?engine=yandex_images&api_key=SERPKEY123#frag",
            status=500,
            code=(f"upload failed: {presigned}; Authorization: Bearer tok.en-789; api_key=SERPKEY123 "
                  "key=PLAINKEY456 Signature=SIGV2VALUE"),
            model="sk-ant-api03-abcdefghijklmnop",
            usage={
                "input_tokens": 120,
                "output_tokens": 4,
                "meta": {"auth": "Bearer nested-bearer", "keys": ["sk-or-v1-0123456789abcdef"]},
                "api_key": "nestedplain",
            },
        )
        raw = (run_dir / "run_log.jsonl").read_text()
        for secret in self.SECRETS:
            self.assertNotIn(secret, raw)
        event = json.loads(raw)
        self.assertEqual(event["endpoint"], "/search.json")
        self.assertEqual(event["usage"]["input_tokens"], 120)
        self.assertEqual(event["usage"]["output_tokens"], 4)
        self.assertIn("https://bucket.s3.amazonaws.com/k?", event["code"])


class UploadsTests(ArtifactTestCase):
    def test_missing_uploads_is_empty_manifest(self):
        _, run_dir = create_run(self.runs, SESSION)
        self.assertEqual(read_uploads(run_dir), UploadsManifest(run_id=run_dir.name, objects=[]))

    def test_round_trip_is_atomic(self):
        _, run_dir = create_run(self.runs, SESSION)
        manifest = UploadsManifest(run_id=run_dir.name, objects=[
            UploadRecord(bucket="b", key="tmp/3f2a.jpg"),
            UploadRecord(bucket="b", key="tmp/9c1d.jpg", version_id="v1", deleted=True),
        ])
        write_uploads(run_dir, manifest)
        self.assertEqual(read_uploads(run_dir), manifest)
        self.assertEqual([p.name for p in run_dir.iterdir()], ["uploads.json"])

    def test_rejects_mismatch_malformed_and_urls(self):
        _, run_dir = create_run(self.runs, SESSION)
        with self.assertRaises(ArtifactError):
            write_uploads(run_dir, UploadsManifest(run_id="other-run"))
        signed = "https://b.s3.amazonaws.com/k?X-Amz-Signature=abc"
        with self.assertRaises(ArtifactError):
            write_uploads(run_dir, UploadsManifest(run_id=run_dir.name, objects=[UploadRecord(bucket="b", key=signed)]))
        (run_dir / "uploads.json").write_text("[")
        with self.assertRaises(ArtifactError):
            read_uploads(run_dir)


class PurgeTests(ArtifactTestCase):
    def populate(self, run_dir: Path) -> None:
        (run_dir / "01_raw_evidence.json").write_text("{}")
        log_event(run_dir, stage=0, status="ok")
        cache = run_dir / "cache" / "serpapi"
        cache.mkdir(parents=True)
        (cache / "abc.json").write_text("{}")

    def add_uploads(self, run_dir: Path, *keys: str) -> list[UploadRecord]:
        records = [UploadRecord(bucket="bucket", key=key) for key in keys]
        write_uploads(run_dir, UploadsManifest(run_id=run_dir.name, objects=records))
        return records

    def test_plain_purge_deletes_run_and_retains_capture(self):
        capture = self.tmp / "captures" / SESSION
        capture.mkdir(parents=True)
        run_dir = self.new_run(capture)
        self.populate(run_dir)
        report = purge_run(self.runs, run_dir)
        self.assertTrue(report.run_deleted)
        self.assertFalse(run_dir.exists())
        self.assertEqual(report.remote_failures, [])
        self.assertEqual(report.capture_dir, capture)
        self.assertFalse(report.capture_deleted)
        self.assertTrue(capture.is_dir())
        self.assertTrue(any("retained" in message and str(capture) in message for message in report.messages))

    def test_purge_rejects_paths_outside_runs_and_symlinks(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        self.runs.mkdir()
        self.symlink(self.runs / "link", outside)
        for path in (outside, self.runs / "link", self.runs / ".." / "outside"):
            with self.subTest(path=path), self.assertRaises(ArtifactError):
                purge_run(self.runs, path)
        self.assertTrue((outside / "keep.txt").exists())

    def test_failed_remote_delete_keeps_minimal_manifest_then_retry_succeeds(self):
        run_dir = self.new_run()
        self.populate(run_dir)
        good, bad = self.add_uploads(run_dir, "tmp/good.jpg", "tmp/bad.jpg")
        calls = []

        def flaky(record):
            calls.append(record.key)
            if record.key == bad.key:
                raise RuntimeError("AccessDenied for https://bucket.s3.amazonaws.com/x?X-Amz-Signature=leaked")

        report = purge_run(self.runs, run_dir, delete_remote=flaky)
        self.assertFalse(report.run_deleted)
        self.assertEqual(calls, [good.key, bad.key])
        self.assertEqual([record.key for record in report.remote_failures], [bad.key])
        self.assertEqual([p.name for p in run_dir.iterdir()], ["uploads.json"])
        self.assertEqual(read_uploads(run_dir).objects, [UploadRecord(bucket="bucket", key=bad.key)])
        self.assertTrue(any("purge" in message for message in report.messages))
        self.assertFalse(any("leaked" in message for message in report.messages))

        calls.clear()
        retry = purge_run(self.runs, run_dir, delete_remote=lambda record: calls.append(record.key))
        self.assertEqual(calls, [bad.key])
        self.assertTrue(retry.run_deleted)
        self.assertEqual(retry.remote_failures, [])
        self.assertFalse(run_dir.exists())

    def test_no_delete_client_counts_as_failure(self):
        run_dir = self.new_run()
        self.add_uploads(run_dir, "tmp/a.jpg")
        report = purge_run(self.runs, run_dir)
        self.assertFalse(report.run_deleted)
        self.assertEqual(len(report.remote_failures), 1)
        self.assertEqual([p.name for p in run_dir.iterdir()], ["uploads.json"])

    def test_already_deleted_uploads_are_skipped(self):
        run_dir = self.new_run()
        write_uploads(run_dir, UploadsManifest(run_id=run_dir.name, objects=[
            UploadRecord(bucket="bucket", key="tmp/a.jpg", deleted=True)]))
        report = purge_run(self.runs, run_dir)
        self.assertTrue(report.run_deleted)

    def test_corrupted_capture_artifact_does_not_block_purge(self):
        run_dir = self.new_run()
        self.populate(run_dir)
        (run_dir / "00_capture.json").write_text("{corrupt")
        report = purge_run(self.runs, run_dir, include_capture=True, captures_dir=self.tmp / "captures")
        self.assertTrue(report.run_deleted)
        self.assertIsNone(report.capture_dir)
        self.assertFalse(report.capture_deleted)
        self.assertFalse(run_dir.exists())

    def test_include_capture_deletes_registered_capture(self):
        captures = self.tmp / "captures"
        capture = captures / SESSION
        capture.mkdir(parents=True)
        (capture / "img-1.jpg").write_bytes(b"\xff\xd8")
        run_dir = self.new_run(capture)
        report = purge_run(self.runs, run_dir, include_capture=True, captures_dir=captures)
        self.assertTrue(report.run_deleted)
        self.assertTrue(report.capture_deleted)
        self.assertFalse(capture.exists())
        self.assertTrue(captures.is_dir())

    def assert_capture_refused(self, capture: Path, captures_dir: Path) -> None:
        (capture / "img-1.jpg").write_bytes(b"\xff\xd8")
        run_dir = self.new_run(capture)
        report = purge_run(self.runs, run_dir, include_capture=True, captures_dir=captures_dir)
        self.assertTrue(report.run_deleted)
        self.assertFalse(report.capture_deleted)
        self.assertTrue((capture / "img-1.jpg").exists())
        self.assertTrue(any("retained" in message for message in report.messages))

    def test_include_capture_refuses_folder_outside_captures_dir(self):
        (self.tmp / "captures").mkdir()
        capture = self.tmp / "elsewhere" / SESSION
        capture.mkdir(parents=True)
        self.assert_capture_refused(capture, self.tmp / "captures")

    def test_include_capture_refuses_nested_folder_and_captures_root(self):
        captures = self.tmp / "captures"
        nested = captures / "group" / SESSION
        nested.mkdir(parents=True)
        self.assert_capture_refused(nested, captures)
        self.assert_capture_refused(captures, captures)

    def test_include_capture_refuses_symlinked_capture(self):
        captures = self.tmp / "captures"
        captures.mkdir()
        real = self.tmp / "real" / SESSION
        real.mkdir(parents=True)
        self.symlink(captures / SESSION, real)
        self.assert_capture_refused(captures / SESSION, captures)
        self.assertTrue((captures / SESSION).is_symlink())

    def test_include_capture_refuses_test_fixtures(self):
        # A temp copy of the tests/fixtures layout; the repo's real fixtures are never touched.
        captures = self.tmp / "tests" / "fixtures" / "captures"
        capture = captures / "alex"
        capture.mkdir(parents=True)
        self.assert_capture_refused(capture, captures)

    def test_include_capture_without_captures_dir_retains(self):
        capture = self.tmp / "captures" / SESSION
        capture.mkdir(parents=True)
        self.assert_capture_refused(capture, None)

    def test_symlink_inside_run_is_removed_without_touching_target(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        outside_file = self.tmp / "outside.txt"
        outside_file.write_text("keep")
        run_dir = self.new_run()
        self.populate(run_dir)
        self.symlink(run_dir / "dir-link", outside)
        self.symlink(run_dir / "cache" / "file-link", outside_file)
        report = purge_run(self.runs, run_dir)
        self.assertTrue(report.run_deleted)
        self.assertFalse(run_dir.exists())
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertEqual(outside_file.read_text(), "keep")

    def test_unreadable_uploads_manifest_is_kept(self):
        run_dir = self.new_run()
        self.populate(run_dir)
        (run_dir / "uploads.json").write_text("{broken")
        report = purge_run(self.runs, run_dir, delete_remote=lambda record: None)
        self.assertFalse(report.run_deleted)
        self.assertEqual([p.name for p in run_dir.iterdir()], ["uploads.json"])
        self.assertEqual((run_dir / "uploads.json").read_text(), "{broken")
