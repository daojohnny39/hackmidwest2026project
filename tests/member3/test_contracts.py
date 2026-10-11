"""Contract acceptance and failure cases at the teammate integration boundary."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from candidate_processing.contracts import (
    BatchResult, CandidateBatch, CandidateImage, Classification, FaceCrop,
    ImageAnalysis, Subject,
)
from candidate_processing.export_contracts import export_contracts

ROOT = Path(__file__).resolve().parents[2]


def input_example():
    return json.loads((ROOT / "contracts/examples/member3-input.json").read_text())


def output_example():
    return json.loads((ROOT / "contracts/examples/member3-output.json").read_text())


class ContractTests(unittest.TestCase):
    def test_name_or_username_identifies_subject(self):
        self.assertEqual(Subject(name="Example Person").usernames, [])
        self.assertEqual(Subject(usernames=["example_user"]).name, None)
        for value in ({}, {"context": "A designer"}, {"name": " "}, {"usernames": [" @ "]}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Subject.model_validate(value)

    def test_input_fixture_preserves_source_and_capture_ids(self):
        source = input_example()
        result = CandidateBatch.model_validate(source).model_dump(mode="json", by_alias=True)
        self.assertEqual(result["requestId"], source["requestId"])
        self.assertEqual(result["captureId"], source["captureId"])
        self.assertEqual(result["candidates"][0]["imageUrl"], source["candidates"][0]["imageUrl"])
        self.assertNotIn("request_id", result)

    def test_version_is_required_and_unsupported_versions_fail(self):
        for version in (None, 2, "1", True, 1.0):
            data = input_example()
            if version is None:
                data.pop("schemaVersion")
            else:
                data["schemaVersion"] = version
            with self.subTest(version=version), self.assertRaises(ValidationError):
                CandidateBatch.model_validate(data)

    def test_output_version_rejects_boolean_and_float(self):
        for version in (True, 1.0):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                BatchResult.model_validate({**output_example(), "schemaVersion": version})

    def test_duplicate_ids_and_oversized_batches_fail(self):
        data = input_example()
        data["candidates"] *= 2
        with self.assertRaises(ValidationError):
            CandidateBatch.model_validate(data)
        data["candidates"] = [{**data["candidates"][0], "id": f"candidate-{i}"} for i in range(21)]
        with self.assertRaises(ValidationError):
            CandidateBatch.model_validate(data)

    def test_empty_batch_is_valid(self):
        self.assertEqual(CandidateBatch(schemaVersion=1, requestId="empty", candidates=[]).candidates, [])

    def test_unexpected_fields_and_oversized_metadata_fail(self):
        for patch in ({"identityConfidence": 0.99}, {"description": "x" * 4001}):
            data = input_example()
            data["candidates"][0].update(patch)
            with self.subTest(patch=list(patch)), self.assertRaises(ValidationError):
                CandidateBatch.model_validate(data)

    def test_provenance_urls_are_http_without_credentials(self):
        for url in ("file:///etc/passwd", "https://user:password@example.com/photo", "https://example.com:invalid", " https://example.com", "https://example.com/a\nb"):
            data = input_example()
            data["candidates"][0]["imageUrl"] = url
            with self.subTest(url=url), self.assertRaises(ValidationError):
                CandidateBatch.model_validate(data)

    def test_available_image_requires_asset_and_no_error(self):
        self.assertEqual(CandidateImage(status="available", assetId="example-asset").asset_id, "example-asset")
        with self.assertRaises(ValidationError):
            CandidateImage(status="available")
        with self.assertRaises(ValidationError):
            CandidateImage(status="unavailable", assetId="example-asset")
        with self.assertRaises(ValidationError):
            CandidateImage(status="available", assetId="example-asset", error={
                "stage": "retrieval", "code": "download_failed", "message": "Download failed.",
            })

    def test_failed_download_retains_failure_reason(self):
        error = {"stage": "retrieval", "code": "download_timeout", "message": "The image download timed out.", "retryable": True}
        image = CandidateImage(status="failed", error=error)
        self.assertTrue(image.error.retryable)
        with self.assertRaises(ValidationError):
            CandidateImage(status="failed")
        with self.assertRaises(ValidationError):
            CandidateImage(status="failed", error={**error, "stage": "classification"})

    def test_zero_faces_is_distinct_from_failed_analysis(self):
        valid = ImageAnalysis(status="ok", valid=True, width=640, height=480, faceCount=0)
        failed = ImageAnalysis(status="error")
        self.assertEqual(valid.face_count, 0)
        self.assertIsNone(failed.face_count)
        with self.assertRaises(ValidationError):
            ImageAnalysis(status="error", faceCount=0)
        with self.assertRaises(ValidationError):
            ImageAnalysis(status="ok", valid=True, width=640, height=480)

    def test_invalid_and_unavailable_validity_are_distinct(self):
        self.assertFalse(ImageAnalysis(status="invalid", valid=False).valid)
        self.assertIsNone(ImageAnalysis().valid)
        with self.assertRaises(ValidationError):
            ImageAnalysis(status="invalid", valid=True)

    def test_detector_capacity_is_reported_explicitly(self):
        data = {"status": "ok", "valid": True, "width": 640, "height": 480, "faceCount": 2}
        with self.assertRaises(ValidationError):
            ImageAnalysis.model_validate(data)
        self.assertTrue(ImageAnalysis(**data, atDetectorLimit=True).at_detector_limit)

    def test_crops_have_valid_bounds_and_detected_face_indices(self):
        crop = {"assetId": "crop-1", "faceIndex": 0, "box": [10., 10., 100., 100.]}
        self.assertEqual(FaceCrop.model_validate(crop).coordinate_space, "oriented_image_pixels")
        for box in ([-1., 0., 10., 10.], [0., 0., 0., 10.], [0., 0., float("inf"), 10.]):
            with self.subTest(box=box), self.assertRaises(ValidationError):
                FaceCrop.model_validate({**crop, "box": box})
        with self.assertRaises(ValidationError):
            ImageAnalysis(status="ok", valid=True, width=50, height=50, faceCount=1, crops=[crop])

    def test_nonfinite_quality_measurements_fail(self):
        with self.assertRaises(ValidationError):
            ImageAnalysis(qualityMetrics={"blur": float("nan")})

    def test_failed_classification_cannot_claim_relevance(self):
        self.assertEqual(Classification(status="error").relevance, "unknown")
        with self.assertRaises(ValidationError):
            Classification(status="error", relevance="high")
        with self.assertRaises(ValidationError):
            Classification(status="ok", sourceType="trusted")

    def test_output_fixture_round_trips_without_losing_review_items(self):
        result = BatchResult.model_validate(output_example())
        restored = BatchResult.model_validate_json(result.model_dump_json(by_alias=True))
        self.assertEqual(restored, result)
        self.assertEqual(restored.review_candidate_ids, [item.id for item in restored.candidates if item.review_required])

    def test_rank_and_review_lists_cannot_drop_or_invent_ids(self):
        for field, values in (("rankedCandidateIds", []), ("rankedCandidateIds", ["missing"]), ("reviewCandidateIds", [])):
            data = output_example()
            data[field] = values
            with self.subTest(field=field, values=values), self.assertRaises(ValidationError):
                BatchResult.model_validate(data)

    def test_duplicates_reference_a_preserved_canonical_result(self):
        data = output_example()
        duplicate = copy.deepcopy(data["candidates"][0])
        duplicate.update(id="duplicate-1", disposition="duplicate", duplicateOf=data["candidates"][0]["id"])
        data["candidates"].append(duplicate)
        data["reviewCandidateIds"].append("duplicate-1")
        BatchResult.model_validate(data)
        duplicate["duplicateOf"] = "missing"
        with self.assertRaises(ValidationError):
            BatchResult.model_validate(data)

    def test_exported_schemas_match_checked_in_files(self):
        with tempfile.TemporaryDirectory() as directory:
            export_contracts(Path(directory))
            for name in ("member3-input.schema.json", "member3-output.schema.json"):
                actual = json.loads((Path(directory) / name).read_text())
                expected = json.loads((ROOT / "contracts" / name).read_text())
                self.assertEqual(actual, expected)
                self.assertEqual(actual["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
