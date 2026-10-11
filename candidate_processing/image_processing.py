"""Worker-side face crops and provisional quality flags for Stage A.

Call only after bounded decode, inside the worker. Direct calls in tests use
synthetic images and fake detectors. No face is associated with the subject.
"""

import base64
from importlib.metadata import version
from io import BytesIO
import math

import cv2
import numpy as np
from PIL import ImageOps

from face_landmarker import create_face_landmarker
from photo_checkpoint import analyze_bgr

INFERENCE_SIDE = 640
CROP_SIDE = 256
CROP_PADDING = 0.15
BLUR_VARIANCE_THRESHOLD = 50.0
DARK_MEAN_THRESHOLD = 40.0
DARK_PIXEL_THRESHOLD = 30
DARK_FRACTION_THRESHOLD = 0.60
EDGE_MARGIN = 0.01


def analyze_rgb(rgb, mime: str, byte_length: int, detector=None) -> dict:
    cv2.setNumThreads(1)
    width, height = rgb.size
    bgr = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
    scale = min(1.0, INFERENCE_SIDE / max(width, height))
    inference = cv2.resize(bgr, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(inference, cv2.COLOR_BGR2GRAY)
    metrics = {
        "laplacianVariance": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "meanLuminance": float(gray.mean()),
        "darkPixelFraction": float((gray < DARK_PIXEL_THRESHOLD).mean()),
    }
    concerns = []
    if metrics["laplacianVariance"] < BLUR_VARIANCE_THRESHOLD:
        concerns.append("possible_blur")
    if metrics["meanLuminance"] < DARK_MEAN_THRESHOLD or metrics["darkPixelFraction"] > DARK_FRACTION_THRESHOLD:
        concerns.append("underexposed")
    flags = ["quality_thresholds_uncalibrated", *concerns]
    analysis = {
        "status": "ok", "valid": True, "mimeType": mime, "width": width, "height": height,
        "byteLength": byte_length, "faceCount": None, "maxFaces": 2, "atDetectorLimit": False,
        "quality": "poor" if concerns else "unknown", "qualityMetrics": metrics, "flags": flags,
        "detectorVersion": f"mediapipe-{version('mediapipe')}",
    }
    try:
        if detector is None:
            with create_face_landmarker() as owned:
                detected = analyze_bgr(inference, owned)
        else:
            detected = analyze_bgr(inference, detector)
        faces = detected.face_landmarks
        if not 0 <= len(faces) <= 2:
            raise ValueError("Unexpected detector face count")
    except Exception as error:
        analysis["status"] = "error"
        return {"analysis": analysis, "crops": [], "errors": [{
            "stage": "image", "code": "detector_unavailable" if isinstance(error, FileNotFoundError) else "detection_failed",
            "message": "Face detection could not be completed.", "retryable": not isinstance(error, FileNotFoundError),
        }]}

    analysis.update(faceCount=len(faces), atDetectorLimit=len(faces) == 2)
    if not faces:
        flags.append("no_face_detected")
    if len(faces) > 1:
        flags.extend(["multiple_faces", "detector_capacity_reached"])
    crops, errors = [], []
    for index, points in enumerate(faces):
        try:
            if not points or not all(math.isfinite(p.x) and math.isfinite(p.y) for p in points):
                flags.append("invalid_landmarks")
                continue
            left, right = min(p.x for p in points), max(p.x for p in points)
            top, bottom = min(p.y for p in points), max(p.y for p in points)
            if left < EDGE_MARGIN or top < EDGE_MARGIN or right > 1 - EDGE_MARGIN or bottom > 1 - EDGE_MARGIN:
                flags.append("possibly_clipped_face")
                analysis["quality"] = "poor"
            dx, dy = (right - left) * CROP_PADDING, (bottom - top) * CROP_PADDING
            box = [max(0, math.floor((left - dx) * width)), max(0, math.floor((top - dy) * height)),
                   min(width, math.ceil((right + dx) * width)), min(height, math.ceil((bottom + dy) * height))]
            if box[2] <= box[0] or box[3] <= box[1]:
                flags.append("invalid_face_box")
                continue
            with rgb.crop(box) as crop, ImageOps.pad(crop, (CROP_SIDE, CROP_SIDE), color="white") as normalized:
                output = BytesIO()
                normalized.save(output, format="JPEG", quality=90)
                crops.append({"faceIndex": index, "box": box, "jpeg": base64.b64encode(output.getvalue()).decode("ascii")})
        except Exception:
            flags.append("crop_failed")
            errors.append({"stage": "image", "code": "crop_failed", "message": "A detected face crop could not be prepared.", "retryable": False})
    analysis["flags"] = list(dict.fromkeys(flags))
    return {"analysis": analysis, "crops": crops, "errors": errors}
