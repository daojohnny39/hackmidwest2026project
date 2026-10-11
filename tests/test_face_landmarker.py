"""Check local detector initialization without a camera or personal photos."""

from pathlib import Path
import tempfile
import unittest

import mediapipe as mp
import numpy as np

from face_landmarker import DEFAULT_MODEL_PATH, create_face_landmarker


class FaceLandmarkerTests(unittest.TestCase):
    def test_missing_model_explains_how_to_download_it(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_model = Path(directory) / "missing.task"
            with self.assertRaises(FileNotFoundError) as raised:
                create_face_landmarker(missing_model)

        self.assertIn(str(missing_model.resolve()), str(raised.exception))
        self.assertIn("Step 3 in README.md", str(raised.exception))

    @unittest.skipUnless(DEFAULT_MODEL_PATH.is_file(), "Download the Step 3 model first")
    def test_real_detector_initializes_and_can_be_reused(self):
        blank_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=np.zeros((256, 256, 3), dtype=np.uint8),
        )

        with create_face_landmarker() as landmarker:
            for _ in range(2):
                result = landmarker.detect(blank_image)
                self.assertEqual(len(result.face_landmarks), 0)


if __name__ == "__main__":
    unittest.main()
