"""Capture bundle loading and the consent gate (spec 0.4 and 0.6).

Bundles are built in temporary folders with tiny synthetic images; nothing is
written into tests/fixtures/.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from mock_internet.seed import build_world
from pipeline import capture
from pipeline.capture import MOCK_FIXTURES, CaptureError, ConsentError, load_capture, recheck_consent
from pipeline.contracts import CaptureArtifact
from tests.support import FIXTURE_CAPTURES, OfflineTestCase

BUNDLES = {
    "alex": [("img-1", "front", "alex-front"), ("img-2", "left", "alex-left"), ("img-3", "right", "alex-right")],
    "sam": [("img-1", "front", "sam-front")],
    "casey": [("img-1", "front", "casey-front")],
    "unknown": [("img-1", "front", "unknown")],
}


def fixture_manifest(name="alex"):
    return json.loads((FIXTURE_CAPTURES / name / "manifest.json").read_text())


class CaptureCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.folder = self.root / "capture"
        self.folder.mkdir()

    def write_manifest(self, data):
        (self.folder / "manifest.json").write_text(json.dumps(data))

    def live_manifest(self, *files, **consent):
        data = fixture_manifest()
        data["consent"].update(consent)
        data["images"] = [{"image_id": f"img-{n}", "file": file, "angle": "front"}
                          for n, file in enumerate(files, 1)]
        return data

    def write_image(self, path, fmt="PNG", size=(32, 24)):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (200, 120, 40)).save(path, fmt)
        return path

    def symlink(self, link, target, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f"symlinks unavailable here: {error}")


class FixtureBundleTests(CaptureCase):
    def test_fixtures_load_in_mock_mode_and_with_models(self):
        for name, expected in BUNDLES.items():
            for models in (False, True):
                with self.subTest(name=name, models=models):
                    loaded = load_capture(FIXTURE_CAPTURES / name, models=models)
                    self.assertEqual(loaded.capture_dir, (FIXTURE_CAPTURES / name).resolve())
                    self.assertEqual(loaded.warnings, [])
                    images = [(i.image_id, i.angle, i.mock_fixture) for i in loaded.bundle.images]
                    self.assertEqual(images, expected)

    def test_fixtures_are_fictional_consenting_and_file_free(self):
        for name in BUNDLES:
            with self.subTest(name=name):
                folder = FIXTURE_CAPTURES / name
                self.assertEqual([path.name for path in folder.iterdir()], ["manifest.json"])
                bundle = load_capture(folder).bundle
                self.assertEqual(bundle.session_id, f"mock-{name}")
                self.assertTrue(bundle.consent.confirmed and bundle.consent.external_processing)
                self.assertIn("(fictional)", bundle.consent.participant)
                self.assertTrue(all(image.file is None for image in bundle.images))

    def test_mock_fixtures_match_seed(self):
        self.assertEqual(MOCK_FIXTURES, frozenset(build_world()[2]))

    def test_mock_mode_never_opens_image_files(self):
        with patch.object(capture.Image, "open") as opened:
            load_capture(FIXTURE_CAPTURES / "alex", models=True)
        opened.assert_not_called()


class ConsentTests(CaptureCase):
    def test_confirmed_false_is_refused(self):
        data = fixture_manifest()
        data["consent"]["confirmed"] = False
        self.write_manifest(data)
        with self.assertRaisesRegex(ConsentError, "confirmed"):
            load_capture(self.folder)

    def test_missing_consent_block_is_refused(self):
        data = fixture_manifest()
        del data["consent"]
        self.write_manifest(data)
        with self.assertRaisesRegex(ConsentError, "no consent block"):
            load_capture(self.folder)

    def test_non_boolean_confirmed_is_refused(self):
        for value in ("true", 1, "yes", None):
            with self.subTest(value=value):
                data = fixture_manifest()
                data["consent"]["confirmed"] = value
                self.write_manifest(data)
                with self.assertRaises(ConsentError):
                    load_capture(self.folder)

    def test_invalid_consent_fields_are_refused(self):
        data = fixture_manifest()
        data["consent"]["confirmed_at"] = "2026-10-10T14:01:50"  # no timezone
        self.write_manifest(data)
        with self.assertRaisesRegex(ConsentError, "consent.confirmed_at"):
            load_capture(self.folder)

    def test_external_calls_need_external_consent(self):
        for external in (False, "true", None):
            data = fixture_manifest()
            if external is None:
                del data["consent"]["external_processing"]
            else:
                data["consent"]["external_processing"] = external
            self.write_manifest(data)
            for flags in ({"models": True}, {"live": True}, {"live": True, "models": True}):
                with self.subTest(external=external, flags=flags):
                    with self.assertRaisesRegex(ConsentError, "external_processing"):
                        load_capture(self.folder, **flags)

    def test_mock_mode_without_models_needs_no_external_consent(self):
        data = fixture_manifest()
        data["consent"]["external_processing"] = False
        self.write_manifest(data)
        self.assertFalse(load_capture(self.folder).bundle.consent.external_processing)

    def test_consent_is_checked_before_images_are_read(self):
        (self.folder / "img-1.jpg").write_bytes(b"not an image")
        revoked = self.live_manifest("img-1.jpg", confirmed=False)
        no_external = self.live_manifest("img-1.jpg", external_processing=False)
        for data in (revoked, no_external):
            self.write_manifest(data)
            with self.subTest(consent=data["consent"]):
                with patch.object(capture.Image, "open") as opened:
                    with self.assertRaises(ConsentError):
                        load_capture(self.folder, live=True)
                opened.assert_not_called()


class ManifestTests(CaptureCase):
    def assert_refused(self, data, pattern, **flags):
        self.write_manifest(data)
        with self.assertRaisesRegex(CaptureError, pattern) as caught:
            load_capture(self.folder, **flags)
        self.assertNotIsInstance(caught.exception, ConsentError)
        return str(caught.exception)

    def test_missing_folder_and_manifest(self):
        with self.assertRaisesRegex(CaptureError, "not found"):
            load_capture(self.root / "nowhere")
        with self.assertRaisesRegex(CaptureError, "no manifest.json"):
            load_capture(self.folder)

    def test_symlinked_manifest_is_rejected(self):
        real = self.root / "elsewhere.json"
        real.write_text(json.dumps(fixture_manifest()))
        self.symlink(self.folder / "manifest.json", real)
        with self.assertRaisesRegex(CaptureError, "regular file"):
            load_capture(self.folder)

    def test_malformed_json_is_a_readable_capture_error(self):
        for text, pattern in (("{not json", "not valid JSON"), ("[1, 2]", "JSON object")):
            with self.subTest(text=text):
                (self.folder / "manifest.json").write_text(text)
                with self.assertRaisesRegex(CaptureError, pattern):
                    load_capture(self.folder)

    def test_schema_errors_are_readable(self):
        data = fixture_manifest()
        data["images"][1]["angle"] = "sideways"
        message = self.assert_refused(data, r"^invalid manifest: images\.1\.angle: ")
        self.assertNotIn("validation error", message)
        self.assertNotIn("pydantic", message)

    def test_duplicate_image_ids(self):
        data = fixture_manifest()
        data["images"][2]["image_id"] = "img-1"
        self.assert_refused(data, "duplicate image IDs: img-1")

    def test_empty_and_oversized_bundles(self):
        data = fixture_manifest()
        data["images"] = []
        self.assert_refused(data, "images")
        image = fixture_manifest()["images"][0]
        data["images"] = [{**image, "image_id": f"img-{n}"} for n in range(1, 10)]
        self.assert_refused(data, "at most 8")

    def test_unsafe_session_id(self):
        data = fixture_manifest()
        data["session_id"] = "../escape"
        self.assert_refused(data, "session_id")
        data["session_id"] = "s" * 120  # its run_id would exceed 128 characters
        self.assert_refused(data, "session_id")

    def test_unknown_fixture(self):
        data = fixture_manifest()
        data["images"][0]["mock_fixture"] = "alex-back"
        self.assert_refused(data, "unknown mock_fixture 'alex-back'")

    def test_file_only_image_in_mock_mode_needs_live(self):
        self.write_image(self.folder / "img-1.png")
        self.assert_refused(self.live_manifest("img-1.png"), "--live")


class LiveImageTests(CaptureCase):
    def assert_refused(self, file, pattern):
        self.write_manifest(self.live_manifest(file))
        with self.assertRaisesRegex(CaptureError, pattern):
            load_capture(self.folder, live=True)

    def test_mock_fixture_is_rejected_in_live_mode(self):
        with self.assertRaisesRegex(CaptureError, "mock_fixture is for tests only"):
            load_capture(FIXTURE_CAPTURES / "alex", live=True)
        data = self.live_manifest("img-1.png")
        data["images"][0]["mock_fixture"] = "alex-front"
        self.write_image(self.folder / "img-1.png")
        self.write_manifest(data)
        with self.assertRaisesRegex(CaptureError, "mock_fixture"):
            load_capture(self.folder, live=True)

    def test_paths_escaping_the_folder_are_rejected(self):
        outside = self.write_image(self.root / "outside.png")
        self.assert_refused("../outside.png", "relative path inside")
        self.assert_refused(str(outside), "relative path inside")

    def test_symlinks_are_rejected(self):
        outside = self.write_image(self.root / "outside.png")
        self.symlink(self.folder / "link.png", outside)
        self.assert_refused("link.png", "symlink")

        real_dir = self.root / "real"
        self.write_image(real_dir / "img.png")
        self.symlink(self.folder / "sub", real_dir, directory=True)
        self.assert_refused("sub/img.png", "symlink")

        inside = self.write_image(self.folder / "inside.png")
        self.symlink(self.folder / "alias.png", inside)
        self.assert_refused("alias.png", "symlink")

    def test_missing_file_and_non_file(self):
        self.assert_refused("img-1.jpg", "not found")
        (self.folder / "folder.jpg").mkdir()
        self.assert_refused("folder.jpg", "not a regular file")

    def test_oversize_file(self):
        self.write_image(self.folder / "img-1.png")
        with patch.object(capture, "MAX_IMAGE_BYTES", 10):
            self.assert_refused("img-1.png", "limit is 10")

    def test_actual_format_is_checked_not_the_extension(self):
        (self.folder / "text.jpg").write_text("definitely not a JPEG")
        self.assert_refused("text.jpg", "not a valid JPEG or PNG")
        (self.folder / "empty.png").write_bytes(b"")
        self.assert_refused("empty.png", "not a valid JPEG or PNG")
        self.write_image(self.folder / "anim.png", fmt="GIF")
        self.assert_refused("anim.png", "not a valid JPEG or PNG")

    def test_corrupt_png_fails_verify(self):
        good = self.write_image(self.folder / "good.png").read_bytes()
        (self.folder / "bad.png").write_bytes(good[:-20])
        self.assert_refused("bad.png", "not a valid JPEG or PNG")

    def test_truncated_jpeg_fails_full_decode(self):
        good = self.write_image(self.folder / "good.jpg", fmt="JPEG", size=(400, 400)).read_bytes()
        (self.folder / "cut.jpg").write_bytes(good[: len(good) // 2])
        self.assert_refused("cut.jpg", "not a valid JPEG or PNG")

    def test_tiny_png_and_jpeg_load_with_small_image_warnings(self):
        self.write_image(self.folder / "img-1.png")
        self.write_image(self.folder / "nested" / "img-2.jpg", fmt="JPEG")
        self.write_image(self.folder / "img-3.jpg", fmt="PNG")  # PNG named .jpg is still a PNG
        self.write_manifest(self.live_manifest("img-1.png", "nested/img-2.jpg", "img-3.jpg"))
        loaded = load_capture(self.folder, live=True, models=True)
        self.assertEqual([image.file for image in loaded.bundle.images], ["img-1.png", "nested/img-2.jpg", "img-3.jpg"])
        self.assertEqual(len(loaded.warnings), 3)
        self.assertIn("24px", loaded.warnings[0])

    def test_recommended_size_has_no_warning(self):
        self.write_image(self.folder / "img-1.jpg", fmt="JPEG", size=(400, 520))
        self.write_manifest(self.live_manifest("img-1.jpg"))
        self.assertEqual(load_capture(self.folder, live=True).warnings, [])


class RecheckConsentTests(CaptureCase):
    def setUp(self):
        super().setUp()
        self.write_manifest(fixture_manifest())
        loaded = load_capture(self.folder)
        self.artifact = CaptureArtifact(
            run_id=f"{loaded.bundle.session_id}-0123abcd", capture=loaded.bundle,
            capture_dir=str(loaded.capture_dir), backend="mock", scenario="default",
            llm_mode="off", jev_mode="fake",
        )

    def edit(self, change):
        data = fixture_manifest()
        change(data)
        self.write_manifest(data)

    def test_unchanged_manifest_passes(self):
        self.assertEqual(recheck_consent(self.artifact, models=True), self.artifact.capture)

    def test_removed_manifest(self):
        (self.folder / "manifest.json").unlink()
        with self.assertRaisesRegex(ConsentError, "no manifest.json"):
            recheck_consent(self.artifact)

    def test_removed_folder(self):
        (self.folder / "manifest.json").unlink()
        self.folder.rmdir()
        with self.assertRaises(ConsentError):
            recheck_consent(self.artifact)

    def test_malformed_manifest(self):
        (self.folder / "manifest.json").write_text("{")
        with self.assertRaises(ConsentError):
            recheck_consent(self.artifact)

    def test_revoked_consent(self):
        self.edit(lambda m: m["consent"].update(confirmed=False))
        with self.assertRaises(ConsentError):
            recheck_consent(self.artifact)

    def test_revoked_external_consent(self):
        self.edit(lambda m: m["consent"].update(external_processing=False))
        recheck_consent(self.artifact)  # local-only rerun is still allowed
        for flags in ({"models": True}, {"live": True}):
            with self.subTest(flags=flags), self.assertRaisesRegex(ConsentError, "external_processing"):
                recheck_consent(self.artifact, **flags)

    def test_changed_participant(self):
        self.edit(lambda m: m["consent"].update(participant="Sam Rivera (fictional)"))
        with self.assertRaisesRegex(ConsentError, "participant changed"):
            recheck_consent(self.artifact)

    def test_changed_session(self):
        self.edit(lambda m: m.update(session_id="mock-alex-2"))
        with self.assertRaisesRegex(ConsentError, "session changed"):
            recheck_consent(self.artifact)
