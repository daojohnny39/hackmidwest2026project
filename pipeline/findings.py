"""Per-source findings for the photo-misuse self-check (the 2026-10-11 pivot): one entry per search
hit whose image matched the user's face, in the candidate shape of frontend/API-CONTRACT.md.

For now a source's confidence is its best face score: Amazon Rekognition's similarity between the
user's face and the face in the result image (backend V3, or dev mode's fake database), labeled with the bands the per-image agent
is told about (rekognition_backend.SCORE_BANDS). Other backends' scores use other scales, and the mock
and Yandex alone have none, so their level is "unknown". Two kinds of context are added as reasons
and never change the confidence: whether the name and city the person gave (contracts.Subject)
appear on the page, and the agent's note on a page it fetched. A fetched page that shows their face
without their name may be someone else using the photo. Live search gives no threat level; in dev
mode server.py adds one from search/fakedb_backend.FakeDbThreats.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from pipeline.contracts import ImageHit, PageRecord, RawEvidenceBundle, Subject
from pipeline.names import normalize_name
from pipeline.platforms import PLATFORM_LABELS, page_kind, platform_for
from pipeline.search.rekognition_backend import score_band

NOTE_LIMIT = 200
FACE_SCORED_BACKENDS = ("rekognition", "fakedb")  # their face_score is a Rekognition similarity
# rekognition_backend.SCORE_BANDS labels -> the level the website shows
LEVELS = {"strong match": "strong", "likely match": "likely", "possible match": "possible",
          "weak match": "weak", "unlikely match": "unlikely"}


@dataclass
class Findings:
    candidates: list[dict]  # highest confidence first
    incomplete: bool = False  # an image's search, one of its engines, or a page fetch failed
    failed: bool = False  # every image's search failed, so an empty list doesn't mean "no matches"
    warnings: list[str] = field(default_factory=list)  # sanitized codes


@dataclass
class _Source:
    key: str  # pages key, or the hit URL when the page wasn't collected
    hit: ImageHit  # the hit with the best face score
    score: int | None
    images: list[str] = field(default_factory=list)


def build_findings(raw: RawEvidenceBundle, subject: Subject | None = None) -> Findings:
    """One finding per page across all images, highest face score first; ties keep search order."""
    subject = subject or Subject()
    sources: dict[str, _Source] = {}
    for image in raw.images:
        rekognition = image.backend in FACE_SCORED_BACKENDS
        for hit in image.hits:
            key = raw.page_key(hit.url) or hit.url
            score = hit.face_score if rekognition else None
            source = sources.get(key)
            if source is None:
                source = sources[key] = _Source(key=key, hit=hit, score=score)
            elif score is not None and (source.score is None or score > source.score):
                source.hit, source.score = hit, score
            if image.image_id not in source.images:
                source.images.append(image.image_id)

    ranked = sorted(sources.values(), key=lambda source: -1 if source.score is None else -source.score)
    candidates = [_candidate(source, raw.pages.get(source.key), raw.num_images, subject) for source in ranked]

    searched = [image for image in raw.images if not any(error.url is None for error in image.errors)]
    warnings = []
    if len(searched) < len(raw.images):
        warnings.append("search_failed")
    if any(image.search_extras.get("failed_engines") for image in raw.images):
        warnings.append("engine_failed")
    if any(error.url is not None for image in raw.images for error in image.errors):
        warnings.append("page_unavailable")
    return Findings(candidates=candidates, incomplete=bool(warnings), failed=not searched, warnings=warnings)


def _candidate(source: _Source, page: PageRecord | None, num_images: int, subject: Subject) -> dict:
    hit = source.hit
    platform = page.platform if page else platform_for(source.key)
    kind = page.page_kind if page else page_kind(source.key)
    title = (page.title if page else "") or hit.title
    if source.score is None:
        confidence = {"level": "unknown", "reasons": ["No face score for this source."]}
    else:
        band = score_band(source.score)
        reasons = [f"Face similarity {source.score}/100 from Amazon Rekognition ({band})."]
        if num_images > 1:
            reasons.append(f"Found from {len(source.images)} of {num_images} photos.")
        confidence = {"score": source.score, "level": LEVELS[band], "reasons": reasons}
    confidence["reasons"] += _subject_reasons(subject, page, hit)
    note = _agent_note(page)
    if note:
        confidence["reasons"].append(note)
    return {
        "id": "src-" + hashlib.sha256(source.key.encode()).hexdigest()[:12],
        "sourceUrl": source.key,
        **({"imageUrl": hit.thumbnail_url} if hit.thumbnail_url else {}),
        **({"pageTitle": title} if title else {}),
        "sourceType": f"{PLATFORM_LABELS.get(platform, PLATFORM_LABELS['other'])} {kind}",
        "confidence": confidence,
    }


def _subject_reasons(subject: Subject, page: PageRecord | None, hit: ImageHit) -> list[str]:
    """Whether the given name and city appear on the page, or in the search result when the page wasn't
    fetched. Only a fetched page is long enough to say that the name is missing."""
    fetched = page is not None and page.extraction == "html"
    if fetched:
        names = [block["name"] for block in page.json_ld if isinstance(block.get("name"), str)]
        text = " ".join([page.title, page.meta_description, page.text, *page.og.values(), *names])
    else:
        text = f"{page.title} {page.text}" if page else f"{hit.title} {hit.snippet}"
    where = "on this page" if fetched else "in the search result"
    reasons = []
    if subject.name:
        if _mentions(text, subject.name):
            reasons.append(f"Your name appears {where}.")
        elif fetched:
            reasons.append("Your name doesn't appear on this page.")
    if subject.city and _mentions(text, subject.city):
        reasons.append(f"Your city appears {where}.")
    return reasons


def _mentions(text: str, phrase: str) -> bool:
    """Whether phrase appears in text as whole words, ignoring case, accents and punctuation."""
    words = normalize_name(phrase)
    return bool(words) and re.search(rf"(?<![a-z0-9]){re.escape(words)}(?![a-z0-9])", normalize_name(text)) is not None


def _agent_note(page: PageRecord | None) -> str | None:
    """The per-image agent's note on a page it fetched. Notes are "<image_id>: <note>" or
    "<image_id> excluded: <note>" (stage1_collect.build_bundle)."""
    for note in page.notes if page else []:
        text = note.partition(": ")[2].strip()
        if text:
            return f"Agent note: {text[:NOTE_LIMIT]}"
    return None
