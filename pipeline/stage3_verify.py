"""Section 3: verification and confidence (spec 3).

Reads 01_raw_evidence.json and 02_profiles.json and writes 03_verification.json.
Code computes each candidate's signals (3.1). One Jev request scores every
candidate, or the fallback formula does when Jev is fake, off, or fails (3.2).
At most one candidate is accepted, and the reason is recorded. The percentages
are model judgments or uncalibrated rule scores, not measured identification
accuracy. Neither the graph nor text-only Jev compares faces.

Decision order once there are candidates:
1. A live (SerpApi) run that fell back to the formula returns review_required.
2. Real Jev only: best=none (>= 0.5) gives jev_none, and a `best` that isn't the
   top noul candidate gives jev_disagreement.
3. The top candidate needs a sourced name, direct image support, and an HTML
   account, or the reason is insufficient_evidence.
4. Accept the top candidate if confidence >= 0.80 and it leads the runner-up by
   >= 0.20. Otherwise the reason is below_threshold or too_close.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.artifacts import read_artifact, write_artifact
from pipeline.config import Settings
from pipeline.contracts import (
    CandidateProfile,
    CandidateSignals,
    CandidateVerdict,
    JevCall,
    ProfilesBundle,
    RawEvidenceBundle,
    VerificationNotes,
    VerificationResult,
    validate_references,
)
from pipeline.jev import Jev, choice, noul
from pipeline.names import names_compatible, normalize_name

# Fallback formula (3.2). These weights reproduce the spec's mock values; they are not calibrated.
WEIGHTS = {
    "bias": -4.0,
    "image_support": 2.0,
    "top_hit_share": 3.0,
    "mean_link_trust": 1.5,  # applied to trust / 100
    "platform_count": 0.3,  # applied to min(platform_count, PLATFORM_CAP)
    "consistency": 1.0,
    "name_collision": -0.5,
}
PLATFORM_CAP = 5

ACCEPT_CONFIDENCE = 0.80
ACCEPT_LEAD = 0.20
LEAD_TOLERANCE = 1e-9  # float noise only: a 0.85 vs 0.65 lead is 0.2, not 0.19999999999999996
JEV_NONE_THRESHOLD = 0.5
DISAGREE_GAP = 0.4  # |Jev - fallback| above this flags jev_fallback_disagree
SPARSE_ACCOUNTS = 3  # fewer accounts than this flags sparse_footprint
BIO_CHARS = 200  # per account bio in the Jev state; keeps the request well under 32K tokens
NOTE_CHARS = 300

NONE = "none"


def match_question_id(profile_id: str) -> str:
    return f"match_{profile_id}"


# 3.1 Signals


def compute_signals(raw: RawEvidenceBundle, profiles: ProfilesBundle) -> dict[str, CandidateSignals]:
    """Signals for every candidate profile, keyed by profile ID."""
    accounts = profiles.accounts
    num_images = raw.num_images  # failed or empty searches stay in the denominator

    # Page key -> the profiles it speaks for: the profile's own account pages and its attributed mentions.
    owner = {account_id: profile.profile_id for profile in profiles.profiles for account_id in profile.account_ids}
    supports: dict[str, set[str]] = {}
    for account_id, profile_id in owner.items():
        supports.setdefault(_key(raw, accounts[account_id].url), set()).add(profile_id)
    for mention in profiles.mentions:
        for account_id in mention.subject_account_ids:
            if account_id in owner:
                supports.setdefault(_key(raw, mention.url), set()).add(owner[account_id])

    top_hits: Counter[str] = Counter()
    for image in raw.images:
        if not image.hits:
            continue
        first = min(image.hits, key=lambda hit: hit.position)
        key = _key(raw, first.url)
        if key in image.page_urls:  # only an image that kept the page after triage counts it
            top_hits.update(supports.get(key, ()))

    signals = {}
    for profile in profiles.profiles:
        members = [accounts[account_id] for account_id in profile.account_ids]
        direct = {image_id for account in members for image_id in account.direct_image_ids}
        signals[profile.profile_id] = CandidateSignals(
            image_support=len(direct) / num_images,
            top_hit_share=top_hits[profile.profile_id] / num_images,
            mean_link_trust=profiles.graphs[profile.profile_id].mean_link_trust,
            platform_count=len({account.platform for account in members}),
            consistency=_consistency([account.workplace for account in members]),
            name_collision=any(
                _names_collide(profile.name, other.name)
                for other in profiles.profiles
                if other.profile_id != profile.profile_id
            ),
            newest_update=_newest([account.updated_at for account in members]),
        )
    return signals


def _key(raw: RawEvidenceBundle, url: str) -> str:
    return raw.page_key(url) or url


def _consistency(workplaces: list[str | None]) -> float:
    """Share of accounts that state the most common workplace. Missing workplaces count against it."""
    counts = Counter(normalized for normalized in map(normalize_name, workplaces) if normalized)
    if not counts:
        return 0.0
    _, count = min(counts.items(), key=lambda item: (-item[1], item[0]))  # ties break lexically
    return count / len(workplaces)


def _names_collide(a: str | None, b: str | None) -> bool:
    same = bool(normalize_name(a)) and normalize_name(a) == normalize_name(b)
    return same or names_compatible(a, b)


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _newest(values: list[str | None]) -> str | None:
    dated = [(when, value) for value in values if (when := _parse_when(value)) is not None]
    return max(dated)[1] if dated else None


# 3.2 Confidence


def fallback_confidence(signals: CandidateSignals) -> float:
    z = (
        WEIGHTS["bias"]
        + WEIGHTS["image_support"] * signals.image_support
        + WEIGHTS["top_hit_share"] * signals.top_hit_share
        + WEIGHTS["mean_link_trust"] * signals.mean_link_trust / 100
        + WEIGHTS["platform_count"] * min(signals.platform_count, PLATFORM_CAP)
        + WEIGHTS["consistency"] * signals.consistency
        + WEIGHTS["name_collision"] * signals.name_collision
    )
    return 1 / (1 + math.exp(-z))


def jev_request(
    raw: RawEvidenceBundle, profiles: ProfilesBundle, signals: dict[str, CandidateSignals]
) -> tuple[dict, dict[str, dict]]:
    """The single verification request: every candidate's account summaries and signals, plus X."""
    candidates = {}
    for profile in profiles.profiles:
        members = [profiles.accounts[account_id] for account_id in profile.account_ids]
        candidates[profile.profile_id] = {
            "name": profile.name,
            "signals": signals[profile.profile_id].model_dump(),
            "graph_flags": list(profiles.graphs[profile.profile_id].flags),
            "accounts": [
                {
                    "platform": account.platform,
                    "display_name": account.display_name,
                    "handle": account.handle,
                    "role": account.role,
                    "workplace": account.workplace,
                    "location": account.location,
                    "updated_at": account.updated_at,
                    "bio": (account.bio or "")[:BIO_CHARS] or None,
                    "found_by_images": len(account.direct_image_ids),
                    "best_hit_position": account.best_hit_position,
                    "followed_link_only": not account.direct_image_ids,
                    "snippet_only": account.extraction == "snippet",
                }
                for account in members
            ],
        }
    state = {
        "task": (
            "Reverse-image search ran on face images of one consenting participant. Each candidate groups the "
            "public accounts the search retrieved that appear to belong to one person. Judge how well the "
            "retrieved evidence supports each candidate. There are no pixels and no face comparison."
        ),
        "num_images": raw.num_images,
        "signal_notes": {
            "image_support": "share of images whose results directly reached this candidate",
            "top_hit_share": "share of images whose first result belongs to this candidate",
            "mean_link_trust": "0-100; how strongly the candidate's accounts link to each other",
            "consistency": "share of accounts that state the most common workplace",
            "name_collision": "another candidate has a compatible name",
        },
        "candidates": candidates,
    }
    questions = {
        match_question_id(profile_id): noul(
            f"Does the retrieved evidence support candidate {profile_id} as the searched participant? "
            "Treat missing or conflicting evidence as uncertainty; you have no pixels or face comparison.",
            {
                "true": "Several images and mutually linked accounts with consistent identity facts point to this candidate.",
                "false": "Evidence is weak, missing, or conflicting, or points to someone else such as a namesake.",
            },
        )
        for profile_id in candidates
    }
    options: dict[str, str | None] = {
        profile_id: f"Candidate {profile_id}: {candidate['name'] or 'unnamed'}, {len(candidate['accounts'])} account(s)"
        for profile_id, candidate in candidates.items()
    }
    options[NONE] = "None of them: the evidence is missing, conflicting, or points to someone else."
    questions["best"] = choice("Which candidate, if any, is the searched person?", options)
    return state, questions


def fake_answers(fallback: dict[str, float]) -> dict[str, dict]:
    """Synthetic wire-format answers for JEV_MODE=fake, derived from the fallback formula.

    They exercise request building and typed parsing; they are never used for scoring.
    """
    answers: dict[str, dict] = {
        match_question_id(profile_id): {"type": "noul", "noul": confidence} for profile_id, confidence in fallback.items()
    }
    ranked = _rank(fallback)
    picked = ranked[0] if fallback[ranked[0]] >= JEV_NONE_THRESHOLD else NONE
    options = [*fallback, NONE]
    rest = 0.1 / (len(options) - 1)
    answers["best"] = {
        "type": "choice",
        "choice": picked,
        "confidence": 0.9,
        "probabilities": {option: 0.9 if option == picked else rest for option in options},
    }
    return answers


def read_jev_answers(call: JevCall, profile_ids: list[str]) -> tuple[dict[str, float], dict] | None:
    """(noul per profile, best answer) from a complete, valid call; None means fall back for the whole stage."""
    if call.error is not None:
        return None
    try:
        nouls = {profile_id: call.answers[match_question_id(profile_id)]["noul"] for profile_id in profile_ids}
        best = call.answers["best"]
        picked, probabilities = best["choice"], best["probabilities"]
        none_probability = probabilities[NONE]
    except (KeyError, TypeError):
        return None
    numbers = [*nouls.values(), none_probability]
    if not all(_unit(value) for value in numbers) or picked not in [*profile_ids, NONE]:
        return None
    return nouls, best


def _unit(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


# Decision


def verify(
    raw: RawEvidenceBundle, profiles: ProfilesBundle, *, jev: Jev | None = None, live: bool = False
) -> VerificationResult:
    """Score every candidate and accept at most one (3.2).

    `jev` is None for JEV_MODE=off. `live` is true for a SerpApi run, where a
    fallback result needs human review instead of an automatic match.
    """
    if not profiles.profiles:
        # No Jev call and no synthesis model for an empty candidate list.
        reason = "evidence_unavailable" if any(image.errors for image in raw.images) else "no_candidates"
        return VerificationResult(run_id=raw.run_id, verdicts=[], accepted_profile_id=None, reason=reason, jev_calls=[])

    profile_ids = [profile.profile_id for profile in profiles.profiles]
    signals = compute_signals(raw, profiles)
    fallback = {profile_id: fallback_confidence(signals[profile_id]) for profile_id in profile_ids}

    calls: list[JevCall] = []
    jev_scores: dict[str, float] | None = None
    best: dict | None = None
    jev_failed = False
    if jev is not None:
        state, questions = jev_request(raw, profiles, signals)
        call = jev.ask(state, questions, fake_answers=None if jev.real else fake_answers(fallback))
        calls.append(call)
        if jev.real:
            parsed = read_jev_answers(call, profile_ids)
            if parsed is None:
                jev_failed = True
            else:
                jev_scores, best = parsed

    method = "jev" if jev_scores is not None else "fallback"
    confidence = jev_scores if jev_scores is not None else fallback
    ranked = _rank(confidence)
    top = ranked[0]
    lead = confidence[top] - (confidence[ranked[1]] if len(ranked) > 1 else 0.0)
    profile_of = {profile.profile_id: profile for profile in profiles.profiles}

    if method == "fallback" and live:
        reason = "review_required"
    elif best is not None and best["probabilities"][NONE] >= JEV_NONE_THRESHOLD:
        reason = "jev_none"
    elif best is not None and best["choice"] not in {p for p in ranked if confidence[p] == confidence[top]}:
        reason = "jev_disagreement"
    elif not _enough_evidence(profile_of[top], profiles, signals[top]):
        reason = "insufficient_evidence"
    elif confidence[top] < ACCEPT_CONFIDENCE:
        reason = "below_threshold"
    elif lead < ACCEPT_LEAD - LEAD_TOLERANCE:
        reason = "too_close"
    else:
        reason = "accepted"
    accepted = top if reason == "accepted" else None

    shared_flags = list(profiles.flags)
    if any(error.url is None for image in raw.images for error in image.errors):
        shared_flags.append("partial_search_failure")
    if jev_failed:
        shared_flags.append("verification_fallback")
    model = calls[0].model if method == "jev" else None

    verdicts = []
    for profile_id in ranked:
        profile = profile_of[profile_id]
        flags = [*profiles.graphs[profile_id].flags, *shared_flags]
        if len(profile.account_ids) < SPARSE_ACCOUNTS:
            flags.append("sparse_footprint")
        if method == "jev" and abs(confidence[profile_id] - fallback[profile_id]) > DISAGREE_GAP:
            flags.append("jev_fallback_disagree")
        verdicts.append(CandidateVerdict(
            profile_id=profile_id,
            name=profile.name,
            confidence=confidence[profile_id],
            fallback_confidence=fallback[profile_id],
            method=method,
            decision="accepted" if profile_id == accepted else "rejected",
            signals=signals[profile_id],
            flags=list(dict.fromkeys(flags)),
            rationale=rationale(profile, signals[profile_id], raw.num_images),
            jev_model=model,
        ))
    return VerificationResult(
        run_id=raw.run_id, verdicts=verdicts, accepted_profile_id=accepted, reason=reason, jev_calls=calls
    )


def _rank(confidence: dict[str, float]) -> list[str]:
    """Profile IDs, highest confidence first; ties keep natural ID order (p2 before p10)."""
    def natural(profile_id: str) -> list:
        return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", profile_id)]
    return sorted(confidence, key=lambda profile_id: (-confidence[profile_id], natural(profile_id)))


def _enough_evidence(profile: CandidateProfile, profiles: ProfilesBundle, signals: CandidateSignals) -> bool:
    """A sourced name, direct image support, and at least one HTML account. Snippets alone can't match."""
    members = [profiles.accounts[account_id] for account_id in profile.account_ids]
    name = normalize_name(profile.name)
    sourced = bool(name) and any(normalize_name(account.display_name) == name for account in members)
    has_html = any(account.extraction != "snippet" for account in members)
    return sourced and signals.image_support > 0 and has_html


def rationale(profile: CandidateProfile, signals: CandidateSignals, num_images: int) -> str:
    """Code-written explanation of the signals. Display only; never used for scoring."""
    found = round(signals.image_support * num_images)
    top = round(signals.top_hit_share * num_images)
    accounts = len(profile.account_ids)
    text = (
        f"Found by {found} of {num_images} image{'s' if num_images != 1 else ''} and the top result for {top}; "
        f"{accounts} account{'s' if accounts != 1 else ''} on {signals.platform_count} "
        f"platform{'s' if signals.platform_count != 1 else ''}; mean link trust {signals.mean_link_trust:g}; "
        f"{signals.consistency:.0%} state the same workplace."
    )
    if signals.name_collision:
        text += " Another candidate has a compatible name."
    return text


def apply_notes(result: VerificationResult, notes: VerificationNotes) -> VerificationResult:
    """Swap in agent-written rationales. Scores, decisions, and flags never change.

    Raises ValueError for unknown profile IDs, empty or overlong notes, or more than
    one sentence; the caller then keeps the code rationales.
    """
    known = {verdict.profile_id for verdict in result.verdicts}
    if unknown := sorted(set(notes.rationales) - known):
        raise ValueError(f"rationales name unknown profiles: {', '.join(unknown)}")
    cleaned = {}
    for profile_id, text in notes.rationales.items():
        text = " ".join(text.split())
        if not text or len(text) > NOTE_CHARS:
            raise ValueError(f"rationale for {profile_id} must be 1-{NOTE_CHARS} characters")
        if re.search(r"[.!?]\s+\S", text):
            raise ValueError(f"rationale for {profile_id} must be one sentence")
        cleaned[profile_id] = text
    verdicts = [
        verdict.model_copy(update={"rationale": cleaned.get(verdict.profile_id, verdict.rationale)})
        for verdict in result.verdicts
    ]
    return VerificationResult.model_validate(result.model_copy(update={"verdicts": verdicts}).model_dump())


# Stage entry points


def run_stage(run_dir: Path, settings: Settings) -> VerificationResult:
    """Read the run's 00/01/02 artifacts and return its VerificationResult. Writes nothing.

    The live policy follows the run's recorded backend, not the current settings,
    because `stage 3` replays saved SerpApi evidence with mock search settings.
    """
    capture = read_artifact(run_dir, 0)
    raw = read_artifact(run_dir, 1)
    profiles = read_artifact(run_dir, 2)
    validate_references(capture=capture, raw=raw, profiles=profiles)
    jev = None if settings.jev_mode == "off" else Jev(settings, run_dir=run_dir, stage=3)
    result = verify(raw, profiles, jev=jev, live=capture.backend == "serpapi")
    validate_references(profiles=profiles, verification=result)
    return result


def run(run_dir: Path, settings: Settings, refresh: bool = False) -> str:
    """CLI stage runner: verify, write 03_verification.json, and return the console view."""
    result = run_stage(run_dir, settings)
    write_artifact(run_dir, 3, result)
    return report(result)


def report(result: VerificationResult) -> str:
    """Spec 5.1 view 5: verdicts with percentages, method, flags, and reason."""
    if result.accepted_profile_id:
        headline = f"Verification: accepted {result.accepted_profile_id}"
    else:
        headline = f"Verification: no accepted candidate ({result.reason})"
    lines = [headline]
    if not result.verdicts:
        lines.append("  no candidates to score")
    for verdict in result.verdicts:
        score = f"{verdict.confidence:4.0%} {verdict.method}"
        if verdict.method == "jev":
            score += f" (fallback {verdict.fallback_confidence:.0%})"
        line = f"  {verdict.profile_id}  {verdict.name or '(unnamed)'}  {score}  {verdict.decision}"
        if verdict.flags:
            line += f"  [{', '.join(verdict.flags)}]"
        lines.append(line)
    for call in result.jev_calls:
        if call.error:
            lines.append(f"  Jev ({call.provider}) failed: {call.error}; used the fallback formula")
    if any(verdict.method == "fallback" for verdict in result.verdicts):
        lines.append("  Fallback scores are uncalibrated rule outputs, not identification accuracy.")
    return "\n".join(lines)
