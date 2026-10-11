"""Build a small, AWS-ready gallery from the big face dataset (run where the data lives).

Standard library only, so it runs on the dataset server without installs:

    python3 build_gallery_subset.py --images ~/dev/images --maps ~/dev/sample_maps \\
        --identity ~/dev/identity_CelebA.txt --people 1000 --out ~/gallery_subset

Why grouping matters: the dataset has many photos per person (about 20). The name
maps give every PHOTO its own random name, so one face would get ~20 names. With
--identity (lines like "000001.jpg 2880": photo, person number) we group photos by
person and give each person ONE name: the name map of their first photo.

Output (copy this folder to the laptop as gallery/, then `face_gallery.py add`):

    gallery_subset/
      names.json             {"p02880": "Jack Cynthia Rogers", ...}
      people.json            per person: source id, name, gallery photo, test photo, all photos
      p02880/000001.png      ONE photo per person goes to AWS (keeps cost ~ $0.001/person)
      _test/p02880.png       a DIFFERENT photo of the same person, never indexed: search
                             with it to check that matching really works

Without --identity every photo is treated as its own person. The script refuses that
unless you pass --no-identity, because results would be misleading.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path


def read_identity(path: Path) -> dict[str, str]:
    """photo stem -> person id. Accepts 'photo.jpg 123' lines; extensions are ignored."""
    mapping = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            mapping[Path(parts[0]).stem] = parts[1]
    if not mapping:
        sys.exit(f"{path} has no 'photo person' lines")
    return mapping


def read_name(maps: Path, stem: str) -> str | None:
    path = maps / f"{stem}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    parts = [data.get("first_name"), data.get("middle_name"), data.get("last_name")]
    name = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return name or None


def find_photo(images: Path, stem: str) -> Path | None:
    for suffix in (".png", ".jpg", ".jpeg", ".PNG", ".JPG"):
        candidate = images / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", type=Path, required=True, help="folder with 000001.png ...")
    parser.add_argument("--maps", type=Path, required=True, help="folder with 000001.json ...")
    parser.add_argument("--identity", type=Path, help="identity file: 'photo person' per line")
    parser.add_argument("--no-identity", action="store_true", help="treat each photo as its own person")
    parser.add_argument("--people", type=int, default=1000)
    parser.add_argument("--min-photos", type=int, default=2, help="skip people with fewer photos (need a test photo)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.identity:
        photo_to_person = read_identity(args.identity)
    elif args.no_identity:
        stems = sorted(p.stem for p in args.maps.glob("*.json"))
        photo_to_person = {stem: stem for stem in stems}
        args.min_photos = 1
    else:
        sys.exit("Need --identity <file> to group photos by person (or --no-identity, not recommended).")

    people: dict[str, list[str]] = {}
    for stem, person in photo_to_person.items():
        people.setdefault(person, []).append(stem)
    candidates = sorted(p for p, stems in people.items() if len(stems) >= args.min_photos)
    random.Random(args.seed).shuffle(candidates)
    print(f"{len(photo_to_person):,} photos, {len(people):,} people, "
          f"{len(candidates):,} with {args.min_photos}+ photos")

    if args.out.exists() and any(args.out.iterdir()):
        sys.exit(f"{args.out} is not empty; pick a new --out folder")
    (args.out / "_test").mkdir(parents=True, exist_ok=True)

    names, records = {}, {}
    for person in candidates:
        if len(records) >= args.people:
            break
        stems = sorted(people[person])
        photos = [(stem, find_photo(args.images, stem)) for stem in stems]
        photos = [(stem, path) for stem, path in photos if path is not None]
        name = read_name(args.maps, stems[0])
        if len(photos) < args.min_photos or not name:
            continue
        pid = f"p{int(person):05d}" if person.isdigit() else f"p_{person}"
        (gallery_stem, gallery_path), *rest = photos
        (args.out / pid).mkdir()
        shutil.copy2(gallery_path, args.out / pid / gallery_path.name)
        test = None
        if rest:
            test_stem, test_path = rest[len(rest) // 2]
            test = f"_test/{pid}{test_path.suffix}"
            shutil.copy2(test_path, args.out / test)
        names[pid] = name
        records[pid] = {"source_id": person, "name": name, "gallery_photo": f"{pid}/{gallery_path.name}",
                        "test_photo": test, "all_photos": [p.name for _, p in photos], "fictional": True}

    (args.out / "names.json").write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")
    (args.out / "people.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(records):,} people to {args.out} (1 gallery photo + 1 test photo each)")


if __name__ == "__main__":
    main()
