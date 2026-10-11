"""Write manifest.json for a folder of photos, so any capture folder works (helper).

    python make_capture.py captures/johnny --participant "Johnny Dao"
    python make_capture.py captures/johnny --participant "Johnny Dao" --yes --force

Takes every .jpg/.jpeg/.png in the folder (at most 6; the pipeline allows 8). The
angle comes from the file name when it contains front, left, right, up or down
(for example "left.jpg" or "img_left_2.png"); anything else is "unknown".
Asks you to confirm the person agreed to have the photos sent to outside services
(AWS and the search engines), unless you pass --yes. An existing manifest.json is
kept unless you pass --force (the old one is saved as manifest.json.bak).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

SUFFIXES = {".jpg", ".jpeg", ".png"}
ANGLES = ("front", "left", "right", "up", "down")
MAX_PHOTOS = 6


def angle_from_name(name: str) -> str:
    words = re.split(r"[^a-z]+", name.lower())
    return next((a for a in ANGLES if a in words), "unknown")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--participant", required=True, help='who is in the photos, e.g. "Johnny Dao"')
    parser.add_argument("--yes", action="store_true", help="the person agreed (skips the question)")
    parser.add_argument("--force", action="store_true", help="replace an existing manifest.json")
    args = parser.parse_args()

    folder = args.folder
    if not folder.is_dir():
        sys.exit(f"No folder at {folder}")
    manifest = folder / "manifest.json"
    if manifest.exists() and not args.force:
        sys.exit(f"{manifest} already exists. Add --force to replace it (a .bak copy is kept).")

    photos = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES):
        try:
            with Image.open(path) as picture:
                picture.verify()
        except Exception:
            print(f"  skipped (not a valid image): {path.name}")
            continue
        photos.append(path)
    if not photos:
        sys.exit(f"No .jpg or .png photos in {folder}")
    if len(photos) > MAX_PHOTOS:
        print(f"  {len(photos)} photos found; using the first {MAX_PHOTOS}")
        photos = photos[:MAX_PHOTOS]

    angles, images = set(), []
    for i, path in enumerate(photos, 1):
        angle = angle_from_name(path.stem)
        if angle != "unknown" and angle in angles:
            angle = "unknown"  # two photos claim the same angle; keep both, label the second unknown
        angles.add(angle)
        images.append({"image_id": f"img-{i}", "file": path.name, "angle": angle})

    if not args.yes:
        answer = input(f"Did {args.participant} agree to have these {len(images)} photo(s) sent to AWS and "
                       f"search engines, and to be searched online? (yes/no): ").strip().lower()
        if answer not in ("y", "yes"):
            sys.exit("No manifest written: consent is required.")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", folder.resolve().name).strip("-") or "capture"
    data = {
        "session_id": session_id,
        "captured_at": now,
        "consent": {"confirmed": True, "participant": args.participant, "confirmed_at": now,
                    "external_processing": True},
        "images": images,
    }
    if manifest.exists():
        shutil.copy2(manifest, folder / "manifest.json.bak")
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {manifest}  (session {session_id})")
    for image in images:
        print(f"  {image['image_id']}: {image['file']}  angle={image['angle']}")
    print(f"\nnext: python narrow_search.py --capture {folder.as_posix()}")


if __name__ == "__main__":
    main()
