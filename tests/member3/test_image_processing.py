"""Stage A face/crop/quality behavior with fakes plus a real-model smoke test."""

import asyncio
import base64
import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
from PIL import Image
from pydantic import ValidationError

from candidate_processing.contracts import CandidateInput
from candidate_processing.image_analysis import ImageProcessingResult, IsolatedImageAnalyzer
from candidate_processing.image_processing import analyze_rgb
from candidate_processing.image_validation import _WorkerFailure
from candidate_processing.storage import AssetNotFound, LocalAssetStore
from face_landmarker import DEFAULT_MODEL_PATH

ROOT = Path(__file__).resolve().parents[2]


def face(left=0.25, top=0.2, right=0.75, bottom=0.8):
    return [SimpleNamespace(x=left, y=top), SimpleNamespace(x=right, y=bottom)]


class FakeDetector:
    def __init__(self, faces=(), error=None):
        self.faces, self.error = faces, error
        self.pixels = None

    def detect(self, image):
        self.pixels = image.numpy_view().copy()
        if self.error:
            raise self.error
        return SimpleNamespace(face_landmarks=self.faces)


def prepare_report(faces=()):
    with Image.new("RGB", (160, 120), "red") as image:
        return analyze_rgb(image, "image/png", 100, FakeDetector(faces))


class FaceProcessingTests(unittest.TestCase):
    def test_zero_one_two_faces_have_explicit_counts_and_capacity(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                report = ImageProcessingResult.model_validate(prepare_report([face()] * count))
                self.assertEqual(report.analysis.status, "ok")
                self.assertEqual(report.analysis.face_count, count)
                self.assertEqual(report.analysis.at_detector_limit, count == 2)
                self.assertEqual(len(report.crops), count)
                for crop in report.crops:
                    with Image.open(BytesIO(base64.b64decode(crop.jpeg))) as image:
                        self.assertEqual(image.size, (256, 256))
                        self.assertEqual(image.format, "JPEG")

    def test_inference_uses_rgb_and_bounded_size_without_changing_source(self):
        detector = FakeDetector([face()])
        with Image.new("RGB", (1280, 960), (255, 0, 0)) as image:
            original = image.tobytes()
            report = analyze_rgb(image, "image/png", 100, detector)
            self.assertEqual(image.tobytes(), original)
        self.assertEqual(detector.pixels.shape, (480, 640, 3))
        self.assertEqual(detector.pixels[0, 0].tolist(), [255, 0, 0])
        self.assertEqual((report["analysis"]["width"], report["analysis"]["height"]), (1280, 960))
        self.assertEqual(report["crops"][0]["box"], [224, 105, 1056, 855])

    def test_boundary_faces_are_flagged_and_crops_clipped(self):
        report = ImageProcessingResult.model_validate(prepare_report([face(-0.05, 0, 1.05, 1)]))
        self.assertIn("possibly_clipped_face", report.analysis.flags)
        self.assertEqual(report.analysis.quality, "poor")
        self.assertEqual(report.crops[0].box, [0, 0, 160, 120])

    def test_invalid_landmarks_do_not_create_invalid_crops(self):
        for points, expected in ((face(float("nan")), "invalid_landmarks"), ([], "invalid_landmarks"),
                                 (face(0.5, 0.5, 0.5, 0.5), "invalid_face_box")):
            with self.subTest(expected=expected):
                report = ImageProcessingResult.model_validate(prepare_report([points]))
                self.assertEqual(report.analysis.face_count, 1)
                self.assertEqual(report.crops, [])
                self.assertIn(expected, report.analysis.flags)

    def test_detection_failure_is_unknown_count_and_preserves_validity(self):
        with Image.new("RGB", (100, 100)) as image:
            report = ImageProcessingResult.model_validate(analyze_rgb(image, "image/png", 100, FakeDetector(error=RuntimeError("private detail"))))
        self.assertTrue(report.analysis.valid)
        self.assertEqual(report.analysis.status, "error")
        self.assertIsNone(report.analysis.face_count)
        self.assertEqual(report.errors[0].code, "detection_failed")
        self.assertNotIn("private", report.errors[0].message)

    def test_missing_model_has_distinct_failure(self):
        with Image.new("RGB", (100, 100)) as image, patch("candidate_processing.image_processing.create_face_landmarker", side_effect=FileNotFoundError()):
            report = ImageProcessingResult.model_validate(analyze_rgb(image, "image/png", 100))
        self.assertEqual(report.errors[0].code, "detector_unavailable")
        self.assertFalse(report.errors[0].retryable)

    def test_blur_and_underexposure_are_review_flags(self):
        with Image.new("RGB", (100, 100), "black") as dark:
            report = ImageProcessingResult.model_validate(analyze_rgb(dark, "image/png", 100, FakeDetector([face()])))
        self.assertEqual(report.analysis.quality, "poor")
        self.assertIn("possible_blur", report.analysis.flags)
        self.assertIn("underexposed", report.analysis.flags)
        self.assertEqual(report.analysis.quality_metrics["meanLuminance"], 0)
        self.assertEqual(len(report.crops), 1)  # Poor quality does not discard a detected face.

    def test_no_quality_concerns_does_not_claim_calibrated_good_quality(self):
        checker = ((np.indices((100, 100)).sum(axis=0) % 2) * 255).astype(np.uint8)
        with Image.fromarray(checker).convert("RGB") as image:
            report = ImageProcessingResult.model_validate(analyze_rgb(image, "image/png", 100, FakeDetector([face()])))
        self.assertEqual(report.analysis.quality, "unknown")
        self.assertEqual(report.analysis.flags, ["quality_thresholds_uncalibrated"])

    def test_crop_encode_failure_retains_detection_and_reason(self):
        with patch("PIL.Image.Image.save", side_effect=OSError("encoder failure")):
            report = ImageProcessingResult.model_validate(prepare_report([face()]))
        self.assertEqual(report.analysis.face_count, 1)
        self.assertEqual(report.crops, [])
        self.assertEqual(report.errors[0].code, "crop_failed")

    def test_worker_cannot_reference_arbitrary_assets_or_invalid_crop_bytes(self):
        original = prepare_report([face()])
        for mutate in (
            lambda report: report["crops"][0].update(jpeg="not-base64"),
            lambda report: report["crops"][0].update(box=[0, 0, 10000, 10000]),
            lambda report: report["analysis"].update(crops=[{"assetId": "existing", "faceIndex": 0, "box": [0, 0, 1, 1]}]),
        ):
            report = copy.deepcopy(original)
            mutate(report)
            with self.assertRaises(ValidationError):
                ImageProcessingResult.model_validate(report)


class CandidateAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = LocalAssetStore(self.directory.name)
        output = BytesIO()
        with Image.new("RGB", (128, 128), "white") as image:
            image.save(output, "PNG")
        self.data = output.getvalue()
        self.asset = self.store.put(self.data, scope="session-a")
        data = json.loads((ROOT / "contracts/examples/member3-input.json").read_text())["candidates"][0]
        data["image"]["assetId"] = self.asset.id
        self.candidate = CandidateInput.model_validate(data)
        self.analyzer = IsolatedImageAnalyzer()

    async def test_candidate_provenance_original_and_crop_scope_are_preserved(self):
        report = ImageProcessingResult.model_validate(prepare_report([face()]))
        with patch.object(self.analyzer, "analyze", new_callable=AsyncMock, return_value=report):
            result = await self.analyzer.analyze_candidate(self.candidate, self.store, scope="session-a")
        self.assertEqual((result.id, result.source_id, result.image_url, result.source_url),
                         (self.candidate.id, self.candidate.source_id, self.candidate.image_url, self.candidate.source_url))
        self.assertEqual(result.asset_id, self.asset.id)
        self.assertEqual(self.store.get(self.asset.id, scope="session-a")[1], self.data)
        crop = result.image_analysis.crops[0]
        self.assertNotEqual(crop.asset_id, result.asset_id)
        _, crop_bytes = self.store.get(crop.asset_id, scope="session-a")
        with Image.open(BytesIO(crop_bytes)) as image:
            self.assertEqual(image.size, (256, 256))
        with self.assertRaises(AssetNotFound):
            self.store.get(crop.asset_id, scope="session-b")
        self.assertTrue(result.review_required)
        self.assertEqual(result.classification.status, "skipped")
        self.assertEqual(result.classification.relevance, "unknown")
        self.assertIn("possible_blur", result.review_reasons)
        self.assertEqual(type(result).model_validate_json(result.model_dump_json(by_alias=True)), result)

    async def test_crop_quota_failure_keeps_source_and_detection(self):
        limited = LocalAssetStore(self.directory.name, max_bytes=len(self.data))
        with patch.object(self.analyzer, "analyze", new_callable=AsyncMock, return_value=ImageProcessingResult.model_validate(prepare_report([face()]))):
            result = await self.analyzer.analyze_candidate(self.candidate, limited, scope="session-a")
        self.assertEqual(result.image_analysis.face_count, 1)
        self.assertEqual(result.image_analysis.crops, [])
        self.assertEqual(result.errors[0].code, "crop_storage_failed")
        self.assertEqual(limited.get(self.asset.id, scope="session-a")[1], self.data)

    async def test_failed_download_is_retained_without_inference(self):
        candidate = CandidateInput.model_validate({**self.candidate.model_dump(), "image": {
            "status": "failed", "error": {"stage": "retrieval", "code": "download_failed", "message": "Download failed."},
        }})
        with patch.object(self.analyzer, "analyze", new_callable=AsyncMock) as analyze:
            result = await self.analyzer.analyze_candidate(candidate, self.store, scope="session-a")
        analyze.assert_not_awaited()
        self.assertEqual(result.id, candidate.id)
        self.assertEqual(result.image_analysis.status, "unavailable")
        self.assertIsNone(result.image_analysis.face_count)
        self.assertEqual(result.errors[0].code, "download_failed")

    async def test_invalid_bytes_and_worker_failure_return_reviewable_results(self):
        for data in (b"", b"not an image"):
            result = await self.analyzer.analyze(data)
            self.assertEqual(result.analysis.status, "invalid")
            self.assertFalse(result.analysis.valid)
        with patch.object(self.analyzer, "_run_worker", new_callable=AsyncMock, side_effect=_WorkerFailure("image_timeout")):
            result = await self.analyzer.analyze(self.data)
        self.assertEqual(result.analysis.status, "error")
        self.assertIsNone(result.analysis.face_count)
        self.assertEqual(result.errors[0].code, "image_timeout")

    async def test_malformed_analysis_worker_output_is_a_failure(self):
        for output in (b'{"analysis": {"status": "ok", "valid": true}}', b'{"analysis": {"status": "error"}, "errors": []}'):
            with self.subTest(output=output), patch.object(self.analyzer, "_run_worker", new_callable=AsyncMock, return_value=output):
                result = await self.analyzer.analyze(self.data)
            self.assertEqual(result.errors[0].code, "invalid_worker_response")

    async def test_cancelling_crop_save_cleans_new_asset(self):
        started, release = threading.Event(), threading.Event()
        saved = []
        original_put = self.store.put

        def slow_put(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test did not release write")
            asset = original_put(*args, **kwargs)
            saved.append(asset)
            return asset

        with patch.object(self.analyzer, "analyze", new_callable=AsyncMock, return_value=ImageProcessingResult.model_validate(prepare_report([face()]))), \
             patch.object(self.store, "put", side_effect=slow_put):
            task = asyncio.create_task(self.analyzer.analyze_candidate(self.candidate, self.store, scope="session-a"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(saved), 1)
        with self.assertRaises(AssetNotFound):
            self.store.get(saved[0].id, scope="session-a")

    @unittest.skipUnless(DEFAULT_MODEL_PATH.is_file(), "Download the Face Landmarker model first")
    async def test_real_model_blank_image_and_recovery_after_invalid_image(self):
        failed = await self.analyzer.analyze(b"not an image")
        self.assertEqual(failed.analysis.status, "invalid")
        result = await self.analyzer.analyze_candidate(self.candidate, self.store, scope="session-a")
        self.assertEqual(result.image_analysis.status, "ok", result.errors)
        self.assertTrue(result.image_analysis.valid)
        self.assertEqual(result.image_analysis.face_count, 0)
        self.assertEqual(result.image_analysis.crops, [])
        self.assertIn("no_face_detected", result.review_reasons)
        self.assertEqual(self.store.get(self.asset.id, scope="session-a")[1], self.data)


if __name__ == "__main__":
    unittest.main()
