"""Verify the installed OpenCV package using synthetic images, without a camera."""

from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np


class OpenCVTests(unittest.TestCase):
    def test_bgr_to_rgb_converts_channels_without_changing_input(self):
        bgr = np.array([[[0, 0, 255], [0, 255, 0], [255, 0, 0]]], dtype=np.uint8)
        original = bgr.copy()
        expected_rgb = np.array(
            [[[255, 0, 0], [0, 255, 0], [0, 0, 255]]], dtype=np.uint8
        )

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        self.assertEqual(rgb.dtype, np.uint8)
        np.testing.assert_array_equal(rgb, expected_rgb)
        np.testing.assert_array_equal(bgr, original)

    def test_jpeg_can_be_saved_and_read_back(self):
        bgr = np.full((32, 48, 3), (0, 0, 255), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "snapshot.jpg"
            self.assertTrue(cv2.imwrite(str(image_path), bgr), "JPEG write failed")
            self.assertTrue(image_path.is_file())
            restored = cv2.imread(str(image_path), cv2.IMREAD_COLOR)

        self.assertIsNotNone(restored, "OpenCV could not read the saved JPEG")
        self.assertEqual(restored.shape, bgr.shape)
        self.assertEqual(restored.dtype, bgr.dtype)
        # JPEG is lossy, so allow small rounding differences in this solid color.
        np.testing.assert_allclose(restored, bgr, atol=2, rtol=0)


if __name__ == "__main__":
    unittest.main()
