"""Reproducible contextual relevance evaluation; offline mode is not Jev accuracy."""

import argparse
from collections import Counter
import asyncio
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import tempfile
import time
import unicodedata
from typing import Literal

from pydantic import Field, model_validator

from .contracts import CandidateInput, Contract, Relevance
from .jev_client import JevClient, JevDecision
from .pipeline import analyze_candidates
from .storage import LocalAssetStore

LABELS = ("high", "medium", "low", "unknown")
DEFAULT_CASES = Path(__file__).resolve().parent.parent / "evals/member3/cases.json"


class EvaluationCase(Contract):
    id: str
    split: Literal["development", "heldout"]
    tags: list[str]
    expected_relevance: Relevance
    rationale: str
    candidate: CandidateInput


class EvaluationDataset(Contract):
    dataset_version: str
    label_source: str
    human_reviewed: bool
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Evaluation case IDs must be unique")
        owners = {}
        for case in self.cases:
            if case.candidate.id != case.id or case.candidate.image.status != "unavailable":
                raise ValueError("Evaluation uses matching IDs and text-only unavailable images")
            subject = case.candidate.subject
            identifiers = [subject.name] if subject.name else []
            identifiers.extend(subject.usernames)
            keys = ["page:" + case.candidate.source_url, *[
                "identifier:" + " ".join(unicodedata.normalize("NFKC", identifier).casefold().lstrip("@").split())
                for identifier in identifiers
            ]]
            for key in keys:
                if key in owners and owners[key] != case.split:
                    raise ValueError("Subject/page leakage across development and held-out splits")
                owners[key] = case.split
        return self


class AbstainingOfflineProvider:
    """Fixed mock baseline, independent of expected labels and case text."""

    sdk_version = "offline-mock"

    async def decide(self, _state, questions):
        return JevDecision(model="offline-abstain", choices={
            key: "none" if key.startswith("context_") else "unknown" for key in questions
        })


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(rows: list[dict]) -> dict:
    """Unknown is abstention; relevant abstentions still reduce end-to-end recall."""
    confusion = {expected: {predicted: 0 for predicted in LABELS} for expected in LABELS}
    tp = fp = fn = abstentions = relevant_abstentions = 0
    false_exclusions, simulated_low, deleted = [], [], []
    ambiguous = [row for row in rows if row["expected"] == "unknown"]
    for row in rows:
        expected, predicted = row["expected"], row["predicted"]
        confusion[expected][predicted] += 1
        relevant = expected in {"high", "medium"}
        positive = predicted in {"high", "medium"}
        if expected != "unknown":
            tp += relevant and positive
            fp += expected == "low" and positive
            fn += relevant and not positive
            abstentions += predicted == "unknown"
            relevant_abstentions += relevant and predicted == "unknown"
        if relevant and not row["includedInRankedList"]:
            false_exclusions.append(row["id"])
        if relevant and predicted == "low":
            simulated_low.append(row["id"])
        if not row["retained"]:
            deleted.append(row["id"])
    latencies = sorted(row["latencySeconds"] for row in rows)
    return {
        "caseCount": len(rows), "expectedCounts": dict(Counter(row["expected"] for row in rows)),
        "confusion": confusion, "truePositive": tp, "falsePositive": fp, "falseNegative": fn,
        "precision": _ratio(tp, tp + fp), "recallIncludingAbstentions": _ratio(tp, tp + fn),
        "determinateLabelCount": sum(row["expected"] != "unknown" for row in rows),
        "abstentionsOnDeterminateCases": abstentions, "relevantAbstentions": relevant_abstentions,
        "classificationReviewRate": _ratio(sum(row["classificationReviewRequired"] for row in rows), len(rows)),
        "pipelineReviewRate": _ratio(sum(row["reviewRequired"] for row in rows), len(rows)),
        "ambiguousReviewCoverage": _ratio(sum(row["classificationReviewRequired"] for row in ambiguous), len(ambiguous)),
        "falseExclusionIds": false_exclusions, "simulatedSuppressLowFalseExclusionIds": simulated_low,
        "deletedCandidateIds": deleted,
        "providerErrorCounts": dict(Counter(code for row in rows for code in row["providerErrors"])),
        "latencySeconds": {"mean": _ratio(sum(latencies), len(latencies)),
                           "p95": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None},
    }


async def evaluate(dataset: EvaluationDataset, provider, *, split="all", mode="offline", dataset_sha256=None):
    rows = []
    with tempfile.TemporaryDirectory(prefix="member3-evaluation-") as directory:
        store = LocalAssetStore(directory)
        for case in dataset.cases:
            if split != "all" and case.split != split:
                continue
            start = time.monotonic()
            batch = await analyze_candidates({"schemaVersion": 1, "requestId": case.id,
                                               "candidates": [case.candidate.model_dump(mode="json", by_alias=True)]},
                                              store=store, scope="synthetic-evaluation", provider=provider)
            elapsed = time.monotonic() - start
            result = next((item for item in batch["candidates"] if item["id"] == case.id), None)
            classification = result["classification"] if result else {
                "status": "error", "relevance": "unknown", "sourceType": "unknown", "reasons": [],
                "model": None, "sdkVersion": None, "rubricVersion": "context-v1",
            }
            reasons = {reason["code"] for reason in classification["reasons"]}
            rows.append({
                "id": case.id, "split": case.split, "tags": case.tags, "expected": case.expected_relevance,
                "predicted": classification["relevance"], "status": classification["status"],
                "sourceType": classification["sourceType"], "reviewRequired": result["reviewRequired"] if result else False,
                "classificationReviewRequired": classification["status"] != "ok"
                    or classification["relevance"] in {"medium", "unknown"}
                    or classification["sourceType"] == "unknown"
                    or bool(reasons & {"contradictory_context", "ambiguous_context", "context_truncated"}),
                "retained": result is not None, "includedInRankedList": case.id in batch["rankedCandidateIds"],
                "providerErrors": [error["code"] for error in result["errors"] if error["stage"] == "classification"] if result else [],
                "model": classification["model"], "sdkVersion": classification["sdkVersion"],
                "rubricVersion": classification["rubricVersion"], "latencySeconds": elapsed,
            })
    metrics = summarize(rows)
    return {
        "mode": mode, "syntheticDataOnly": True, "datasetVersion": dataset.dataset_version,
        "datasetSha256": dataset_sha256, "labelSource": dataset.label_source,
        "humanReviewed": dataset.human_reviewed,
        "accuracyAcceptanceComplete": False,
        "limitations": [
            "Offline runs use a fixed abstaining mock, not Jev predictions." if mode == "offline" else "Live run; interpret provider failures independently of relevance.",
            "Provisional agent-authored labels require human review." if not dataset.human_reviewed else "Human review is recorded in dataset metadata.",
            "Small synthetic text-only evaluation does not establish population accuracy, facial identity, or image quality.",
            "Unknown human labels are excluded from binary precision/recall; relevant abstentions count as missed positives.",
            "Every fixture lacks an image, so pipeline review rate includes image-unavailable flags.",
        ],
        "versions": {"pipeline": "member3-v1", "typesafeSdk": version("typesafe-sdk"),
                     "models": sorted({row["model"] for row in rows if row["model"]}), "rubric": "context-v1"},
        "metrics": metrics,
        "bySplit": {value: summarize([row for row in rows if row["split"] == value])
                    for value in ("development", "heldout")},
        "byTag": {tag: summarize([row for row in rows if tag in row["tags"]])
                  for tag in sorted({tag for row in rows for tag in row["tags"]})},
        "cases": rows,
    }


async def _run(args):
    raw = args.cases.read_bytes()
    dataset = EvaluationDataset.model_validate_json(raw)
    settings = {"split": args.split, "mode": args.mode, "dataset_sha256": hashlib.sha256(raw).hexdigest()}
    if args.mode == "live":
        async with JevClient() as provider:
            report = await evaluate(dataset, provider, **settings)
    else:
        report = await evaluate(dataset, AbstainingOfflineProvider(), **settings)
    output = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(json.dumps({"report": str(args.output), "mode": args.mode, "metrics": report["metrics"]}, indent=2))
    else:
        print(output, end="")
    return 1 if args.mode == "live" and report["metrics"]["providerErrorCounts"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--split", choices=["development", "heldout", "all"], default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
