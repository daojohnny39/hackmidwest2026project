"""Analyze a local photo and save a landmark overlay plus JSON results."""

import argparse
import json
from pathlib import Path
import tempfile

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import vision

from face_landmarker import DEFAULT_MODEL_PATH, create_face_landmarker


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "checkpoints"


def decode_image(image_bytes: bytes) -> np.ndarray:
    """Decode compressed image bytes into OpenCV's BGR pixel order."""
    if not image_bytes:
        raise ValueError("The photo file is empty.")
    try:
        bgr = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as error:
        raise ValueError("Could not decode the photo. Try a JPEG or PNG file.") from error
    if bgr is None:
        raise ValueError("Could not decode the photo. Try a JPEG or PNG file.")
    return bgr


def analyze_image(
    image_bytes: bytes, landmarker: vision.FaceLandmarker
) -> tuple[np.ndarray, vision.FaceLandmarkerResult]:
    """Prepare an image with OpenCV and analyze it using an existing detector."""
    bgr = decode_image(image_bytes)
    return bgr, analyze_bgr(bgr, landmarker)


def analyze_bgr(
    bgr: np.ndarray, landmarker: vision.FaceLandmarker
) -> vision.FaceLandmarkerResult:
    """Analyze already decoded BGR pixels without changing the source array."""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    return landmarker.detect(image)


def draw_landmarks(
    bgr: np.ndarray, result: vision.FaceLandmarkerResult
) -> np.ndarray:
    """Draw each face's landmark points in a different color on a copy."""
    annotated = bgr.copy()
    height, width = bgr.shape[:2]
    colors = ((0, 255, 0), (255, 200, 0))
    radius = max(1, round(min(height, width) / 700))
    for index, landmarks in enumerate(result.face_landmarks):
        for landmark in landmarks:
            x = round(landmark.x * (width - 1))
            y = round(landmark.y * (height - 1))
            if 0 <= x < width and 0 <= y < height:
                cv2.circle(
                    annotated, (x, y), radius, colors[index % len(colors)],
                    thickness=-1, lineType=cv2.LINE_AA,
                )
    return annotated


def run_checkpoint(
    photo_path: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    model_path: str | Path = DEFAULT_MODEL_PATH,
) -> tuple[Path, dict]:
    """Read one photo, analyze it locally, and save results in a new directory."""
    photo_path = Path(photo_path).expanduser().resolve()
    image_bytes = photo_path.read_bytes()
    with create_face_landmarker(model_path) as landmarker:
        bgr, result = analyze_image(image_bytes, landmarker)

    height, width = bgr.shape[:2]
    report = {
        "image_width": width,
        "image_height": height,
        "face_count": len(result.face_landmarks),
        "max_faces": 2,
        "landmarks_per_face": [len(face) for face in result.face_landmarks],
        "face_landmarks": [
            [{"x": point.x, "y": point.y, "z": point.z} for point in face]
            for face in result.face_landmarks
        ],
        "facial_transformation_matrixes": [
            matrix.tolist() for matrix in result.facial_transformation_matrixes
        ],
    }
    report_json = json.dumps(report, indent=2, allow_nan=False) + "\n"
    saved, encoded = cv2.imencode(".png", draw_landmarks(bgr, result))
    if not saved:
        raise OSError("Could not encode the landmark overlay.")
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="photo-", dir=output_dir))
    (run_dir / "landmarks.png").write_bytes(encoded.tobytes())
    (run_dir / "results.json").write_text(report_json, encoding="utf-8")
    return run_dir, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", type=Path, help="Path to your JPEG or PNG photo")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()
    try:
        run_dir, report = run_checkpoint(args.photo, args.output_dir, args.model)
    except (OSError, ValueError, RuntimeError, cv2.error) as error:
        parser.exit(1, f"Photo checkpoint failed: {error}\n")
    print(f"Image: {report['image_width']} x {report['image_height']}")
    print(f"Faces detected: {report['face_count']} (detector limit: 2)")
    print(f"Landmarks per face: {report['landmarks_per_face']}")
    print(f"Transformation matrices: {len(report['facial_transformation_matrixes'])}")
    print(f"Landmark overlay: {run_dir / 'landmarks.png'}")
    print(f"JSON results: {run_dir / 'results.json'}")
    if report["face_count"] == 0:
        print("No face detected. Try a clear, well-lit photo with a larger face.")
    elif report["face_count"] > 1:
        print("Multiple faces detected. Use a single-person photo for this checkpoint.")


if __name__ == "__main__":
    main()
