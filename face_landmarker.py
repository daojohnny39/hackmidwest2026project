"""Create the local Face Landmarker once and reuse it for individual photos."""

import argparse
from pathlib import Path

from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision


DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_landmarker.task"


def create_face_landmarker(
    model_path: str | Path = DEFAULT_MODEL_PATH,
) -> vision.FaceLandmarker:
    """Return an IMAGE-mode detector; use it in a ``with`` block or call close().

    Create one instance at application startup and reuse it for sequential image
    requests. The caller owns its lifetime and must close it during shutdown.
    """
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_file():
        raise FileNotFoundError(
            f"Face Landmarker model not found: {model_path}. "
            "Download it using Step 3 in README.md."
        )

    options = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(
            model_asset_path=str(model_path),
            delegate=BaseOptions.Delegate.CPU,
        ),
        running_mode=vision.RunningMode.IMAGE,
        num_faces=2,
        output_facial_transformation_matrixes=True,
        output_face_blendshapes=False,
    )
    return vision.FaceLandmarker.create_from_options(options)


def main() -> None:
    """Check that the model initializes, then release the detector."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()
    with create_face_landmarker(args.model):
        print("Face Landmarker initialized: IMAGE mode, up to 2 faces, CPU.")
        print("Facial transformation matrices enabled; blendshapes disabled.")
    print("Face Landmarker closed successfully.")


if __name__ == "__main__":
    main()
