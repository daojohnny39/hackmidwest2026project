"""Face gallery on Rekognition Collections: add faces, then search a frame (prototype).

Works on the AWS Free plan (RecognizeCelebrities does not). Folder layout:

    gallery/
      names.json            optional: {"person_a": "Display Name", ...}
      person_a/ 1.jpg 2.jpg one folder per person; the folder name is their ID
      person_b/ 1.jpg
      _test/                folders starting with "_" are skipped (test photos)

Commands (run from the repo root, venv on):

    python face_gallery.py create
    python face_gallery.py add --consent                 # index every new photo in gallery/
    python face_gallery.py search --consent --image photo.jpg
    python face_gallery.py search --consent --camera --save
    python face_gallery.py status
    python face_gallery.py delete --yes                  # delete the AWS collection

Cost: about $0.001 per photo added or searched, plus a tiny monthly storage fee per
face. Only add people who agreed, or public celebrity photos. gallery/ holds face
photos: keep it out of git (it is in .gitignore).

Region: --region, else AWS_REGION (environment or .env), else your `aws configure`
region. The collection lives in ONE region; add and search must use the same one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pipeline.rekognition import best_per_person, index_face, make_client, search_face
from probe_rekognition import grab_camera_frame, region_from_env, save_annotated

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_COLLECTION = "hm-gallery"
DEFAULT_GALLERY = REPO_ROOT / "gallery"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SAME_PERSON = 90.0  # similarity at or above: very likely the same person
LIKELY = 80.0  # at or above: probably the same person; below: a lookalike


def load_index(gallery: Path) -> dict:
    path = gallery / "index.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"collection": None, "region": None, "photos": {}}


def save_index(gallery: Path, index: dict) -> None:
    (gallery / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def display_names(gallery: Path) -> dict[str, str]:
    path = gallery / "names.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def label(similarity: float) -> str:
    if similarity >= SAME_PERSON:
        return "same person"
    if similarity >= LIKELY:
        return "likely same"
    return "lookalike"


def aws_error(exc: Exception) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return f"{code or type(exc).__name__}: {exc}"


def cmd_create(client, args) -> None:
    try:
        client.create_collection(CollectionId=args.collection)
        print(f"created collection {args.collection} in {client.meta.region_name}")
    except client.exceptions.ResourceAlreadyExistsException:
        print(f"collection {args.collection} already exists in {client.meta.region_name}")


def cmd_add(client, args) -> None:
    if not args.consent:
        sys.exit("Refusing: add --consent to confirm everyone in gallery/ agreed (or is a public celebrity photo).")
    gallery: Path = args.gallery
    if not gallery.is_dir():
        sys.exit(f"No gallery folder at {gallery}. Make one folder per person inside it.")
    index = load_index(gallery)
    region = client.meta.region_name
    if index["collection"] and (index["collection"], index["region"]) != (args.collection, region):
        sys.exit(f"gallery/index.json belongs to {index['collection']} in {index['region']}, "
                 f"not {args.collection} in {region}. Use the same --collection and --region.")
    index["collection"], index["region"] = args.collection, region

    # Folders starting with "_" (like _test/) are never indexed.
    todo, skipped = [], 0
    for person_dir in sorted(p for p in gallery.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for photo in sorted(p for p in person_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES):
            key = photo.relative_to(gallery).as_posix()
            if key in index["photos"]:
                skipped += 1
            else:
                todo.append((key, person_dir.name, photo))
    total, verbose = len(todo), len(todo) <= 300
    print(f"{total} photos to add, {skipped} already there, {args.workers} at a time")

    def work(item):
        key, person, photo = item
        face_id = index_face(client, args.collection, photo.read_bytes(), person)
        time.sleep(args.delay)  # new accounts get a low request rate
        return key, person, face_id

    added = no_face = done = 0
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = [pool.submit(work, item) for item in todo]
    try:
        for future in as_completed(futures):
            try:
                key, person, face_id = future.result()
            except Exception as exc:
                pool.shutdown(wait=True, cancel_futures=True)
                save_index(gallery, index)  # keep what worked so far
                sys.exit(f"stopped after {done} photos ({aws_error(exc)}). Run add again to continue.")
            index["photos"][key] = {"person": person, "face_id": face_id}
            done += 1
            if face_id is None:
                no_face += 1
                if verbose:
                    print(f"  no clear face: {key}")
            else:
                added += 1
                if verbose:
                    print(f"  added {key}")
            if done % 50 == 0:
                save_index(gallery, index)
            if not verbose and done % 100 == 0:
                rate = done / (time.monotonic() - started)
                print(f"  {done}/{total}  ({rate:.1f}/s, about {(total - done) / rate / 60:.0f} min left)")
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        save_index(gallery, index)
        sys.exit(f"\nstopped by you after {done} photos; progress saved. Run add again to continue.")
    pool.shutdown()
    save_index(gallery, index)
    print(f"\nadded {added}, already there {skipped}, no face {no_face}  (collection {args.collection}, {region})")


def cmd_search(client, args) -> None:
    if not args.consent:
        sys.exit("Refusing: add --consent to confirm the person in the frame agreed to send it to AWS.")
    if args.image and not args.image.is_file():
        sys.exit(f"No file at {args.image}.")
    image = args.image.read_bytes() if args.image else grab_camera_frame(args.camera_index)
    names = display_names(args.gallery)
    started = time.monotonic()
    try:
        matches = best_per_person(search_face(client, args.collection, image, max_faces=args.top * 3))[: args.top]
    except Exception as exc:
        sys.exit(f"search failed ({aws_error(exc)})")
    print(f"collection {args.collection} in {client.meta.region_name}, {(time.monotonic() - started) * 1000:.0f} ms")
    if not matches:
        print("no face found in the frame, or the collection is empty")
    for match in matches:
        name = names.get(match.person_id, match.person_id)
        shown = f"{name} ({match.person_id})" if name != match.person_id else name
        print(f"  {match.similarity:5.1f}  {label(match.similarity):12} {shown}")
    if args.save:
        print(f"saved: {save_annotated(image, _NoBoxes(), REPO_ROOT / 'runs' / 'rekognition')}")


class _NoBoxes:  # save_annotated wants a result; search gives no boxes worth drawing
    celebrities: list = []
    unrecognized: list = []


def cmd_status(client, args) -> None:
    index = load_index(args.gallery)
    people = {p["person"] for p in index["photos"].values() if p["face_id"]}
    print(f"local index: {len(people)} people, {sum(1 for p in index['photos'].values() if p['face_id'])} faces")
    try:
        info = client.describe_collection(CollectionId=args.collection)
        print(f"AWS collection {args.collection} in {client.meta.region_name}: {info['FaceCount']} faces")
    except client.exceptions.ResourceNotFoundException:
        print(f"no collection {args.collection} in {client.meta.region_name} (run create, or check --region)")


def cmd_delete(client, args) -> None:
    if not args.yes:
        sys.exit("This deletes every face in the collection. Add --yes to confirm.")
    client.delete_collection(CollectionId=args.collection)
    index = load_index(args.gallery)
    index.update(collection=None, region=None, photos={})
    if args.gallery.is_dir():
        save_index(args.gallery, index)
    print(f"deleted collection {args.collection} in {client.meta.region_name} (your photo files were not touched)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("create", "add", "search", "status", "delete"))
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--gallery", type=Path, default=DEFAULT_GALLERY)
    parser.add_argument("--region")
    parser.add_argument("--consent", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds each worker waits between adds (default 0.3)")
    parser.add_argument("--workers", type=int, default=1, help="photos added at the same time (try 4 for big galleries)")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--camera", action="store_true")
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "search" and not (args.image or args.camera):
        sys.exit("search needs --image <file> or --camera")

    client = make_client(region_from_env(args.region))
    if not client.meta.region_name:
        sys.exit("No AWS region set. Run: aws configure set region us-east-1")
    {"create": cmd_create, "add": cmd_add, "search": cmd_search,
     "status": cmd_status, "delete": cmd_delete}[args.command](client, args)


if __name__ == "__main__":
    main()
