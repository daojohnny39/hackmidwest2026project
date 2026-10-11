"""Export public JSON Schema files without importing processing/network modules."""

import argparse
import json
from pathlib import Path

from .contracts import BatchResult, CandidateBatch


def export_contracts(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    for filename, model in (("member3-input.schema.json", CandidateBatch), ("member3-output.schema.json", BatchResult)):
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["description"] = "FaceTrace Member 3 v1 structural schema. Cross-field and state validation also runs in candidate_processing.contracts."
        (destination / filename).write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent.parent / "contracts")
    export_contracts(parser.parse_args().output_dir)
