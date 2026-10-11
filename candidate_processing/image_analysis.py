"""Callable Stage A: authorized candidate image to measurements and crop assets."""

import asyncio
import base64
import binascii
import hashlib
import sqlite3

from pydantic import Field, ValidationError, model_validator

from .contracts import AnalyzedCandidate, CandidateInput, Classification, Contract, Evidence, FaceCrop, Failure, ImageAnalysis
from .image_validation import (
    ImageValidation, IsolatedImageValidator, WORKER_MESSAGES, _WorkerFailure, load_candidate_image,
)
from .image_worker import INVALID_MESSAGES, MAX_IMAGE_BYTES
from .storage import AssetNotFound, LocalAssetStore, StorageFull


class _EncodedCrop(Contract):
    face_index: int = Field(ge=0, le=1)
    box: list[float] = Field(min_length=4, max_length=4)
    jpeg: str = Field(min_length=1, max_length=192 * 1024)


class ImageProcessingResult(Contract):
    analysis: ImageAnalysis
    crops: list[_EncodedCrop] = Field(default_factory=list, max_length=2)
    errors: list[Failure] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def check_crops(self):
        if self.analysis.status != "ok" and not self.errors:
            raise ValueError("Unsuccessful image processing requires a failure reason")
        if self.analysis.crops:
            raise ValueError("Worker must not supply existing crop asset IDs")
        pending = [FaceCrop(assetId="pending", faceIndex=crop.face_index, box=crop.box) for crop in self.crops]
        ImageAnalysis.model_validate({**self.analysis.model_dump(), "crops": pending})
        for crop in self.crops:
            try:
                jpeg = base64.b64decode(crop.jpeg, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError("Worker supplied invalid crop encoding") from error
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise ValueError("Worker supplied an invalid JPEG crop")
        return self


class IsolatedImageAnalyzer(IsolatedImageValidator):
    async def analyze(self, data: bytes) -> ImageProcessingResult:
        if not isinstance(data, bytes):
            raise TypeError("Image analysis requires immutable bytes")
        if not data or len(data) > MAX_IMAGE_BYTES:
            code = "empty_image" if not data else "image_too_large"
            return ImageProcessingResult(analysis=ImageAnalysis(status="invalid", valid=False, byteLength=len(data)),
                                         errors=[Failure(stage="validation", code=code, message=INVALID_MESSAGES[code])])
        digest = hashlib.sha256(data).hexdigest()
        async with self._lock:
            try:
                raw = await self._run_worker(data, analyze=True)
                report = ImageProcessingResult.model_validate_json(raw)
            except (ValidationError, ValueError):
                report = self._failed("invalid_worker_response")
            except _WorkerFailure as error:
                report = self._failed(error.code)
        report.analysis = ImageAnalysis.model_validate({
            **report.analysis.model_dump(), "byte_length": len(data), "content_sha256": digest,
        })
        return report

    @staticmethod
    def _failed(code: str) -> ImageProcessingResult:
        return ImageProcessingResult(analysis=ImageAnalysis(status="error"),
                                     errors=[Failure(stage="image", code=code, message=WORKER_MESSAGES[code])])

    async def analyze_candidate(self, candidate: CandidateInput, store: LocalAssetStore, *, scope: str) -> AnalyzedCandidate:
        """Preserve one source record; classification remains explicitly unassessed."""
        loaded = await load_candidate_image(candidate.image, store, scope=scope)
        if isinstance(loaded, ImageValidation):
            report = ImageProcessingResult(analysis=ImageAnalysis(status=loaded.status, valid=loaded.valid), errors=[loaded.error])
        else:
            report = await self.analyze(loaded)
        crops = []
        try:
            for crop in report.crops:
                pending = asyncio.create_task(asyncio.to_thread(store.put, base64.b64decode(crop.jpeg), scope=scope, mime_type="image/jpeg"))
                try:
                    asset = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    # A cancelled caller still owns cleanup of a completed
                    # thread write; don't leave an unreferenced crop behind.
                    try:
                        asset = await pending
                    except (OSError, sqlite3.Error, StorageFull, ValueError):
                        pass
                    else:
                        await asyncio.to_thread(store.delete, asset.id, scope=scope)
                    raise
                crops.append(FaceCrop(assetId=asset.id, faceIndex=crop.face_index, box=crop.box))
        except asyncio.CancelledError:
            for saved in crops:
                try:
                    await asyncio.to_thread(store.delete, saved.asset_id, scope=scope)
                except AssetNotFound:
                    pass
            raise
        except (OSError, sqlite3.Error, StorageFull, ValueError):
            report.errors.append(Failure(stage="storage", code="crop_storage_failed", message="A face crop could not be stored."))
        analysis = ImageAnalysis.model_validate({**report.analysis.model_dump(), "crops": crops})
        review_reasons = list(dict.fromkeys([
            "classification_not_assessed", *analysis.flags, *[error.code for error in report.errors],
        ]))
        return AnalyzedCandidate(
            id=candidate.id, sourceId=candidate.source_id, imageUrl=candidate.image_url, sourceUrl=candidate.source_url,
            assetId=candidate.image.asset_id, imageAnalysis=analysis, errors=report.errors,
            classification=Classification(reasons=[Evidence(code="classification_not_assessed", message="Context classification belongs to Stage B.", method="operational")]),
            reviewRequired=True, reviewReasons=review_reasons,
        )
