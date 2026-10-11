"""Stage A/B handoff, deduplication, stable ranking and failure preservation."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from candidate_processing.classification import ClassificationOutcome, classify_candidate
from candidate_processing.contracts import BatchResult, CandidateBatch, Classification, Failure, ImageAnalysis
from candidate_processing.image_analysis import IsolatedImageAnalyzer
from candidate_processing.jev_client import JevFailure
from candidate_processing.stage_b import _run_live_smoke, apply_classification, classify_analyzed_batch, main, prioritize_candidates
from candidate_processing.storage import LocalAssetStore
from face_landmarker import DEFAULT_MODEL_PATH
from tests.member3.test_classification import MockJev, analyzed, candidate, valid_image


class StageBTests(unittest.IsolatedAsyncioTestCase):
    def batch(self, items):
        return CandidateBatch(schemaVersion=1, requestId="request-1", captureId="capture-1", candidates=items)

    async def test_empty_batch_does_not_call_provider(self):
        provider = MockJev()
        result = await classify_analyzed_batch(self.batch([]), [], provider)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.ranked_candidate_ids, [])
        self.assertEqual(provider.calls, [])

    async def test_stable_ranking_keeps_all_inputs_and_unknowns(self):
        labels = ["unknown", "high", "low", "medium", "high"]
        results = []
        for index, label in enumerate(labels):
            item = candidate(id=f"candidate-{index}")
            result = analyzed(item, imageAnalysis=valid_image())
            result = apply_classification(result, ClassificationOutcome(
                Classification(status="ok", relevance=label, sourceType="professional_profile"), [], [],
            ))
            results.append(result)
        ordered = prioritize_candidates(results, request_id="request-1")
        self.assertEqual(ordered.ranked_candidate_ids, ["candidate-1", "candidate-4", "candidate-3", "candidate-2", "candidate-0"])
        self.assertEqual(ordered.review_candidate_ids, ["candidate-0", "candidate-3"])
        self.assertEqual([r.id for r in ordered.candidates], [r.id for r in results])
        self.assertEqual(BatchResult.model_validate_json(ordered.model_dump_json(by_alias=True)), ordered)

    async def test_high_does_not_clear_image_errors_quality_or_multiple_face_flags(self):
        item = candidate()
        outcome = await classify_candidate(item, MockJev())
        for image, expected in (
            (ImageAnalysis(), "image_unavailable"),
            (ImageAnalysis(status="invalid", valid=False), "image_invalid"),
            (ImageAnalysis(status="error"), "image_error"),
            (valid_image(faceCount=0), "no_face_detected"),
            (valid_image(faceCount=2, atDetectorLimit=True), "multiple_faces"),
            (valid_image(quality="poor", flags=["possible_blur"]), "possible_blur"),
            (valid_image(quality="unknown", flags=["quality_thresholds_uncalibrated"]), "quality_thresholds_uncalibrated"),
        ):
            with self.subTest(expected=expected):
                original = analyzed(item, imageAnalysis=image)
                updated = apply_classification(original, outcome)
                self.assertEqual(updated.classification.relevance, "high")
                self.assertTrue(updated.review_required)
                self.assertIn(expected, updated.review_reasons)
                self.assertEqual(updated.image_analysis, original.image_analysis)
                self.assertEqual(original.classification.status, "skipped")
                self.assertNotIn("classification_not_assessed", updated.review_reasons)

    async def test_image_failure_and_successful_classification_are_independent(self):
        item = candidate(image={"status": "failed", "error": {
            "stage": "retrieval", "code": "download_failed", "message": "Image download failed.",
        }})
        original = analyzed(item, errors=[item.image.error])
        result = await classify_analyzed_batch(self.batch([item]), [original], MockJev())
        self.assertEqual(result.candidates[0].classification.status, "ok")
        self.assertEqual(result.candidates[0].errors[0], item.image.error)
        self.assertEqual(result.ranked_candidate_ids, [item.id])
        self.assertEqual(result.review_candidate_ids, [item.id])

    async def test_mixed_provider_failures_do_not_stop_later_candidates(self):
        items = [candidate(id=f"candidate-{i}", pageTitle=str(i)) for i in range(3)]
        class Mixed(MockJev):
            async def decide(self, state, questions):
                title = state["pageFields"]["pageTitle"]
                if title == "0":
                    raise JevFailure("jev_timeout", "Timed out.", retryable=True)
                if title == "1":
                    raise RuntimeError("PRIVATE failure details")
                return await super().decide(state, questions)
        result = await classify_analyzed_batch(self.batch(items), [analyzed(i, imageAnalysis=valid_image()) for i in items], Mixed())
        self.assertEqual([r.classification.status for r in result.candidates], ["error", "error", "ok"])
        self.assertEqual(result.ranked_candidate_ids, ["candidate-2", "candidate-0", "candidate-1"])
        self.assertNotIn("PRIVATE", result.model_dump_json())
        self.assertTrue(all(r.image_analysis.valid for r in result.candidates))

    async def test_all_provider_failures_remain_ranked_and_reviewable(self):
        items = [candidate(id=f"candidate-{i}", sourceUrl=f"https://example.invalid/page-{i}") for i in range(3)]
        class Failed(MockJev):
            async def decide(self, *_):
                raise JevFailure("jev_authentication_failed", "Server credentials rejected.")
        result = await classify_analyzed_batch(self.batch(items), [analyzed(i) for i in items], Failed())
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(result.ranked_candidate_ids, [i.id for i in items])
        self.assertEqual(result.review_candidate_ids, [i.id for i in items])

    async def test_exact_record_dedup_reuses_classification_and_preserves_provenance(self):
        items = [candidate(), candidate(id="candidate-2", sourceId="source-2", imageUrl="https://example.invalid/other.jpg")]
        provider = MockJev()
        result = await classify_analyzed_batch(self.batch(items), [analyzed(i, imageAnalysis=valid_image()) for i in items], provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.ranked_candidate_ids, ["candidate-1"])
        duplicate = result.candidates[1]
        self.assertEqual(duplicate.disposition, "duplicate")
        self.assertEqual(duplicate.duplicate_of, "candidate-1")
        self.assertEqual((duplicate.id, duplicate.source_id, duplicate.image_url, duplicate.source_url),
                         (items[1].id, items[1].source_id, items[1].image_url, items[1].source_url))
        self.assertEqual(result.capture_id, "capture-1")
        self.assertEqual(result.request_id, "request-1")

    async def test_same_image_on_different_pages_or_with_changed_context_is_not_collapsed(self):
        for patch in ({"sourceUrl": "https://example.invalid/another-page"},
                      {"description": "A different page description."},
                      {"profileMetadata": {"organization": "Another Studio"}},
                      {"subject": {"name": "Another Example"}}):
            items = [candidate(), candidate(id="candidate-2", **patch)]
            provider = MockJev()
            result = await classify_analyzed_batch(self.batch(items), [analyzed(i, imageAnalysis=valid_image()) for i in items], provider)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(len(result.ranked_candidate_ids), 2)

    async def test_same_url_changed_bytes_and_unverifiable_hashes_do_not_dedup(self):
        for image in (valid_image(contentSha256="b" * 64), valid_image(contentSha256="unverified"),
                      ImageAnalysis(), ImageAnalysis(status="invalid", valid=False, contentSha256="a" * 64)):
            items = [candidate(), candidate(id="candidate-2")]
            provider = MockJev()
            result = await classify_analyzed_batch(self.batch(items),
                                                  [analyzed(items[0], imageAnalysis=valid_image()), analyzed(items[1], imageAnalysis=image)], provider)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(len(result.ranked_candidate_ids), 2)

    async def test_failed_canonical_classification_does_not_hide_duplicate_review(self):
        class Failed(MockJev):
            async def decide(self, state, questions):
                self.calls.append(state)
                raise JevFailure("jev_rate_limited", "Rate limited.", retryable=True)
        items = [candidate(), candidate(id="candidate-2")]
        provider = Failed()
        result = await classify_analyzed_batch(self.batch(items), [analyzed(i, imageAnalysis=valid_image()) for i in items], provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.ranked_candidate_ids, ["candidate-1"])
        self.assertEqual(result.review_candidate_ids, ["candidate-1", "candidate-2"])
        self.assertEqual(result.candidates[1].errors[0].code, "jev_rate_limited")

    async def test_duplicate_keeps_its_own_image_review_flags(self):
        items = [candidate(), candidate(id="candidate-2")]
        originals = [analyzed(items[0], imageAnalysis=valid_image()),
                     analyzed(items[1], imageAnalysis=valid_image(), errors=[Failure(
                         stage="storage", code="crop_storage_failed", message="Crop could not be stored.")])]
        result = await classify_analyzed_batch(self.batch(items), originals, MockJev())
        self.assertEqual(result.review_candidate_ids, ["candidate-2"])
        self.assertIn("crop_storage_failed", result.candidates[1].review_reasons)

    async def test_batch_deadline_preserves_completed_and_unfinished_results(self):
        items = [candidate(id=f"candidate-{i}", pageTitle=str(i)) for i in range(3)]
        cancelled = asyncio.Event()
        class Slow(MockJev):
            async def decide(self, state, questions):
                if state["pageFields"]["pageTitle"] == "0":
                    return await super().decide(state, questions)
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        result = await classify_analyzed_batch(self.batch(items), [analyzed(i) for i in items], Slow(), deadline_seconds=0.02)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.candidates[0].classification.status, "ok")
        for item in result.candidates[1:]:
            self.assertEqual(item.classification.relevance, "unknown")
            self.assertEqual(item.errors[0].code, "classification_batch_timeout")
        self.assertEqual(len(result.ranked_candidate_ids), 3)

    async def test_external_batch_cancellation_propagates(self):
        started = asyncio.Event()
        class Waiting(MockJev):
            async def decide(self, *_):
                started.set()
                await asyncio.Event().wait()
        item = candidate()
        task = asyncio.create_task(classify_analyzed_batch(self.batch([item]), [analyzed(item)], Waiting()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_full_batch_is_validated_before_any_provider_call(self):
        item = candidate()
        provider = MockJev()
        wrong_source = analyzed(item)
        wrong_source.source_url = "https://example.invalid/wrong"
        for results in ([], [analyzed(item), analyzed(item)], [wrong_source]):
            with self.subTest(results=len(results)), self.assertRaises(ValueError):
                await classify_analyzed_batch(self.batch([item]), results, provider)
        self.assertEqual(provider.calls, [])

    async def test_input_order_is_retained_when_stage_a_results_are_reordered(self):
        items = [candidate(id=f"candidate-{i}") for i in range(2)]
        result = await classify_analyzed_batch(self.batch(items), [analyzed(i) for i in reversed(items)], MockJev())
        self.assertEqual([r.id for r in result.candidates], [i.id for i in items])

    async def test_stage_a_and_input_objects_are_not_mutated(self):
        item = candidate()
        original = analyzed(item)
        before_item, before_result = item.model_dump(), original.model_dump()
        await classify_analyzed_batch(self.batch([item]), [original], MockJev())
        self.assertEqual(item.model_dump(), before_item)
        self.assertEqual(original.model_dump(), before_result)

    async def test_invalid_deadline_is_rejected_before_calls(self):
        provider = MockJev()
        for value in (0, -1, 121, float("inf"), float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await classify_analyzed_batch(self.batch([]), [], provider, deadline_seconds=value)
        self.assertEqual(provider.calls, [])

    def test_smoke_requires_explicit_live_flag(self):
        with patch("sys.argv", ["stage_b"]), patch("candidate_processing.stage_b.asyncio.run") as run:
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    async def test_smoke_without_credentials_reports_pending_live_acceptance(self):
        output = StringIO()
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}), redirect_stdout(output):
            code = await _run_live_smoke()
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["liveProviderResponseValidated"])
        self.assertEqual(payload["errors"][0]["code"], "jev_credentials_missing")

    @unittest.skipUnless(DEFAULT_MODEL_PATH.is_file(), "MediaPipe model not downloaded")
    async def test_real_stage_a_blank_image_to_mocked_stage_b(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalAssetStore(directory)
            stream = BytesIO()
            with Image.new("RGB", (80, 60), "white") as image:
                image.save(stream, format="PNG")
            asset = store.put(stream.getvalue(), scope="synthetic-test", mime_type="image/png")
            item = candidate(image={"status": "available", "assetId": asset.id})
            stage_a = await IsolatedImageAnalyzer().analyze_candidate(item, store, scope="synthetic-test")
            result = await classify_analyzed_batch(self.batch([item]), [stage_a], MockJev())
            self.assertEqual(result.candidates[0].image_analysis.face_count, 0)
            self.assertEqual(result.candidates[0].classification.relevance, "high")
            self.assertIn("no_face_detected", result.candidates[0].review_reasons)
            self.assertEqual(result.candidates[0].asset_id, asset.id)
            self.assertEqual(BatchResult.model_validate_json(result.model_dump_json(by_alias=True)), result)


if __name__ == "__main__":
    unittest.main()
