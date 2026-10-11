"""Classify and order an already analyzed Stage A batch; no downloads or agents."""

import argparse
import asyncio
import json
import math
import re

from .classification import ClassificationOutcome, classify_candidate, failure_outcome
from .contracts import AnalyzedCandidate, BatchResult, CandidateBatch, CandidateInput, Evidence, Failure
from .jev_client import JevClient, JevProvider

BATCH_DEADLINE_SECONDS = 120.0
RELEVANCE_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def apply_classification(result: AnalyzedCandidate, outcome: ClassificationOutcome) -> AnalyzedCandidate:
    """Copy Stage A's result; a text decision never clears image concerns."""
    updated = result.model_copy(deep=True)
    previous_codes = {reason.code for reason in result.classification.reasons}
    previous_codes.update(error.code for error in result.errors if error.stage == "classification")
    previous_codes.update({"classification_not_assessed", "classification_unknown", "classification_medium", "source_type_unknown"})
    reasons = [code for code in result.review_reasons if code not in previous_codes]
    if result.review_required and not result.review_reasons:
        reasons.append("prior_review_required")
    updated.classification = outcome.classification.model_copy(deep=True)
    updated.errors = [error for error in updated.errors if error.stage != "classification"]
    updated.errors.extend(error.model_copy(deep=True) for error in outcome.errors)
    analysis = updated.image_analysis
    if analysis.status != "ok":
        reasons.append(f"image_{analysis.status}")
    if analysis.face_count == 0:
        reasons.append("no_face_detected")
    if analysis.face_count is not None and analysis.face_count > 1:
        reasons.append("multiple_faces")
    if analysis.at_detector_limit:
        reasons.append("detector_capacity_reached")
    if analysis.quality != "good":
        reasons.append(f"quality_{analysis.quality}")
    reasons.extend(analysis.flags)
    reasons.extend(error.code for error in updated.errors)
    reasons.extend(outcome.review_reasons)
    if updated.classification.status != "ok":
        reasons.append(f"classification_{updated.classification.status}")
    if updated.classification.relevance in {"medium", "unknown"}:
        reasons.append(f"classification_{updated.classification.relevance}")
    if updated.classification.source_type == "unknown":
        reasons.append("source_type_unknown")
    updated.review_reasons = list(dict.fromkeys(reasons))
    updated.review_required = bool(updated.review_reasons)
    return AnalyzedCandidate.model_validate(updated.model_dump())


def prioritize_candidates(results: list[AnalyzedCandidate], *, request_id: str,
                          capture_id: str | None = None) -> BatchResult:
    """Input order breaks ties. Unknowns, failures and duplicates stay inspectable."""
    canonical = [result for result in results if result.disposition == "candidate"]
    ranked = sorted(canonical, key=lambda result: RELEVANCE_ORDER[result.classification.relevance])
    return BatchResult(
        requestId=request_id, captureId=capture_id,
        candidates=[result.model_copy(deep=True) for result in results],
        rankedCandidateIds=[result.id for result in ranked],
        reviewCandidateIds=[result.id for result in results if result.review_required],
    )


def _duplicate_key(candidate: CandidateInput, analyzed: AnalyzedCandidate) -> str | None:
    # Only backend-produced, successfully validated image hashes are eligible.
    # Unavailable images and unverifiable hashes never establish duplicates.
    image = analyzed.image_analysis
    if (candidate.image.status != "available" or image.valid is not True
            or not re.fullmatch(r"[0-9a-f]{64}", image.content_sha256 or "")):
        return None
    return json.dumps({
        "sourceUrl": candidate.source_url, "sha256": image.content_sha256,
        "subject": candidate.subject.model_dump(), "pageTitle": candidate.page_title,
        "description": candidate.description, "profileMetadata": candidate.profile_metadata.model_dump(),
    }, sort_keys=True, ensure_ascii=False)


async def classify_analyzed_batch(batch: CandidateBatch, analyzed_results: list[AnalyzedCandidate],
                                 provider: JevProvider, *, deadline_seconds: float = BATCH_DEADLINE_SECONDS,
                                 _deadline_at: float | None = None) -> BatchResult:
    """Run Stage B on trusted backend Stage A results in the original input order.

    This is not an HTTP boundary: callers must never accept image measurements
    or hashes from client JSON. Stage C's callable owns combined vision
    orchestration, exact-image reuse and the absolute shared deadline.
    """
    if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= BATCH_DEADLINE_SECONDS):
        raise ValueError("Batch deadline must be greater than zero and at most 120 seconds")
    batch = CandidateBatch.model_validate(batch.model_dump())
    analyzed_results = [AnalyzedCandidate.model_validate(result.model_dump()) for result in analyzed_results]
    by_id = {result.id: result for result in analyzed_results}
    if len(by_id) != len(analyzed_results) or set(by_id) != {item.id for item in batch.candidates}:
        raise ValueError("Stage B requires one Stage A result for every input candidate")
    for candidate in batch.candidates:
        result = by_id[candidate.id]
        if (result.source_id, result.source_url, result.image_url, result.asset_id) != (
                candidate.source_id, candidate.source_url, candidate.image_url, candidate.image.asset_id):
            raise ValueError("Stage A provenance must match its input candidate")
        if result.disposition != "candidate":
            raise ValueError("Supply original Stage A candidates before duplicate grouping")

    results, cache = [], {}
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    if _deadline_at is not None:
        deadline = min(deadline, _deadline_at)
    for candidate in batch.candidates:
        analyzed = by_id[candidate.id]
        key = _duplicate_key(candidate, analyzed)
        if key is not None and key in cache:
            canonical_id, outcome = cache[key]
            result = apply_classification(analyzed, outcome)
            result.disposition, result.duplicate_of = "duplicate", canonical_id
            result.classification.reasons.append(Evidence(
                code="exact_duplicate_record", message="Equivalent subject, page context and verified image bytes reuse the canonical classification.",
                field="duplicateOf", excerpt=canonical_id, method="rule",
            ))
        else:
            try:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(deadline):
                    outcome = await classify_candidate(candidate, provider)
            except TimeoutError:
                outcome = failure_outcome(Failure(stage="classification", code="classification_batch_timeout",
                                                 message="The Stage B batch deadline elapsed before classification completed.", retryable=True),
                                          provider.sdk_version)
            except Exception:
                # Preserve accepted evidence if an injected provider fails
                # unexpectedly. Cancellation is BaseException and propagates.
                outcome = failure_outcome(Failure(stage="classification", code="classification_failed",
                                                 message="Context classification could not be completed."), provider.sdk_version)
            result = apply_classification(analyzed, outcome)
            if key is not None:
                cache[key] = (candidate.id, outcome)
        results.append(result)
    return prioritize_candidates(results, request_id=batch.request_id, capture_id=batch.capture_id)


async def _run_live_smoke() -> int:
    candidate = CandidateInput(
        id="synthetic-smoke", imageUrl="https://example.invalid/unused.jpg",
        sourceUrl="https://example.invalid/team/avery-example",
        pageTitle="Avery Example — Example Research Studio",
        description="Avery Example is a software designer at Example Research Studio and builds accessible education tools.",
        subject={"name": "Avery Example", "usernames": ["avery_example"],
                 "context": "Software designer at Example Research Studio building accessible education tools."},
        profileMetadata={"username": "avery_example", "organization": "Example Research Studio", "role": "Software designer"},
        image={"status": "unavailable"},
    )
    async with JevClient() as provider:
        outcome = await classify_candidate(candidate, provider)
    success = outcome.classification.status == "ok"
    print(json.dumps({
        "liveProviderResponseValidated": success,
        "syntheticDataOnly": True,
        "classification": outcome.classification.model_dump(mode="json", by_alias=True),
        "errors": [error.model_dump(mode="json", by_alias=True) for error in outcome.errors],
        "reviewReasons": outcome.review_reasons,
    }, indent=2))
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic-only Jev live smoke test; sends no photos or downloaded content.")
    parser.add_argument("--live", action="store_true", help="Send the fixed synthetic text to the configured Jev model.")
    if not parser.parse_args().live:
        parser.error("Pass --live explicitly to enable a provider request")
    return asyncio.run(_run_live_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
