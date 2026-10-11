"""Verify photo decoding, color order, and the local checkpoint outputs."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import cv2
import numpy as np

from face_landmarker import DEFAULT_MODEL_PATH
from photo_checkpoint import analyze_image, decode_image, draw_landmarks, run_checkpoint


class PhotoCheckpointTests(unittest.TestCase):
    def test_empty_or_invalid_photo_is_rejected(self):
        for content in (b"", b"not an image"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                decode_image(content)

    def test_detector_receives_rgb_and_original_bgr_is_preserved(self):
        bgr = np.array([[[0, 0, 255], [255, 0, 0]]], dtype=np.uint8)
        saved, encoded = cv2.imencode(".png", bgr)
        self.assertTrue(saved)
        expected_result = object()

        class RecordingDetector:
            def detect(self, image):
                self.pixels = image.numpy_view().copy()
                return expected_result

        detector = RecordingDetector()
        decoded, result = analyze_image(encoded.tobytes(), detector)
        np.testing.assert_array_equal(detector.pixels, bgr[:, :, ::-1])
        np.testing.assert_array_equal(decoded, bgr)
        self.assertIs(result, expected_result)

    def test_overlay_uses_image_dimensions_and_preserves_original(self):
        bgr = np.zeros((20, 40, 3), dtype=np.uint8)
        result = SimpleNamespace(
            face_landmarks=[[SimpleNamespace(x=0.5, y=0.5, z=0)]]
        )
        overlay = draw_landmarks(bgr, result)
        self.assertFalse(bgr.any())
        self.assertEqual(overlay[10, 20, 1], 255)
        self.assertEqual(overlay.shape, bgr.shape)

    @unittest.skipUnless(DEFAULT_MODEL_PATH.is_file(), "Download the Step 3 model first")
    def test_blank_photo_saves_valid_outputs_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo with spaces.png"
            blank = np.zeros((128, 192, 3), dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(source), blank))
            original_bytes = source.read_bytes()

            run_dir, report = run_checkpoint(source, Path(directory) / "results")

            self.assertEqual(report["face_count"], 0)
            self.assertEqual(report["image_width"], 192)
            self.assertEqual(report["image_height"], 128)
            self.assertEqual(report["face_landmarks"], [])
            self.assertEqual(report["facial_transformation_matrixes"], [])
            self.assertEqual(json.loads((run_dir / "results.json").read_text()), report)
            np.testing.assert_array_equal(cv2.imread(str(run_dir / "landmarks.png")), blank)
            self.assertEqual(source.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
