"""Member 3's callable Stage A/B pipeline. No discovery, agent or HTTP required."""

import asyncio
import hashlib
import math
from typing import Protocol

from .contracts import AnalyzedCandidate, CandidateBatch, CandidateInput, Failure, ImageAnalysis
from .image_analysis import ImageProcessingResult, IsolatedImageAnalyzer, save_image_analysis
from .image_validation import ImageValidation, load_candidate_image
from .jev_client import JevClient, JevProvider
from .stage_b import BATCH_DEADLINE_SECONDS, classify_analyzed_batch
from .storage import LocalAssetStore


class ImageAnalyzer(Protocol):
    async def analyze(self, data: bytes) -> ImageProcessingResult: ...


def failed_image(candidate: CandidateInput, code: str, message: str) -> AnalyzedCandidate:
    return AnalyzedCandidate(
        id=candidate.id, sourceId=candidate.source_id, sourceUrl=candidate.source_url,
        imageUrl=candidate.image_url, assetId=candidate.image.asset_id,
        imageAnalysis=ImageAnalysis(status="error"), reviewReasons=[code],
        errors=[Failure(stage="pipeline", code=code, message=message, retryable=True)],
    )


async def analyze_candidates(
    request: dict | CandidateBatch, *, store: LocalAssetStore, scope: str,
    analyzer: ImageAnalyzer | None = None, provider: JevProvider | None = None,
    deadline_seconds: float = BATCH_DEADLINE_SECONDS,
) -> dict:
    """Validate the whole batch, then return versioned, JSON-compatible findings.

    Dependencies and scope are backend-owned, never read from candidate JSON.
    A supplied provider belongs to the caller; otherwise a client is opened and
    closed for this call. Cancellation propagates and the image worker is reaped.
    The deadline covers asset reads, vision, crop writes and classification;
    cancellation cleanup can take additional time to finish safely.
    """
    batch = CandidateBatch.model_validate(request.model_dump() if isinstance(request, CandidateBatch) else request)
    LocalAssetStore._validate_scope(scope)
    if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= BATCH_DEADLINE_SECONDS):
        raise ValueError("Batch deadline must be greater than zero and at most 120 seconds")
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    analyzer = analyzer if analyzer is not None else IsolatedImageAnalyzer()
    results, cache = [], {}
    for candidate in batch.candidates:
        try:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            async with asyncio.timeout_at(deadline):
                loaded = await load_candidate_image(candidate.image, store, scope=scope)
                if isinstance(loaded, ImageValidation):
                    report = ImageProcessingResult(
                        analysis=ImageAnalysis(status=loaded.status, valid=loaded.valid), errors=[loaded.error],
                    )
                    result = await save_image_analysis(candidate, report, store, scope=scope)
                else:
                    # Recompute from authorized bytes; never trust a URL, caller
                    # hash or another scope's cache. Cache lives only this batch.
                    digest = hashlib.sha256(loaded).hexdigest()
                    if digest in cache:
                        result = cache[digest].model_copy(deep=True, update={
                            "id": candidate.id, "source_id": candidate.source_id,
                            "source_url": candidate.source_url, "image_url": candidate.image_url,
                            "asset_id": candidate.image.asset_id,
                        })
                    else:
                        report = await analyzer.analyze(loaded)
                        report = ImageProcessingResult.model_validate(report.model_dump())
                        report.analysis.content_sha256 = digest
                        report.analysis.byte_length = len(loaded)
                        result = await save_image_analysis(candidate, report, store, scope=scope)
                        # Operational failures retry on later candidates. Crops
                        # from successful analysis are shared within this scope.
                        if result.image_analysis.status == "ok" and not result.errors:
                            cache[digest] = result.model_copy(deep=True)
        except TimeoutError:
            result = failed_image(candidate, "pipeline_batch_timeout", "The combined batch deadline elapsed before image analysis completed.")
        except Exception:
            result = failed_image(candidate, "image_analysis_failed", "Candidate image analysis could not be completed.")
        results.append(result)

    async def finish(active_provider):
        result = await classify_analyzed_batch(batch, results, active_provider, _deadline_at=deadline)
        return result.model_dump(mode="json", by_alias=True)

    if provider is not None:
        return await finish(provider)
    async with JevClient() as owned_provider:
        return await finish(owned_provider)
