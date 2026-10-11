"""Narrowing search (prototype): photos first, then name, then more details, until the user stops.

Steps, each one re-ranks every page found so far:
1. Image search: every capture photo (up to 6 angles) goes to Yandex reverse image
   search. Pages found from several angles rank higher.
2. Name: a Google search for the name adds pages, and every page whose text has the
   name gets a boost, including pages that step 1 found.
3. Image <-> name: for the top pages that have a picture (thumbnail), Rekognition
   CompareFaces checks the picture against the front photo. Same face -> big boost.
   A clear different face -> big penalty (this removes lookalikes).
4. More details (city, school, employer, username...): each one is searched together
   with the name, and pages that mention it get a boost. Repeat until the user stops.

Scores are simple points with written reasons, so the ranking can be explained:
    found by image search        +2 (+1 more if 2 or more angles found it)
    full name in the page text   +3 (last name only: +1)
    found by the name search     +1
    each detail in the text      +1 (at most +3)
    details given, page has none of them and no photo/face evidence  -1
    face matches (>= 90)         +4    face looks similar (70-90) +1
    clearly different face (<50) -4
Levels: confirmed >= 7, likely >= 4, possible >= 2, below that: noise.

This module does no network calls itself: the search and face functions are passed
in, so the logic is tested offline (tests/test_narrow.py).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from pipeline.platforms import normalize_url, platform_for

LEVELS = ((7, "confirmed"), (4, "likely"), (2, "possible"))
FACE_SAME, FACE_SIMILAR, FACE_DIFFERENT = 90.0, 70.0, 50.0

ImageSearch = Callable[[bytes], list[dict]]  # photo bytes -> result rows (title, link, snippet, source, thumbnail)
TextSearch = Callable[[str], list[dict]]  # query -> result rows
FaceCompare = Callable[[bytes, str], float | None]  # (front photo, thumbnail URL) -> similarity, None = no face/failed


def _fold(text: str) -> str:
    """Lowercase, no accents, single spaces: 'Élodie  Durand' -> 'elodie durand'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w@.]+", " ", text.lower())).strip()


def _thumbnail(row: dict) -> str | None:
    thumb = row.get("thumbnail")
    if isinstance(thumb, dict):
        thumb = thumb.get("link") or thumb.get("src") or thumb.get("url")
    return thumb if isinstance(thumb, str) and thumb.startswith("http") else None


@dataclass
class Candidate:
    url: str
    title: str = ""
    snippet: str = ""
    source: str = ""
    thumbnail: str | None = None
    angles: set[str] = field(default_factory=set)  # capture angles whose image search found it
    queries: set[str] = field(default_factory=set)  # text searches that found it
    face_similarity: float | None = None
    face_checked: bool = False

    @property
    def text(self) -> str:
        return _fold(f"{self.title} {self.snippet} {self.url}")

    @property
    def platform(self) -> str:
        return platform_for(self.url)


@dataclass
class Ranked:
    candidate: Candidate
    score: int
    level: str
    reasons: list[str]


class NarrowSession:
    def __init__(self, *, image_search: ImageSearch, text_search: TextSearch, face_compare: FaceCompare | None = None,
                 face_limit: int = 10):
        self.image_search, self.text_search, self.face_compare = image_search, text_search, face_compare
        self.face_limit = face_limit
        self.candidates: dict[str, Candidate] = {}
        self.name: str | None = None
        self.details: list[str] = []
        self.front_photo: bytes | None = None
        self.searches = 0  # SerpApi credits used
        self.face_checks = 0  # Rekognition CompareFaces calls
        self.photo_only_checks = 0  # of those, on pages found only by the photo search

    # --- steps -----------------------------------------------------------------

    def add_photos(self, photos: list[tuple[str, bytes]]) -> int:
        """Step 1. photos: (angle, bytes), front first if there is one. Returns new pages."""
        before = len(self.candidates)
        for angle, data in photos:
            if self.front_photo is None or angle == "front":
                self.front_photo = data  # face checks use the front photo when there is one
            rows = self.image_search(data)
            self.searches += 1
            for row in rows:
                cand = self._merge(row)
                if cand:
                    cand.angles.add(angle)
        self.check_faces()
        return len(self.candidates) - before

    def add_name(self, name: str) -> int:
        """Step 2 (+3). Returns new pages."""
        self.name = name.strip()
        return self._text_step(f'"{self.name}"')

    def add_detail(self, detail: str) -> int:
        """Step 4, repeatable. Searched together with the name when there is one."""
        detail = detail.strip()
        self.details.append(detail)
        query = f'"{self.name}" {detail}' if self.name else detail
        return self._text_step(query)

    def check_faces(self) -> None:
        """Step 3: CompareFaces on the best unchecked pages that have a picture."""
        if not self.face_compare or self.front_photo is None:
            return
        # Pages that already match the name or a detail go first. Pages found only by the
        # photo search (often lookalikes) may use at most half of the budget, so checks are
        # left for the name and detail steps.
        ranked = [r for r in self.ranked() if r.candidate.thumbnail and not r.candidate.face_checked]
        evidence = [r.candidate for r in ranked if r.candidate.queries or self._name_or_detail_hit(r.candidate)]
        photo_only = [r.candidate for r in ranked if r.candidate not in evidence]
        for cand in evidence + photo_only:
            if self.face_checks >= self.face_limit:
                break
            if cand in photo_only and self.photo_only_checks >= self.face_limit // 2:
                continue
            cand.face_checked = True
            cand.face_similarity = self.face_compare(self.front_photo, cand.thumbnail)
            self.face_checks += 1
            if cand in photo_only:
                self.photo_only_checks += 1

    def _name_or_detail_hit(self, cand: Candidate) -> bool:
        text = cand.text
        if self.name and all(t in text for t in _fold(self.name).split() if len(t) >= 2):
            return True
        return any(d and _fold(d) in text for d in self.details)

    # --- ranking ---------------------------------------------------------------

    def score(self, cand: Candidate) -> Ranked:
        points, reasons = 0, []
        if cand.angles:
            points += 2
            reasons.append(f"found by image search ({', '.join(sorted(cand.angles))})")
            if len(cand.angles) >= 2:
                points += 1
        if self.name:
            tokens = [t for t in _fold(self.name).split() if len(t) >= 2]
            text = cand.text
            if tokens and all(t in text for t in tokens):
                points += 3
                reasons.append("full name in the page")
            elif tokens and tokens[-1] in text:
                points += 1
                reasons.append("last name in the page")
            if any(q.startswith(f'"{self.name}"') for q in cand.queries):
                points += 1
                reasons.append("found by the name search")
        hits = [d for d in self.details if d and _fold(d) in cand.text]
        if hits:
            points += min(3, len(hits))
            reasons.append(f"mentions {', '.join(hits)}")
        sim = cand.face_similarity
        if self.details and not hits and not cand.angles and not (sim is not None and sim >= FACE_SIMILAR):
            points -= 1  # the user narrowed the search and this page matches none of it
            reasons.append("matches none of the details")
        if sim is not None:
            if sim >= FACE_SAME:
                points += 4
                reasons.append(f"face matches ({sim:.0f})")
            elif sim >= FACE_SIMILAR:
                points += 1
                reasons.append(f"face looks similar ({sim:.0f})")
            elif sim < FACE_DIFFERENT:
                points -= 4
                reasons.append(f"different face ({sim:.0f})")
        level = next((name for minimum, name in LEVELS if points >= minimum), "noise")
        return Ranked(cand, points, level, reasons)

    def ranked(self) -> list[Ranked]:
        return sorted((self.score(c) for c in self.candidates.values()),
                      key=lambda r: (-r.score, -len(r.candidate.angles), r.candidate.url))

    # --- helpers ---------------------------------------------------------------

    def _text_step(self, query: str) -> int:
        before = len(self.candidates)
        rows = self.text_search(query)
        self.searches += 1
        for row in rows:
            cand = self._merge(row)
            if cand:
                cand.queries.add(query)
        self.check_faces()
        return len(self.candidates) - before

    def _merge(self, row: dict) -> Candidate | None:
        link = row.get("link")
        url = normalize_url(link) if isinstance(link, str) else None
        if not url:
            return None
        cand = self.candidates.get(url)
        if cand is None:
            cand = self.candidates[url] = Candidate(url=url)
        cand.title = cand.title or str(row.get("title") or "")[:300]
        cand.snippet = cand.snippet or str(row.get("snippet") or "")[:500]
        cand.source = cand.source or str(row.get("source") or "")
        cand.thumbnail = cand.thumbnail or _thumbnail(row)
        return cand
