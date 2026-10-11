"""Metric arithmetic and dataset provenance; these tests make no accuracy claim."""

from collections import Counter
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from candidate_processing.evaluation import (
    AbstainingOfflineProvider, DEFAULT_CASES, EvaluationDataset, evaluate, summarize,
)


def row(identifier, expected, predicted, **changes):
    return {"id": identifier, "expected": expected, "predicted": predicted,
            "classificationReviewRequired": predicted in {"medium", "unknown"},
            "reviewRequired": True, "includedInRankedList": True, "retained": True,
            "providerErrors": [], "latencySeconds": 0.1, **changes}


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_relevant_abstentions_count_as_missed_positives(self):
        report = summarize([
            row("tp", "high", "medium"), row("fp", "low", "high"),
            row("abstain", "high", "unknown"), row("wrong-low", "medium", "low"),
            row("ambiguous", "unknown", "high"), row("negative-abstain", "low", "unknown"),
        ])
        self.assertEqual(report["precision"], 0.5)
        self.assertEqual(report["recallIncludingAbstentions"], 1 / 3)
        self.assertEqual(report["relevantAbstentions"], 1)
        self.assertEqual(report["abstentionsOnDeterminateCases"], 2)
        self.assertEqual(report["determinateLabelCount"], 5)
        self.assertEqual(report["ambiguousReviewCoverage"], 0)
        self.assertEqual(report["simulatedSuppressLowFalseExclusionIds"], ["wrong-low"])

    def test_absent_denominators_are_null_and_exclusions_are_separate(self):
        self.assertIsNone(summarize([])["precision"])
        self.assertIsNone(summarize([])["recallIncludingAbstentions"])
        report = summarize([row("excluded", "high", "unknown", includedInRankedList=False),
                            row("lost", "low", "low", retained=False)])
        self.assertEqual(report["falseExclusionIds"], ["excluded"])
        self.assertEqual(report["deletedCandidateIds"], ["lost"])

    def test_dataset_splits_tags_and_honest_label_provenance(self):
        data = EvaluationDataset.model_validate_json(DEFAULT_CASES.read_bytes())
        self.assertEqual(Counter(case.split for case in data.cases), {"development": 24, "heldout": 16})
        self.assertEqual(sum("name_collision" in case.tags for case in data.cases), 10)
        self.assertEqual(sum("sparse" in case.tags for case in data.cases), 10)
        self.assertFalse(data.human_reviewed)
        self.assertEqual(data.label_source, "agent-authored-provisional")

    async def test_offline_report_labels_mock_and_preserves_every_case(self):
        data = EvaluationDataset.model_validate_json(DEFAULT_CASES.read_bytes())
        report = await evaluate(data, AbstainingOfflineProvider())
        self.assertEqual(report["mode"], "offline")
        self.assertFalse(report["accuracyAcceptanceComplete"])
        self.assertEqual(report["metrics"]["caseCount"], 40)
        self.assertIsNone(report["metrics"]["precision"])
        self.assertEqual(report["metrics"]["recallIncludingAbstentions"], 0)
        self.assertEqual(report["metrics"]["deletedCandidateIds"], [])
        self.assertEqual(report["metrics"]["falseExclusionIds"], [])
        self.assertEqual(report["versions"]["models"], ["offline-abstain"])

    async def test_heldout_only_is_reproducibly_selectable(self):
        data = EvaluationDataset.model_validate_json(DEFAULT_CASES.read_bytes())
        report = await evaluate(data, AbstainingOfflineProvider(), split="heldout")
        self.assertEqual(report["metrics"]["caseCount"], 16)
        self.assertEqual(report["bySplit"]["development"]["caseCount"], 0)

    def test_same_identifier_with_changed_context_cannot_leak_across_splits(self):
        data = EvaluationDataset.model_validate_json(DEFAULT_CASES.read_bytes()).model_dump()
        data["cases"][-1]["candidate"]["subject"]["name"] = data["cases"][0]["candidate"]["subject"]["name"].upper()
        with self.assertRaises(ValidationError):
            EvaluationDataset.model_validate(data)

    async def test_missing_result_is_counted_as_lost_evidence(self):
        data = EvaluationDataset.model_validate_json(DEFAULT_CASES.read_bytes())
        data.cases = data.cases[:1]
        with patch("candidate_processing.evaluation.analyze_candidates", new_callable=AsyncMock,
                   return_value={"candidates": [], "rankedCandidateIds": []}):
            report = await evaluate(data, AbstainingOfflineProvider())
        self.assertEqual(report["metrics"]["deletedCandidateIds"], [data.cases[0].id])
        self.assertEqual(report["metrics"]["falseExclusionIds"], [data.cases[0].id])


if __name__ == "__main__":
    unittest.main()
