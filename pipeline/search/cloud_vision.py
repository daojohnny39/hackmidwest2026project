"""Google Cloud Vision web detection, the engine V6 searches instead of Google Lens.

Lens (through SerpApi) and Cloud Vision's WEB_DETECTION feature both look an image up in
Google's image index. Cloud Vision is Google's own API for it, so it takes the face crop's bytes
in the request (no image hosting) and a Google Cloud API key with the Cloud Vision API enabled
(`python -m pipeline set-key google-vision`). One request per image (REST v1, docs checked
2026-10-11):

POST https://vision.googleapis.com/v1/images:annotate with
{"requests": [{"image": {"content": <base64>}, "features": [{"type": "WEB_DETECTION", "maxResults": 50}]}]}.
The key goes in the X-Goog-Api-Key header, never in the URL. The reply's webDetection has:
- pagesWithMatchingImages: pages showing the same image (fullMatchingImages) or a crop or edit of
  it (partialMatchingImages), with a pageTitle that may contain HTML;
- fullMatchingImages and partialMatchingImages: the image URLs, without their pages;
- visuallySimilarImages: image URLs that look alike, without their pages;
- webEntities and bestGuessLabels, which V6 doesn't use.

vision_results() turns that into backend V3's engine shape: the pages, each with its matching
images, then the images no listed page shows, whose link is the image URL itself. Errors are
Google's JSON error objects; a bad key is HTTP 400 with reason API_KEY_INVALID (checked live with
a made-up key, 2026-10-11). Pricing (checked 2026-10-11): the first 1,000 web detection images a
month are free, then $3.50 per 1,000.
"""

from __future__ import annotations

import base64
import html
import re

import httpx

from pipeline import trace
from pipeline.search.base import BackendError, _thumbnail, status_error
from pipeline.search.serpapi_backend import SEARCH_TIMEOUT_S, USER_AGENT

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
MAX_RESULTS = 50
CHECK_TIMEOUT_S = 10.0
TAG = re.compile(r"<[^>]*>")
# ErrorInfo reasons -> BackendError reasons; other errors map by HTTP status.
ERROR_REASONS = {"API_KEY_INVALID": "unauthorized", "API_KEY_SERVICE_BLOCKED": "forbidden",
                 "SERVICE_DISABLED": "forbidden", "BILLING_DISABLED": "forbidden", "RATE_LIMIT_EXCEEDED": "rate_limited"}
SNIPPETS = {"full": "Cloud Vision: page showing the same image",
            "partial": "Cloud Vision: page showing part of the image",
            "image": "Cloud Vision: the same image, page not given",
            "similar": "Cloud Vision: visually similar image, page not given"}


class KeyRejected(Exception):
    """Google refused the API key."""


class CheckFailed(Exception):
    """Google couldn't be reached or answered unexpectedly; the key may still be valid."""


def request_body(image: bytes) -> dict:
    return {"requests": [{"image": {"content": base64.b64encode(image).decode("ascii")},
                          "features": [{"type": "WEB_DETECTION", "maxResults": MAX_RESULTS}]}]}


def google_error(payload: object) -> tuple[str, str, str]:
    """(status, ErrorInfo reason, message) from a Google error reply; empty strings where missing."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "", "", ""
    reasons = [detail.get("reason") for detail in error.get("details") or []
               if isinstance(detail, dict) and isinstance(detail.get("reason"), str)]
    status, message = error.get("status"), error.get("message")
    return (status if isinstance(status, str) else "", reasons[0] if reasons else "",
            message[:300] if isinstance(message, str) else "")


class CloudVisionSearch:
    """Async client for one stage-1 run: one web detection request per image."""

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, trust_env=False,
                                                  follow_redirects=False, headers={"User-Agent": USER_AGENT})

    async def search(self, image_id: str, image: bytes) -> dict:
        """The webDetection object for image (empty if Google found nothing); raises BackendError."""
        try:
            response = await self.client.post(VISION_URL, json=request_body(image),
                                              headers={"X-Goog-Api-Key": self.api_key})
        except httpx.TimeoutException:
            raise BackendError("timeout") from None
        except httpx.HTTPError:  # never show the exception: it could carry the request
            raise BackendError("connection_error") from None
        payload = _json(response)
        if response.status_code != 200:
            status, reason, message = google_error(payload)
            trace.error("cloud_vision_http_error", image=image_id, status=response.status_code,
                        google_status=status, reason=reason, message=message)
            error = status_error(response.status_code, response.headers)
            if reason in ERROR_REASONS:
                error = BackendError(ERROR_REASONS[reason], status_code=response.status_code,
                                     retry_after=error.retry_after)
            raise error
        replies = payload.get("responses") if isinstance(payload, dict) else None
        if not isinstance(replies, list) or len(replies) != 1 or not isinstance(replies[0], dict):
            raise BackendError("invalid_response")
        reply = replies[0]
        if isinstance(reply.get("error"), dict):  # this image failed, e.g. "Bad image data."
            error = reply["error"]
            trace.error("cloud_vision_image_error", image=image_id, code=error.get("code"),
                        message=str(error.get("message") or "")[:300])
            raise BackendError("provider_error")
        detection = reply.get("webDetection", {})
        if not isinstance(detection, dict):
            raise BackendError("invalid_response")
        return detection

    async def aclose(self) -> None:
        await self.client.aclose()


def vision_results(detection: dict) -> dict:
    """webDetection in backend V3's sanitized engine shape. Pages come first, in Google's order, each
    with its full then partial matching images. Then the full matches, partial matches and visually
    similar images that no listed page shows, linked to the image URL itself, since Google doesn't
    say where they appear."""
    results, seen = [], set()
    for page in _list(detection, "pagesWithMatchingImages"):
        link = _url(page.get("url"))
        if link is None:
            continue
        full, partial = _urls(page, "fullMatchingImages"), _urls(page, "partialMatchingImages")
        seen.update(full + partial)
        title = page.get("pageTitle")
        results.append({
            "link": link,
            "title": " ".join(html.unescape(TAG.sub("", title)).split())[:300] if isinstance(title, str) else "",
            "snippet": SNIPPETS["full" if full else "partial"],
            "thumbnail": None, "images": list(dict.fromkeys(full + partial)),
        })
    for key, kind in (("fullMatchingImages", "image"), ("partialMatchingImages", "image"),
                      ("visuallySimilarImages", "similar")):
        for url in _urls(detection, key):
            if url in seen:
                continue
            seen.add(url)
            results.append({"link": url, "title": "", "snippet": SNIPPETS[kind], "thumbnail": None, "images": [url]})
    return {"search_metadata": {"status": "Success"}, "results": results}


def check_key(api_key: str) -> str:
    """Check a key with an empty annotate request, which analyzes no image. Returns a short note;
    raises KeyRejected for a key Google says is invalid, CheckFailed otherwise."""
    try:
        response = httpx.post(VISION_URL, json={"requests": []}, timeout=CHECK_TIMEOUT_S, trust_env=False,
                              headers={"X-Goog-Api-Key": api_key, "User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise CheckFailed(type(exc).__name__) from None
    status, reason, message = google_error(_json(response))
    if reason == "API_KEY_INVALID":
        raise KeyRejected(reason)
    if response.status_code == 403:
        raise CheckFailed(f"HTTP 403 {reason or status}: {message}" if message else f"HTTP 403 {reason or status}")
    if response.status_code == 200 or (response.status_code == 400 and status == "INVALID_ARGUMENT"):
        return "the Cloud Vision API answered"  # any other 400 is about the empty request, not the key
    raise CheckFailed(f"HTTP {response.status_code}")


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _list(scope: dict, key: str) -> list[dict]:
    rows = scope.get(key)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _url(value: object) -> str | None:
    return _thumbnail(value) if isinstance(value, str) else None


def _urls(scope: dict, key: str) -> list[str]:
    return [url for url in (_url(row.get("url")) for row in _list(scope, key)) if url]
