"""Find someone's online presence step by step: photos, then name, then details (prototype).

    python narrow_search.py --capture captures/me
    python narrow_search.py --capture captures/me --name "Jane Doe" --detail UMKC --detail "Kansas City" --no-prompt

What it does (see pipeline/narrow.py for the scoring):
  1. every photo in the capture folder (up to 6 angles) -> Yandex reverse image search
  2. you type the name (or press Enter to skip) -> Google search for it
  3. Rekognition CompareFaces checks page pictures against the front photo
  4. you add one detail at a time (city, school, employer, username) -> searched
     with the name; press Enter on an empty line to finish
  5. the pages ranked "confirmed" or "likely" get risk scores (pipeline/risk.py)

The capture folder needs manifest.json with consent.confirmed and
consent.external_processing set to true (same rules as the pipeline). Only search
yourself or someone who agreed.

Costs: one SerpApi search per photo and per name/detail (stops at --max-searches,
default 12); one CompareFaces per checked picture (about $0.001, at most --face-limit).
Photos go to the S3 bucket under hackmidwest-faces/ as an unsigned public link for a
few seconds (Yandex can't read signed links) and are deleted right after. That needs
the bucket policy that allows public reads of hackmidwest-faces/ only.
.env: SERPAPI_KEY, IMAGE_BUCKET, AWS_REGION. AWS credentials: the normal AWS chain.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

from pipeline.capture import CaptureError, load_capture
from pipeline.narrow import NarrowSession
from pipeline.rekognition import compare_face, make_client
from pipeline.risk import PresenceItem, score_presence
from pipeline.search.base import BackendError
from pipeline.search.serpapi_backend import _public_ip, check_url

REPO_ROOT = Path(__file__).resolve().parent
SERPAPI = "https://serpapi.com/search.json"
ANGLE_ORDER = {"front": 0, "left": 1, "right": 2, "up": 3, "down": 4, "unknown": 5}
MAX_THUMB_BYTES = 5 * 1024 * 1024


class OutOfSearches(Exception):
    pass


class Live:
    """The network side: SerpApi searches, S3 public links, thumbnails, CompareFaces."""

    def __init__(self, env: dict, max_searches: int):
        self.key, self.bucket, self.region = env["SERPAPI_KEY"], env["IMAGE_BUCKET"], env["AWS_REGION"]
        self.max_searches, self.used = max_searches, 0
        self.faces_ok = True
        self.http = httpx.Client(timeout=60, follow_redirects=False)
        import boto3
        from botocore.config import Config

        self.s3 = boto3.client("s3", region_name=self.region, config=Config(signature_version="s3v4"))
        self.rekognition = make_client(self.region)

    def _serpapi(self, params: dict) -> dict:
        if self.used >= self.max_searches:
            raise OutOfSearches()
        self.used += 1
        response = self.http.get(SERPAPI, params={**params, "api_key": self.key})
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        error = str(payload.get("error") or "")
        if response.status_code != 200 or (error and "hasn't returned any results" not in error):
            print(f"    search problem: HTTP {response.status_code} {error[:120]}")
        return payload

    def image_search(self, data: bytes) -> list[dict]:
        suffix = ".png" if data.startswith(b"\x89PNG") else ".jpg"
        key = f"hackmidwest-faces/{secrets.token_hex(16)}{suffix}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                           ContentType="image/png" if suffix == ".png" else "image/jpeg")
        try:
            link = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"
            if self.http.get(link, timeout=20).status_code != 200:
                raise SystemExit("The public photo link doesn't work: check the bucket policy for hackmidwest-faces/.")
            payload = self._serpapi({"engine": "yandex_images", "url": link, "tab": "about"})
        finally:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        return [r for r in payload.get("image_results") or [] if isinstance(r, dict)]

    def text_search(self, query: str) -> list[dict]:
        payload = self._serpapi({"engine": "google", "q": query, "num": 20, "hl": "en"})
        return [r for r in payload.get("organic_results") or [] if isinstance(r, dict)]

    def fetch_picture(self, url: str) -> bytes | None:
        """Public http(s) pictures only, no private addresses, one redirect at most."""
        for _ in range(2):
            try:
                check_url(url)
                host = httpx.URL(url).host
                if not all(_public_ip(info[4][0]) for info in socket.getaddrinfo(host, None)):
                    return None
                response = self.http.get(url, timeout=10)
            except (BackendError, OSError, httpx.HTTPError, ValueError):
                return None
            if response.is_redirect and response.headers.get("location"):
                url = str(response.url.join(response.headers["location"]))
                continue
            if response.status_code != 200 or len(response.content) > MAX_THUMB_BYTES:
                return None
            return response.content if response.headers.get("content-type", "").startswith("image/") else None
        return None

    def face_compare(self, front: bytes, picture_url: str) -> float | None:
        if not self.faces_ok:
            return None
        picture = self.fetch_picture(picture_url)
        if picture is None:
            return None
        try:
            return compare_face(self.rekognition, front, picture)
        except Exception as exc:  # a bad thumbnail or an AWS hiccup skips this one picture
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
            if code in ("AccessDeniedException", "UnrecognizedClientException") and self.faces_ok:
                self.faces_ok = False
                print(f"    face check unavailable ({code}); continuing without it")
            return None


def show(session: NarrowSession, top: int, title: str) -> None:
    ranked = session.ranked()
    counts = {level: sum(1 for r in ranked if r.level == level) for level in ("confirmed", "likely", "possible", "noise")}
    print(f"\n== {title}: {len(ranked)} pages  "
          f"(confirmed {counts['confirmed']}, likely {counts['likely']}, possible {counts['possible']}, noise {counts['noise']})"
          f"  | searches used {session.searches}, face checks {session.face_checks}")
    for i, r in enumerate(ranked[:top], 1):
        c = r.candidate
        print(f"  {i:2}. {r.level.upper():9} {r.score:3}  {c.title[:70] or c.url[:70]}")
        print(f"      {c.url[:100]}")
        if r.reasons:
            print(f"      - {'; '.join(r.reasons)}")


def ask(prompt: str, enabled: bool) -> str:
    if not enabled:
        return ""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def build_jev(mode: str):
    if mode == "off":
        return None
    from pipeline.config import ConfigError, load_settings
    from pipeline.jev import Jev

    try:
        return Jev(load_settings(backend="mock", models=True,
                                 environ={**os.environ, "JEV_MODE": "typesafe", "LLM_MODE": "off"}))
    except ConfigError as exc:
        sys.exit(f"Can't use --jev typesafe: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capture", type=Path, required=True, help="capture folder with manifest.json and photos")
    parser.add_argument("--name", help="skip the name question")
    parser.add_argument("--detail", action="append", default=[], help="a detail to add (repeatable)")
    parser.add_argument("--no-prompt", action="store_true", help="don't ask questions; use only --name/--detail")
    parser.add_argument("--max-searches", type=int, default=12)
    parser.add_argument("--face-limit", type=int, default=10, help="CompareFaces calls at most (0 = off)")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--jev", default="off", choices=("off", "typesafe"), help="risk scoring with real Jev")
    parser.add_argument("--save", action="store_true", help="write the ranking to runs/narrow-<session>-<time>.json")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        loaded = load_capture(args.capture, live=True)  # consent gate + image checks
    except CaptureError as exc:
        hint = ""
        if "no manifest.json" in str(exc):
            hint = (f"\nMake one from the photos in that folder:\n  python make_capture.py "
                    f"{args.capture.as_posix()} --participant \"Full Name\"")
        sys.exit(f"capture problem: {exc}{hint}")
    env = dotenv_values(REPO_ROOT / ".env")
    if not all(env.get(k) for k in ("SERPAPI_KEY", "IMAGE_BUCKET", "AWS_REGION")):
        sys.exit("Need SERPAPI_KEY, IMAGE_BUCKET and AWS_REGION in .env")
    for warning in loaded.warnings:
        print(f"warning: {warning}")

    live = Live(env, args.max_searches)
    session = NarrowSession(image_search=live.image_search, text_search=live.text_search,
                            face_compare=live.face_compare if args.face_limit > 0 else None,
                            face_limit=args.face_limit)
    images = sorted(loaded.bundle.images, key=lambda im: ANGLE_ORDER.get(im.angle, 9))
    photos = [(im.angle, (loaded.capture_dir / im.file).read_bytes()) for im in images]
    print(f"participant: {loaded.bundle.consent.participant}; {len(photos)} photo(s): "
          f"{', '.join(a for a, _ in photos)}")

    prompts = not args.no_prompt
    try:
        print("\nStep 1: image search ...")
        session.add_photos(photos)
        show(session, args.top, "after image search")

        name = args.name or ask("\nStep 2: full name (Enter to skip): ", prompts)
        if name:
            session.add_name(name)
            show(session, args.top, f"after name '{name}'")

        details = list(args.detail)
        while True:
            detail = details.pop(0) if details else ask(
                "\nAdd a detail (city, school, employer, username) or Enter to finish: ", prompts)
            if not detail:
                break
            session.add_detail(detail)
            show(session, args.top, f"after detail '{detail}'")
    except OutOfSearches:
        print(f"\nStopped: reached --max-searches {args.max_searches}. Showing what was found.")

    kept = [r for r in session.ranked() if r.level in ("confirmed", "likely")]
    print(f"\nStep 5: risk scoring on {len(kept)} confirmed/likely page(s)")
    if kept:
        items = [PresenceItem(item_id=f"p{i}", url=r.candidate.url, title=r.candidate.title,
                              text=r.candidate.snippet, platform=r.candidate.platform) for i, r in enumerate(kept, 1)]
        report = score_presence(session.name or loaded.bundle.consent.participant, items, build_jev(args.jev))
        print(f"overall {report.overall_level.upper()}, exposure score {report.overall_score}/100")
        for r in report.items:
            flag = "  [not this person]" if r.excluded else ""
            print(f"  {r.level.upper():6} {r.site[:32]:32} {r.kind:16}{flag}")
            for reason in r.reasons:
                print(f"         - {reason}")
    else:
        print("nothing confirmed: low exposure, or add more details")

    if args.save:
        out = REPO_ROOT / "runs" / f"narrow-{loaded.bundle.session_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({
            "participant": loaded.bundle.consent.participant, "name": session.name, "details": session.details,
            "searches": session.searches, "face_checks": session.face_checks,
            "pages": [{"url": r.candidate.url, "title": r.candidate.title, "snippet": r.candidate.snippet,
                       "level": r.level, "score": r.score, "reasons": r.reasons,
                       "angles": sorted(r.candidate.angles), "face_similarity": r.candidate.face_similarity}
                      for r in session.ranked()],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nsaved: {out}")
    print("\nCheck the results by hand: same-name strangers and lookalikes are common.")


if __name__ == "__main__":
    main()
