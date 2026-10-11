"""Combined Member 2 -> Member 3 -> Member 1 behavior, without external calls."""

import asyncio
import base64
from io import BytesIO
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from pydantic import ValidationError

from candidate_processing.contracts import BatchResult, CandidateBatch, ImageAnalysis
from candidate_processing.image_analysis import ImageProcessingResult, IsolatedImageAnalyzer
from candidate_processing.pipeline import analyze_candidates
from candidate_processing.storage import AssetNotFound, LocalAssetStore
from candidate_processing.strands_adapter import create_candidate_tool
from face_landmarker import DEFAULT_MODEL_PATH
from tests.member3.test_classification import MockJev, candidate


def image_bytes(color="white"):
    output = BytesIO()
    with Image.new("RGB", (64, 64), color) as image:
        image.save(output, format="JPEG")
    return output.getvalue()


def image_report(face_count=0):
    return ImageProcessingResult(analysis=ImageAnalysis(
        status="ok", valid=True, width=64, height=64, mimeType="image/jpeg",
        faceCount=face_count, atDetectorLimit=face_count == 2,
        flags=["quality_thresholds_uncalibrated"],
    ))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalAssetStore(self.temp.name)
        self.asset = self.store.put(image_bytes(), scope="session-a")
        self.analyzer = AsyncMock()
        self.analyzer.analyze.return_value = image_report()
        self.provider = MockJev()

    def item(self, **changes):
        return candidate(image={"status": "available", "assetId": self.asset.id}, **changes)

    def batch(self, items):
        return CandidateBatch(schemaVersion=1, requestId="request-c", captureId="capture-c", candidates=items)

    async def run_batch(self, items, **settings):
        return BatchResult.model_validate(await analyze_candidates(
            self.batch(items), store=self.store, scope=settings.pop("scope", "session-a"),
            analyzer=self.analyzer, provider=self.provider, **settings,
        ))

    async def test_json_roundtrip_preserves_no_face_and_context_independently(self):
        result = await self.run_batch([self.item()])
        self.assertEqual(result, BatchResult.model_validate_json(result.model_dump_json()))
        found = result.candidates[0]
        self.assertEqual(found.asset_id, self.asset.id)
        self.assertEqual(found.image_analysis.face_count, 0)
        self.assertEqual(found.classification.relevance, "high")
        self.assertIn("no_face_detected", found.review_reasons)
        self.assertEqual(result.capture_id, "capture-c")

    async def test_duplicate_records_and_separate_pages_share_vision_only(self):
        other_asset = self.store.put(image_bytes(), scope="session-a")
        items = [self.item(), self.item(id="duplicate", sourceId="other-source"),
                 candidate(id="page-2", sourceUrl="https://example.invalid/another-page",
                           image={"status": "available", "assetId": other_asset.id}),
                 self.item(id="changed-metadata", pageTitle="Updated staff profile")]
        result = await self.run_batch(items)
        self.assertEqual(self.analyzer.analyze.await_count, 1)
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(result.ranked_candidate_ids, [items[0].id, "page-2", "changed-metadata"])
        self.assertEqual(result.candidates[1].duplicate_of, items[0].id)
        self.assertEqual(result.candidates[1].source_id, "other-source")
        self.assertEqual(result.candidates[2].asset_id, other_asset.id)
        self.assertEqual(len(result.candidates), 4)

    async def test_same_url_changed_bytes_runs_vision_again(self):
        asset2 = self.store.put(image_bytes("black"), scope="session-a")
        items = [self.item(), candidate(id="changed", image={"status": "available", "assetId": asset2.id})]
        result = await self.run_batch(items)
        self.assertEqual(self.analyzer.analyze.await_count, 2)
        self.assertEqual(len(result.ranked_candidate_ids), 2)

    async def test_crops_reused_with_originals_preserved_and_private_scope(self):
        report = image_report(1)
        report.crops = ImageProcessingResult.model_validate({**report.model_dump(), "crops": [{
            "faceIndex": 0, "box": [1., 1., 60., 60.], "jpeg": base64.b64encode(image_bytes()).decode(),
        }]}).crops
        self.analyzer.analyze.return_value = report
        result = await self.run_batch([self.item(), self.item(id="second")])
        first, second = result.candidates
        crop = first.image_analysis.crops[0]
        self.assertEqual(crop, second.image_analysis.crops[0])
        self.assertNotEqual(crop.asset_id, self.asset.id)
        self.assertEqual(self.store.get(self.asset.id, scope="session-a")[1], image_bytes())
        with self.assertRaises(AssetNotFound):
            self.store.get(crop.asset_id, scope="session-b")

    async def test_scope_is_checked_before_cache_even_identical_asset(self):
        hidden = self.store.put(image_bytes(), scope="session-b")
        result = await self.run_batch([
            self.item(), candidate(id="hidden", image={"status": "available", "assetId": hidden.id}),
        ])
        self.assertEqual(result.candidates[1].image_analysis.status, "unavailable")
        self.assertEqual(result.candidates[1].image_analysis.content_sha256, None)
        self.assertEqual(result.candidates[1].disposition, "candidate")

    async def test_image_failure_recovers_and_does_not_discard_page_context(self):
        self.analyzer.analyze.side_effect = [RuntimeError("PRIVATE detail"), image_report()]
        result = await self.run_batch([self.item(), self.item(id="recovered")])
        self.assertEqual([r.image_analysis.status for r in result.candidates], ["error", "ok"])
        self.assertTrue(all(r.classification.status == "ok" for r in result.candidates))
        self.assertNotIn("PRIVATE", result.model_dump_json())

    async def test_upstream_failure_and_missing_metadata_are_preserved(self):
        item = candidate(pageTitle=None, description=None, image={"status": "failed", "error": {
            "stage": "retrieval", "code": "download_failed", "message": "Upstream download failed.",
        }})
        result = await self.run_batch([item])
        found = result.candidates[0]
        self.assertEqual(found.classification.status, "skipped")
        self.assertIn("download_failed", found.review_reasons)
        self.assertEqual(self.provider.calls, [])
        self.analyzer.analyze.assert_not_awaited()

    async def test_invalid_batch_has_no_side_effects(self):
        payload = self.batch([self.item()]).model_dump(mode="json", by_alias=True)
        payload["candidates"].append(payload["candidates"][0])
        with self.assertRaises(ValidationError):
            await analyze_candidates(payload, store=self.store, scope="session-a", analyzer=self.analyzer, provider=self.provider)
        self.analyzer.analyze.assert_not_awaited()
        self.assertEqual(self.provider.calls, [])

    async def test_combined_deadline_keeps_completed_images_and_all_ids(self):
        count = 0
        async def analyze(data):
            nonlocal count
            count += 1
            if count == 1:
                return image_report()
            await asyncio.sleep(1)
        self.analyzer.analyze.side_effect = analyze
        asset2 = self.store.put(image_bytes("black"), scope="session-a")
        items = [self.item(), candidate(id="slow", image={"status": "available", "assetId": asset2.id}), self.item(id="unfinished")]
        result = await self.run_batch(items, deadline_seconds=0.05)
        self.assertEqual([r.id for r in result.candidates], [r.id for r in items])
        self.assertEqual(result.candidates[0].image_analysis.status, "ok")
        self.assertEqual(result.candidates[1].errors[0].code, "pipeline_batch_timeout")
        self.assertTrue(all(r.classification.status == "error" for r in result.candidates))
        self.assertEqual(self.provider.calls, [])

    async def test_external_cancellation_propagates(self):
        started = asyncio.Event()
        async def slow(_):
            started.set()
            await asyncio.sleep(5)
        self.analyzer.analyze.side_effect = slow
        task = asyncio.create_task(self.run_batch([self.item()]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.provider.calls, [])

    async def test_completed_classification_survives_combined_deadline(self):
        class SlowSecond(MockJev):
            async def decide(self, state, questions):
                if self.calls:
                    await asyncio.sleep(1)
                return await super().decide(state, questions)
        self.provider = SlowSecond()
        result = await self.run_batch([self.item(), self.item(id="slow-context", pageTitle="Another staff page")], deadline_seconds=0.1)
        self.assertEqual(result.candidates[0].classification.relevance, "high")
        self.assertEqual(result.candidates[1].classification.status, "error")
        self.assertTrue(all(r.image_analysis.status == "ok" for r in result.candidates))
        self.assertEqual(result.candidates[1].errors[-1].code, "classification_batch_timeout")

    async def test_name_collision_does_not_become_identity_verdict(self):
        self.provider = MockJev(relevance="low", effects={"context_1": "conflicts"})
        result = await self.run_batch([self.item(description="Avery Example is a chef, explicitly a different person from the software designer.")])
        found = result.candidates[0]
        self.assertEqual(found.classification.relevance, "low")
        self.assertIn("contradictory_context", found.review_reasons)
        self.assertEqual(result.ranked_candidate_ids, [found.id])
        self.assertNotIn("confidence", found.model_dump_json())

    async def test_cache_is_batch_local_and_digest_comes_from_bytes(self):
        self.analyzer.analyze.return_value.analysis.content_sha256 = "0" * 64
        first = await self.run_batch([self.item()])
        await self.run_batch([self.item()])
        self.assertEqual(self.analyzer.analyze.await_count, 2)
        self.assertEqual(first.candidates[0].image_analysis.content_sha256, self.asset.sha256)

    async def test_missing_credentials_preserves_real_image_result(self):
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}):
            result = await analyze_candidates(self.batch([self.item()]), store=self.store, scope="session-a", analyzer=self.analyzer)
        self.assertTrue(result["candidates"][0]["imageAnalysis"]["valid"])
        self.assertIn("jev_credentials_missing", result["candidates"][0]["reviewReasons"])

    @unittest.skipUnless(DEFAULT_MODEL_PATH.exists(), "local MediaPipe model not downloaded")
    async def test_real_worker_no_face_then_corrupt_image_with_mock_jev(self):
        self.analyzer = IsolatedImageAnalyzer()
        bad = self.store.put(b"not an image", scope="session-a")
        result = await self.run_batch([self.item(), candidate(id="bad", image={"status": "available", "assetId": bad.id})])
        self.assertEqual(result.candidates[0].image_analysis.face_count, 0)
        self.assertEqual(result.candidates[1].image_analysis.status, "invalid")
        self.assertTrue(all(r.classification.status == "ok" for r in result.candidates))

    async def test_strands_actual_tool_schema_dispatch_and_validation(self):
        import jsonschema
        tool = create_candidate_tool(store=self.store, scope="session-a", analyzer=self.analyzer, provider=self.provider)
        payload = self.batch([self.item()]).model_dump(mode="json", by_alias=True)
        schema = tool.tool_spec["inputSchema"]["json"]
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate({"request": payload}, schema)
        self.assertEqual(list(schema["properties"]), ["request"])
        events = [event async for event in tool.stream({"toolUseId": "call-1", "name": "analyze_candidates", "input": {"request": payload}}, {})]
        result = events[-1].tool_result
        self.assertEqual(result["status"], "success")
        BatchResult.model_validate(result["content"][0]["json"])
        error = await tool(request={"PRIVATE": "bad"})
        self.assertEqual(error["status"], "error")
        self.assertNotIn("PRIVATE", json.dumps(error))


if __name__ == "__main__":
    unittest.main()
