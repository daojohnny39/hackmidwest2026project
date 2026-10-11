"""Check whether SerpApi's Yandex engine can fetch our S3 presigned link (dev test).

    python probe_yandex_url.py captures/celeb1/front.jpg            # link encoded twice (the fix)
    python probe_yandex_url.py captures/celeb1/front.jpg --plain    # link as the pipeline sends it now
    python probe_yandex_url.py captures/celeb1/front.jpg --engine google_lens --plain
    python probe_yandex_url.py captures/celeb1/front.jpg --public    # unsigned link, no "&" in it

Why: SerpApi copies the `url` parameter into Yandex's address without encoding it,
so Yandex cuts the presigned S3 link at its first "&" and loses the signature
("The URL does not refer to an image, or the image is not publicly accessible").
Encoding the link once more should survive that copy.

Uses SERPAPI_KEY, IMAGE_BUCKET and AWS_REGION from .env. Costs ONE SerpApi search
(TWO with Google Lens when it returns an AI overview, which is fetched right away).
The photo is uploaded under hackmidwest-faces/ and deleted at the end. Prints only
counts, titles and SerpApi's error text, never the key or the signed link.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path)
    parser.add_argument("--plain", action="store_true", help="send the link like the pipeline does today")
    parser.add_argument("--public", action="store_true",
                        help="send a plain unsigned link (needs the bucket policy that allows public reads of hackmidwest-faces/)")
    parser.add_argument("--engine", default="yandex_images", choices=("yandex_images", "google_lens"))
    parser.add_argument("--lens-type", default="all", choices=("all", "visual_matches", "exact_matches"),
                        help="Google Lens result type (default all)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    env = dotenv_values(REPO_ROOT / ".env")
    key, bucket, region = env.get("SERPAPI_KEY"), env.get("IMAGE_BUCKET"), env.get("AWS_REGION")
    if not (key and bucket and region):
        sys.exit("Need SERPAPI_KEY, IMAGE_BUCKET and AWS_REGION in .env")
    if not args.image.is_file():
        sys.exit(f"No file at {args.image}")
    data = args.image.read_bytes()
    content_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"

    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", region_name=region, config=Config(signature_version="s3v4"))
    object_key = f"hackmidwest-faces/probe-{secrets.token_hex(8)}{'.png' if content_type == 'image/png' else '.jpg'}"
    s3.put_object(Bucket=bucket, Key=object_key, Body=data, ContentType=content_type)
    try:
        if args.public:
            link = f"https://{bucket}.s3.{region}.amazonaws.com/{object_key}"
            check = httpx.get(link, timeout=20)  # can the open internet read it?
            print(f"public link readable without login: {'yes' if check.status_code == 200 else f'no (HTTP {check.status_code})'}")
            if check.status_code != 200:
                raise SystemExit("Stopping before using a search credit: fix the bucket policy first.")
            sent = link
        else:
            link = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": object_key}, ExpiresIn=600)
            sent = link if args.plain else quote(link, safe="")
        params = {"engine": args.engine, "url": sent, "api_key": key}
        if args.engine == "google_lens":
            params["type"] = args.lens_type
        if args.engine == "yandex_images":
            params["tab"] = "about"
        response = httpx.get("https://serpapi.com/search.json", timeout=60, params=params)
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        overview = None
        token = (payload.get("ai_overview") or {}).get("page_token")
        if token:  # the token expires within minutes, so fetch the overview now (one more search credit)
            follow = httpx.get("https://serpapi.com/search.json", timeout=60, params={
                "engine": "google_ai_overview", "page_token": token, "api_key": key})
            if follow.headers.get("content-type", "").startswith("application/json"):
                overview = follow.json()
    finally:
        s3.delete_object(Bucket=bucket, Key=object_key)

    mode = "public unsigned link" if args.public else "plain (today's pipeline)" if args.plain else "encoded twice"
    print(f"engine: {args.engine}, link sent: {mode}")
    print(f"HTTP {response.status_code}, SerpApi status: {payload.get('search_metadata', {}).get('status')}")
    if payload.get("error"):
        print(f"SerpApi error: {payload['error']}")
    sections = {k: (len(v) if isinstance(v, list) else type(v).__name__) for k, v in payload.items()
                if k not in ("search_metadata", "search_parameters")}
    print(f"response sections: {sections}")
    for name in ("exact_matches", "related_content"):
        for row in (payload.get(name) or [])[:5]:
            if isinstance(row, dict):
                print(f"  {name}: {str(row.get('title') or row.get('query', ''))[:90]}  [{row.get('source', '')}]")
    for kg in payload.get("knowledge_graph") or []:
        if isinstance(kg, dict):
            print(f"knowledge graph: {kg.get('title')}  ({kg.get('subtitle', '')})")
    results = payload.get("image_results") or payload.get("visual_matches") or []
    print(f"results: {len(results)}")
    for row in results[:10]:
        print(f"  - {row.get('title', '')[:90]}  [{row.get('source') or row.get('link', '')[:60]}]")
    if overview is not None:
        print("\nAI overview (second credit):")
        if overview.get("error"):
            print(f"  error: {overview['error']}")
        body = overview.get("ai_overview") or {}
        for block in (body.get("text_blocks") or [])[:8]:
            text = block.get("snippet") or " ".join(str(i.get("snippet", "")) for i in block.get("list") or [])
            if text:
                print(f"  - {text[:220]}")
        for ref in (body.get("references") or [])[:6]:
            print(f"  source: {str(ref.get('title', ''))[:80]}  [{ref.get('source', '')}]")
    print("test photo deleted from S3")


if __name__ == "__main__":
    main()
