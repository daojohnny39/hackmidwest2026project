"""Face -> identity with AWS Rekognition RecognizeCelebrities (prototype).

RecognizeCelebrities only says "this face IS celebrity X" when it is sure. It does
NOT return lookalikes: an ordinary person's face comes back in `unrecognized`, with
no names. Rekognition never searches the open web; the `urls` it returns (IMDb,
Wikidata, ...) are a good seed for the "identity -> presence" step.

Limits (AWS docs): JPEG or PNG, at most 5 MB when sent as bytes, the 64 largest
faces per image. Cost is about $0.001 per image. The caller needs the IAM action
rekognition:RecognizeCelebrities. Credentials come from the normal AWS chain
(aws configure, AWS_PROFILE, env vars), never from this module.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from PIL import Image

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 1920  # big webcam frames are shrunk to stay well under 5 MB
DEFAULT_MIN_CONFIDENCE = 90.0  # MatchConfidence below this is shown but not used


@dataclass
class Celebrity:
    name: str
    celebrity_id: str
    match_confidence: float  # 0-100, from Rekognition
    urls: list[str]
    box: dict  # BoundingBox as image ratios: Width, Height, Left, Top
    face_confidence: float | None = None
    pose: dict | None = None  # Yaw / Pitch / Roll in degrees


@dataclass
class RecognitionResult:
    celebrities: list[Celebrity] = field(default_factory=list)
    unrecognized: list[dict] = field(default_factory=list)  # faces with no celebrity match
    image_size: tuple[int, int] = (0, 0)

    def best(self, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Celebrity | None:
        """The most confident match at or above min_confidence, or None."""
        good = [c for c in self.celebrities if c.match_confidence >= min_confidence]
        return max(good, key=lambda c: c.match_confidence, default=None)


def prepare_image(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """Check the image and return JPEG bytes under the size limit, plus (width, height)."""
    with Image.open(io.BytesIO(data)) as picture:
        picture.load()
        if picture.format in ("JPEG", "PNG") and len(data) <= MAX_BYTES and max(picture.size) <= MAX_SIDE:
            return data, picture.size
        picture = picture.convert("RGB")
        picture.thumbnail((MAX_SIDE, MAX_SIDE))
        for quality in (90, 80, 70, 60):
            buffer = io.BytesIO()
            picture.save(buffer, format="JPEG", quality=quality)
            if buffer.tell() <= MAX_BYTES:
                return buffer.getvalue(), picture.size
    raise ValueError("image is still over 5 MB after shrinking")


def make_client(region: str | None = None):
    """region=None uses the AWS CLI's configured region. Adaptive retries absorb the
    low request rate new and Free-plan accounts get (ThrottlingException)."""
    import boto3  # imported lazily, like the rest of the pipeline's AWS code
    from botocore.config import Config

    config = Config(retries={"mode": "adaptive", "max_attempts": 8})
    return boto3.client("rekognition", region_name=region, config=config)


def recognize(image: bytes, client) -> RecognitionResult:
    """One RecognizeCelebrities call. AWS errors are raised to the caller unchanged."""
    payload, size = prepare_image(image)
    response = client.recognize_celebrities(Image={"Bytes": payload})
    result = RecognitionResult(image_size=size)
    for item in response.get("CelebrityFaces", []):
        face = item.get("Face") or {}
        result.celebrities.append(Celebrity(
            name=item.get("Name", ""),
            celebrity_id=item.get("Id", ""),
            match_confidence=float(item.get("MatchConfidence", 0.0)),
            urls=list(item.get("Urls", [])),
            box=face.get("BoundingBox", {}),
            face_confidence=face.get("Confidence"),
            pose=face.get("Pose"),
        ))
    for face in response.get("UnrecognizedFaces", []):
        result.unrecognized.append({
            "box": face.get("BoundingBox", {}),
            "confidence": face.get("Confidence"),
            "pose": face.get("Pose"),
        })
    result.celebrities.sort(key=lambda c: -c.match_confidence)
    return result


# --- Face collection (IndexFaces / SearchFacesByImage) ---------------------------
# Works on the Free plan, unlike RecognizeCelebrities. We add faces we chose (a
# celebrity gallery, teammates who agreed) and search a frame against them. A low
# threshold returns the closest faces even when no one is a true match: lookalikes.

_EXTERNAL_ID = re.compile(r"[^A-Za-z0-9_.\-:]")


def external_id(person_id: str) -> str:
    """Rekognition's ExternalImageId allows only [A-Za-z0-9_.-:], up to 255 characters."""
    cleaned = _EXTERNAL_ID.sub("_", person_id.strip())[:255]
    if not cleaned:
        raise ValueError("person id is empty")
    return cleaned


@dataclass
class FaceMatch:
    person_id: str  # the ExternalImageId we stored
    similarity: float  # 0-100
    face_id: str


def index_face(client, collection: str, image: bytes, person_id: str) -> str | None:
    """Add the largest face in the image under person_id. Returns its FaceId, or None if no face."""
    payload, _ = prepare_image(image)
    response = client.index_faces(
        CollectionId=collection, Image={"Bytes": payload}, ExternalImageId=external_id(person_id),
        MaxFaces=1, QualityFilter="AUTO", DetectionAttributes=[],
    )
    records = response.get("FaceRecords", [])
    return records[0]["Face"]["FaceId"] if records else None


def search_face(client, collection: str, image: bytes, *, threshold: float = 0.0, max_faces: int = 5) -> list[FaceMatch]:
    """Closest faces to the largest face in the image, best first. [] if the image has no face."""
    payload, _ = prepare_image(image)
    try:
        response = client.search_faces_by_image(
            CollectionId=collection, Image={"Bytes": payload},
            FaceMatchThreshold=threshold, MaxFaces=max_faces, QualityFilter="AUTO",
        )
    except client.exceptions.InvalidParameterException as exc:
        if "no faces" in str(exc).lower():
            return []
        raise
    matches = [
        FaceMatch(person_id=m["Face"].get("ExternalImageId", ""), similarity=float(m["Similarity"]),
                  face_id=m["Face"]["FaceId"])
        for m in response.get("FaceMatches", [])
    ]
    return sorted(matches, key=lambda m: -m.similarity)


def best_per_person(matches: list[FaceMatch]) -> list[FaceMatch]:
    """One row per person (a person may have several photos indexed), best first."""
    best: dict[str, FaceMatch] = {}
    for match in matches:
        if match.person_id not in best or match.similarity > best[match.person_id].similarity:
            best[match.person_id] = match
    return sorted(best.values(), key=lambda m: -m.similarity)
