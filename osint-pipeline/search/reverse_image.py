"""Reverse image search tool for the Strands agent (SerpApi-backed).

SETUP
-----
1. SerpApi key: sign up at https://serpapi.com, then copy the key from
   https://serpapi.com/manage-api-key (the free tier includes ~100 searches/mo).
2. Put it in `.env` (gitignored — never commit a real key):

       SERPAPI_KEY=your_key_here
       USE_REAL_SEARCH=false
       FAKE_SERVER_URL=http://localhost:8000/search

MODES
-----
USE_REAL_SEARCH=true   -> real SerpApi calls, costs credits, needs SERPAPI_KEY.
anything else (default) -> GETs FAKE_SERVER_URL instead. Free, offline, no PII.

Flip modes per-run without editing .env:

    USE_REAL_SEARCH=true python -m search.reverse_image        # Linux/macOS
    $env:USE_REAL_SEARCH="true"; python -m search.reverse_image  # PowerShell

ENGINES
-------
engine="yandex" (default) -> SerpApi "yandex_images"; best recall on faces.
engine="google"           -> SerpApi "google_lens".
Both return the identical output shape, so downstream code never branches on it.

Nothing is written to disk — results are returned in memory and dropped.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

import requests

try:
    from strands import tool
except ImportError:  # strands not installed yet — keep the module importable
    def tool(func):  # type: ignore[misc]
        return func

LOG = logging.getLogger(__name__)

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
DEFAULT_FAKE_URL = "http://localhost:8000/search"
TIMEOUT_S = 15.0

# engine name we accept -> (SerpApi engine, key holding the match list)
ENGINES: dict[str, tuple[str, str]] = {
    "yandex": ("yandex_images", "image_results"),
    "google": ("google_lens", "visual_matches"),
}

# Extend as the demo needs more sites; anything unlisted becomes "other".
PLATFORM_DOMAINS: dict[str, str] = {
    "linkedin.com": "LinkedIn",
    "github.com": "GitHub",
    "instagram.com": "Instagram",
}

# image_match is a *rank proxy*, not a visual-similarity score — neither engine
# returns a real confidence number, so rank 0 gets 1.0 and each step down loses
# a little, floored so a deep result still reads as "weak match", not "no match".
MATCH_STEP = 0.05
MATCH_FLOOR = 0.10


def _use_real_search() -> bool:
    return (os.getenv("USE_REAL_SEARCH") or "").strip().lower() in {"true", "1", "yes"}


def _platform_for(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    for domain, name in PLATFORM_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return "other"


def _text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _to_result(raw: dict[str, Any], rank: int) -> dict[str, Any] | None:
    """One engine result -> our shape. None if there is no usable URL."""
    url = _text(raw, "link", "url", "source_link", "original_page_url")
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "platform": _platform_for(url),
        "display_name": _text(raw, "title", "name", "source") or _platform_for(url),
        "url": url,
        "snippet": _text(raw, "snippet", "description", "subtitle", "source"),
        "image_match": round(max(MATCH_FLOOR, 1.0 - rank * MATCH_STEP), 2),
    }


def _parse(payload: Any, result_key: str) -> list[dict[str, Any]]:
    """Accepts a SerpApi payload, or a list already in our output shape."""
    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict):
        if payload.get("error"):
            LOG.warning("search API returned an error: %s", payload["error"])
            return []
        # the engine's own key first, then whatever the fake server calls it
        matches = next(
            (
                payload[key]
                for key in (result_key, "image_results", "visual_matches", "results")
                if isinstance(payload.get(key), list)
            ),
            [],
        )
    else:
        return []

    results: list[dict[str, Any]] = []
    for rank, raw in enumerate(matches):
        if not isinstance(raw, dict):
            continue
        # Already in our shape (fake server may hand back finished results).
        if {"platform", "display_name", "url", "image_match"} <= raw.keys():
            results.append(raw)
            continue
        parsed = _to_result(raw, rank)
        if parsed:
            results.append(parsed)
    return results


def _fetch(image_url: str, engine: str) -> list[dict[str, Any]]:
    """Do the HTTP call for whichever mode is active. Raises on failure."""
    serp_engine, result_key = ENGINES[engine]

    if _use_real_search():
        key = (os.getenv("SERPAPI_KEY") or "").strip()
        if not key:
            raise RuntimeError("USE_REAL_SEARCH=true but SERPAPI_KEY is empty")
        endpoint = SERPAPI_ENDPOINT
        params = {"engine": serp_engine, "url": image_url, "api_key": key}
    else:
        endpoint = (os.getenv("FAKE_SERVER_URL") or DEFAULT_FAKE_URL).strip()
        params = {"engine": serp_engine, "url": image_url, "image_url": image_url}

    response = requests.get(endpoint, params=params, timeout=TIMEOUT_S)
    response.raise_for_status()
    return _parse(response.json(), result_key)


@tool
def reverse_image_search(image_url: str, engine: str = "yandex") -> list[dict]:
    """Reverse-image-search a photo and return the pages that contain it.

    Args:
        image_url: Public http(s) URL of the image to search for.
        engine: "yandex" (default, strongest on faces) or "google".

    Returns:
        A list of matches, best first, each with: platform ("LinkedIn",
        "GitHub", "Instagram" or "other"), display_name, url, snippet, and
        image_match (0-1, estimated from result rank). Returns an empty list
        if the search fails, is misconfigured, or finds nothing — it never
        raises, so the agent can just move on.
    """
    engine = (engine or "yandex").strip().lower()
    if engine not in ENGINES:
        LOG.warning("unknown engine %r — expected one of %s", engine, list(ENGINES))
        return []
    if not image_url or not image_url.startswith(("http://", "https://")):
        LOG.warning("image_url must be an http(s) URL, got %r", image_url)
        return []

    try:
        results = _fetch(image_url, engine)
    except requests.Timeout:
        LOG.warning("%s search timed out after %.0fs", engine, TIMEOUT_S)
        return []
    except Exception as exc:  # noqa: BLE001 — the agent gets [] whatever breaks
        LOG.warning("%s search failed: %s", engine, exc)
        return []

    if not results:
        LOG.info("%s search returned no usable matches", engine)
    return results


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.environ.setdefault("USE_REAL_SEARCH", "false")  # demo never spends credits

    demo_image = "https://upload.wikimedia.org/wikipedia/commons/3/3f/Placeholder_view_vector.svg"
    print(f"mode: {'REAL SerpApi' if _use_real_search() else 'fake server'}")
    print(f"fake server: {os.getenv('FAKE_SERVER_URL') or DEFAULT_FAKE_URL}\n")

    matches = reverse_image_search(demo_image)
    print(f"{len(matches)} result(s):")
    print(json.dumps(matches, indent=2))
    if not matches:
        print("\n(empty is expected if the fake server isn't running — start it with:")
        print(" uvicorn search.fake_server:app --port 8000 )")
