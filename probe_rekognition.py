"""Try Rekognition RecognizeCelebrities on one frame (dev tool, prototype).

    python probe_rekognition.py --consent --image path\\to\\frame.jpg
    python probe_rekognition.py --consent --camera          # grab one webcam frame
    python probe_rekognition.py --consent --camera --save   # also save the frame + boxes

Each run makes ONE paid AWS call (about $0.001). --consent is required because the
face is sent to AWS: only use your own face, a teammate who said yes, or a photo of
a celebrity (for example on a phone held up to the camera).

Region: --region, else AWS_REGION from the environment or .env, else your `aws configure` region.
Credentials: the normal AWS chain (aws configure / AWS_PROFILE), never .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from pipeline.rekognition import DEFAULT_MIN_CONFIDENCE, make_client, recognize

REPO_ROOT = Path(__file__).resolve().parent


def region_from_env(cli_region: str | None) -> str | None:
    """--region, else AWS_REGION (env or .env), else None = the `aws configure` region."""
    if cli_region:
        return cli_region
    if os.environ.get("AWS_REGION"):
        return os.environ["AWS_REGION"]
    try:
        from dotenv import dotenv_values

        value = dotenv_values(REPO_ROOT / ".env").get("AWS_REGION")
        if value:
            return value
    except ImportError:
        pass
    return None


def grab_camera_frame(index: int) -> bytes:
    try:
        import cv2
    except ImportError:
        sys.exit("--camera needs OpenCV, which isn't installed in this venv.\n"
                 "Run: pip install -r requirements.txt   (or: pip install opencv-python)\n"
                 "Or skip the camera: --image path\\to\\photo.jpg")

    camera = cv2.VideoCapture(index)
    if not camera.isOpened():
        sys.exit(f"Can't open camera {index}. Close other apps using it, or try --camera-index 1.")
    try:
        for _ in range(10):  # first frames are often dark while the camera adjusts
            camera.read()
            time.sleep(0.05)
        ok, frame = camera.read()
    finally:
        camera.release()
    if not ok:
        sys.exit("The camera opened but gave no frame.")
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        sys.exit("Couldn't encode the frame as JPEG.")
    return encoded.tobytes()


def save_annotated(image: bytes, result, out_dir: Path) -> Path:
    import io

    from PIL import Image, ImageDraw

    picture = Image.open(io.BytesIO(image)).convert("RGB")
    width, height = picture.size
    draw = ImageDraw.Draw(picture)

    def rect(box):
        left, top = box.get("Left", 0) * width, box.get("Top", 0) * height
        return [left, top, left + box.get("Width", 0) * width, top + box.get("Height", 0) * height]

    for celeb in result.celebrities:
        r = rect(celeb.box)
        draw.rectangle(r, outline=(0, 200, 0), width=3)
        draw.text((r[0] + 4, r[1] + 4), f"{celeb.name} {celeb.match_confidence:.1f}", fill=(0, 255, 0))
    for face in result.unrecognized:
        draw.rectangle(rect(face["box"]), outline=(220, 0, 0), width=3)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"frame_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    picture.save(path, quality=90)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="a JPEG or PNG file")
    source.add_argument("--camera", action="store_true", help="grab one frame from the webcam")
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--consent", action="store_true", help="the person in the frame agreed to send it to AWS")
    parser.add_argument("--region")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--save", action="store_true", help="save the frame with face boxes under runs/rekognition/")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not args.consent:
        sys.exit("Refusing: add --consent to confirm the person in the frame agreed to send it to AWS.")

    if args.image and not args.image.is_file():
        sys.exit(f"No file at {args.image}. Give the full path to a real JPEG or PNG (tip: drag the file into the terminal).")
    image = args.image.read_bytes() if args.image else grab_camera_frame(args.camera_index)
    region = region_from_env(args.region)

    try:
        started = time.monotonic()
        client = make_client(region)
        region = client.meta.region_name
        result = recognize(image, client)
        took = (time.monotonic() - started) * 1000
    except Exception as exc:  # show the AWS error code, not a stack trace
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        sys.exit(f"Rekognition call failed ({code or type(exc).__name__}): {exc}")

    if args.json:
        print(json.dumps({"region": region, "celebrities": [asdict(c) for c in result.celebrities],
                          "unrecognized": result.unrecognized, "image_size": result.image_size}, indent=2))
    else:
        print(f"region {region}, image {result.image_size[0]}x{result.image_size[1]}, {took:.0f} ms")
        print(f"celebrities: {len(result.celebrities)}   unrecognized faces: {len(result.unrecognized)}")
        for celeb in result.celebrities:
            mark = "*" if celeb.match_confidence >= args.min_confidence else " "
            print(f"  {mark} {celeb.match_confidence:5.1f}  {celeb.name}  (id {celeb.celebrity_id})")
            for url in celeb.urls:
                print(f"           {url}")
        best = result.best(args.min_confidence)
        print(f"\nidentity: {best.name if best else 'none'}"
              + ("" if best else f"  (no match at or above {args.min_confidence:.0f})"))
        if not result.celebrities and result.unrecognized:
            print("note: a face was found but it is not a known celebrity. "
                  "RecognizeCelebrities never returns lookalikes.")

    if args.save:
        print(f"saved: {save_annotated(image, result, REPO_ROOT / 'runs' / 'rekognition')}")


if __name__ == "__main__":
    main()
