"""Risk scoring (prototype): how risky is each page we found about the person?

Input: PresenceItem list (one per page/account we found for the identity).
Output: RiskReport with a level per page (low / medium / high), a short reason,
and an overall exposure score 0-100.

How a level is chosen, per page:
1. Rules give a FLOOR from facts we can check without a model:
   - site category (people-search, dating, adult, leak sites -> high), and
   - personal data seen in the text (phone number or street address -> high,
     email or birth date -> medium).
2. Jev answers two typed questions per page (one batched call):
   - risk_<id>  score  0 low / 1 medium / 2 high  (the exposure risk to the person)
   - kind_<id>  choice what kind of page it is (also lets Jev say "not about this person")
3. Final level = max(Jev level, rule floor). Jev can RAISE a level (it reads the
   text), but it can never lower a level the rules proved. This is the same idea
   as the stage-2 name veto: a confident model can't overrule a hard fact.
   Jev level = the expected score rounded, but HIGH if P(high) >= HIGH_ALERT,
   because missing a real high risk is worse than one extra warning.
If Jev is off, fake, or the call fails, the rules decide alone (method "rules").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pipeline.jev import Jev, choice, score

LEVELS = ("low", "medium", "high")
LEVEL_POINTS = (5, 15, 40)  # overall score = sum of points, capped at 100
HIGH_ALERT = 0.35  # P(high) at or above this -> high
EXCLUDE_AT = 0.80  # P(not_about_person) at or above this -> page left out of the score
MAX_ITEMS_PER_CALL = 15  # 2 questions each; ~2.6K chars per page at worst, so 15 pages stay under 60K
MAX_TEXT_CHARS = 800

# --- The question Jev answers -------------------------------------------------

RISK_LEVELS = [
    # 0
    "Low: professional or intentionally public presence (work profile, portfolio, code, "
    "press, official or fan pages). Shows little beyond name, job, and public work.",
    # 1
    "Medium: personal social presence that shows everyday life - personal photos, friends "
    "or family names, hobbies, school, city, routine. Helps someone build a profile of the "
    "person or pretend to be them.",
    # 2
    "High: sensitive or dangerous exposure - home address, phone number, exact or live "
    "location, children's details, ID numbers, health or money details, dating or adult "
    "content, people-search or data-broker listings, or leaked/breached data.",
]

RISK_INSTRUCTIONS = (
    "Rate how much this page exposes the subject to real harm (stalking, scams, identity "
    "theft, harassment) if a stranger found it. Judge what the page reveals about the "
    "subject, not how popular the site is. If the page shows several things, rate the worst one."
)

KINDS = {
    "professional": "Work, portfolio, code, or official page about the subject.",
    "public_media": "News, interview, encyclopedia, or fan page about the subject.",
    "social": "Personal social media account or post by or about the subject.",
    "personal_contact": "Shows the subject's phone, email, or home address.",
    "location": "Shows where the subject lives, is, or will be.",
    "sensitive": "Dating, adult, health, money, people-search, or leaked data about the subject.",
    "not_about_person": "This page is not about the subject (same name, different person, or unrelated).",
}

KIND_INSTRUCTIONS = "What kind of page is this, for the subject?"

# --- Rules -------------------------------------------------------------------

# category -> (floor, prior). Floor is a hard minimum; prior is used only when Jev isn't.
CATEGORY_LEVELS = {
    "professional": (0, 0),
    "public_media": (0, 0),
    "social": (0, 1),
    "other": (0, 1),
    "dating": (2, 2),
    "adult": (2, 2),
    "people_search": (2, 2),
    "leak": (2, 2),
}

SITE_CATEGORIES: dict[str, str] = {
    # professional
    "linkedin.com": "professional", "github.com": "professional", "gitlab.com": "professional",
    "behance.net": "professional", "dribbble.com": "professional", "orcid.org": "professional",
    "scholar.google.com": "professional", "linkup.test": "professional", "codeforge.test": "professional",
    # public media
    "wikipedia.org": "public_media", "wikidata.org": "public_media", "imdb.com": "public_media",
    "medium.com": "public_media", "substack.com": "public_media", "fieldnotes.test": "public_media",
    "riverbend-news.test": "public_media", "syndicated.test": "public_media",
    # social
    "x.com": "social", "twitter.com": "social", "instagram.com": "social", "facebook.com": "social",
    "tiktok.com": "social", "reddit.com": "social", "youtube.com": "social", "threads.net": "social",
    "snapchat.com": "social", "pinterest.com": "social", "chatter.test": "social", "photogram.test": "social",
    # dating
    "tinder.com": "dating", "bumble.com": "dating", "hinge.co": "dating", "okcupid.com": "dating",
    "match.com": "dating", "pof.com": "dating",
    # adult
    "onlyfans.com": "adult", "fansly.com": "adult",
    # people search / data brokers
    "whitepages.com": "people_search", "spokeo.com": "people_search", "beenverified.com": "people_search",
    "truepeoplesearch.com": "people_search", "fastpeoplesearch.com": "people_search",
    "radaris.com": "people_search", "mylife.com": "people_search", "peoplefinders.com": "people_search",
    "intelius.com": "people_search", "thatsthem.com": "people_search",
    # leaks / pastes
    "pastebin.com": "leak", "ghostbin.co": "leak",
}

CATEGORY_TO_KIND = {
    "professional": "professional", "public_media": "public_media", "social": "social", "other": "social",
    "dating": "sensitive", "adult": "sensitive", "people_search": "sensitive", "leak": "sensitive",
}

_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_STREET = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|"
    r"Ct|Court|Way|Pl|Place|Ter|Terrace|Pkwy|Parkway)\b\.?"
)
_BIRTH = re.compile(r"\b(?:born|DOB|date of birth|birthday)\b[:\s]", re.IGNORECASE)


@dataclass
class PresenceItem:
    item_id: str  # short, stable: "p1", "p2", ...
    url: str
    title: str = ""
    text: str = ""  # snippet or page summary; what Jev reads
    platform: str | None = None  # from pipeline.platforms if known


@dataclass
class ItemRisk:
    item_id: str
    url: str
    site: str
    category: str
    level: str  # low / medium / high
    kind: str
    method: str  # "jev" or "rules"
    reasons: list[str] = field(default_factory=list)
    jev_probabilities: dict[str, float] | None = None
    excluded: bool = False  # Jev says the page is not about this person


@dataclass
class RiskReport:
    subject: str
    items: list[ItemRisk]
    overall_score: int  # 0-100
    overall_level: str
    jev_errors: list[str] = field(default_factory=list)


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    host = host.removeprefix("m.").removeprefix("mobile.")
    return host


def category_of(url: str) -> str:
    host = site_of(url)
    parts = host.split(".")
    for start in range(len(parts) - 1):  # en.wikipedia.org -> wikipedia.org
        candidate = ".".join(parts[start:])
        if candidate in SITE_CATEGORIES:
            return SITE_CATEGORIES[candidate]
    return "other"


def rule_floor(item: PresenceItem) -> tuple[int, int, list[str]]:
    """(floor, prior, reasons) from the site category and personal data in the text."""
    category = category_of(item.url)
    floor, prior = CATEGORY_LEVELS[category]
    reasons = [f"site type: {category.replace('_', ' ')}"] if floor else []
    text = f"{item.title}\n{item.text}"
    found = []
    if _PHONE.search(text):
        found.append(("phone number", 2))
    if _STREET.search(text):
        found.append(("street address", 2))
    if _EMAIL.search(text):
        found.append(("email address", 1))
    if _BIRTH.search(text) and category != "public_media":  # a star's birth date on Wikipedia is expected
        found.append(("birth date", 1))
    for what, level in found:
        reasons.append(f"shows {what}")
        floor = max(floor, level)
    return floor, max(prior, floor), reasons


# --- Jev request ---------------------------------------------------------------


def risk_state(subject: str, items: list[PresenceItem]) -> dict:
    return {
        "subject": {"name": subject, "note": "The person whose online exposure is being rated."},
        "pages": {
            item.item_id: {
                "site": site_of(item.url),
                "platform": item.platform,
                "url": item.url,
                "title": item.title[:200],
                "text": item.text[:MAX_TEXT_CHARS],
            }
            for item in items
        },
    }


def risk_questions(items: list[PresenceItem]) -> dict[str, dict]:
    questions = {}
    for item in items:
        questions[f"risk_{item.item_id}"] = score(f"Page {item.item_id}: {RISK_INSTRUCTIONS}", RISK_LEVELS)
        questions[f"kind_{item.item_id}"] = choice(f"Page {item.item_id}: {KIND_INSTRUCTIONS}", KINDS)
    return questions


def fake_answers(items: list[PresenceItem]) -> dict[str, dict]:
    """Rule answers in Jev's format, for JEV_MODE=fake (synthetic, not probabilities)."""
    answers = {}
    for item in items:
        _, prior, _ = rule_floor(item)
        kind = CATEGORY_TO_KIND[category_of(item.url)]
        answers[f"risk_{item.item_id}"] = {
            "type": "score", "score": float(prior), "confidence": 1.0,
            "probabilities": {str(i): 1.0 if i == prior else 0.0 for i in range(3)},
        }
        answers[f"kind_{item.item_id}"] = {
            "type": "choice", "choice": kind, "confidence": 1.0,
            "probabilities": {k: 1.0 if k == kind else 0.0 for k in KINDS},
        }
    return answers


def jev_level(answer: dict) -> int:
    probabilities = answer["probabilities"]
    if probabilities.get("2", 0.0) >= HIGH_ALERT:
        return 2
    expected = sum(int(level) * p for level, p in probabilities.items())
    return min(2, max(0, int(expected + 0.5)))


# --- Main entry ----------------------------------------------------------------


def score_presence(subject: str, items: list[PresenceItem], jev: Jev | None = None) -> RiskReport:
    """Rate every page. jev=None means rules only. Never raises for Jev problems."""
    answers: dict[str, dict] = {}
    errors: list[str] = []
    if jev is not None:
        for start in range(0, len(items), MAX_ITEMS_PER_CALL):
            chunk = items[start:start + MAX_ITEMS_PER_CALL]
            call = jev.ask(risk_state(subject, chunk), risk_questions(chunk),
                           fake_answers=fake_answers(chunk) if not jev.real else None)
            if call.error:
                errors.append(call.error)
            else:
                answers.update(call.answers)

    results = []
    for item in items:
        floor, prior, reasons = rule_floor(item)
        category = category_of(item.url)
        risk = answers.get(f"risk_{item.item_id}")
        kind_answer = answers.get(f"kind_{item.item_id}")
        if risk and kind_answer and jev is not None and jev.real:
            level = max(jev_level(risk), floor)
            kind = kind_answer["choice"]
            excluded = kind_answer["probabilities"].get("not_about_person", 0.0) >= EXCLUDE_AT
            if jev_level(risk) > floor:
                reasons.append("Jev rated the page content higher than the rules")
            results.append(ItemRisk(
                item_id=item.item_id, url=item.url, site=site_of(item.url), category=category,
                level=LEVELS[level], kind=kind, method="jev", reasons=reasons,
                jev_probabilities=risk["probabilities"], excluded=excluded,
            ))
        else:
            results.append(ItemRisk(
                item_id=item.item_id, url=item.url, site=site_of(item.url), category=category,
                level=LEVELS[prior], kind=CATEGORY_TO_KIND[category], method="rules", reasons=reasons,
            ))

    counted = [r for r in results if not r.excluded]
    total = min(100, sum(LEVEL_POINTS[LEVELS.index(r.level)] for r in counted))
    overall = max((LEVELS.index(r.level) for r in counted), default=0)
    return RiskReport(subject=subject, items=results, overall_score=total,
                      overall_level=LEVELS[overall], jev_errors=errors)
