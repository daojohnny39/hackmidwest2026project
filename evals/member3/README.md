# Member 3 contextual relevance evaluation

`cases.json` contains 40 fictitious text-only cases: 24 development and 16 held-out,
with no shared subject identifiers/pages across splits. Ten cases contain explicit
same-name/different-person conflicts; ten are sparse. Other cases cover independent
corroboration, username-only evidence, contradictory signals and embedded instructions.
Expected labels and rationales are **agent-authored and provisional**, not human
annotations. Human review and agreed acceptance targets are still required.

The dataset includes ten `high`, five `medium`, ten `low` and fifteen `unknown`
expected labels. Source categories are returned for inspection, but these fixtures
currently evaluate relevance only. They contain no photographs or real people.

## Run

```bash
# No external calls. Uses the actual callable with a fixed abstaining mock.
.venv/bin/python -m candidate_processing.evaluation \
  --output evals/member3/offline-report.json

# Explicit provider calls with TYPESAFE_API_KEY exported in the server process.
# Start with development cases; freeze rubric/labels before inspecting held-out results.
.venv/bin/python -m candidate_processing.evaluation --mode live --split development \
  --output evals/member3/live-development.json
.venv/bin/python -m candidate_processing.evaluation --mode live --split heldout \
  --output evals/member3/live-heldout.json
```

`--cases` supports a reviewed dataset with the same schema. Change `datasetVersion`
and record label authors/review in `labelSource` and `humanReviewed`; do not mark
human review complete until it actually happens. The runner records a SHA-256 of
the exact dataset, split, model, SDK and rubric versions for reproducibility.
Live provider errors produce exit code 1 and remain explicit in the report.
The offline mode is a plumbing/metric baseline; its provider never sees labels.

## Metrics and current findings

Binary relevance treats expected `high`/`medium` as positive, `low` as negative,
and expected `unknown` as indeterminate. Predictions of `unknown` are abstentions.
Relevant abstentions count as missed positives in end-to-end recall. Undefined
precision/recall is `null`, never a misleading zero or perfect score.

Reports include the four-label confusion table, per-label counts, precision,
recall including abstentions, ambiguous-case review coverage, review rates,
provider failures, mean/p95 latency and split/tag breakdowns. They separately
track missing candidate records, relevant candidates omitted from the default
ranked list, and simulated false exclusions if a consumer suppresses `low`.
Each fixture is evaluated as a one-candidate text-only batch; exact-image duplicate
grouping and multi-candidate ordering are covered by integration tests separately.

The saved offline report returns 40 `unknown` decisions and retains all 40
candidates in their default lists. The mock abstains on all 25 determinate cases,
including all 15 relevant cases: recall is 0 and precision is undefined. Review
coverage is 100%; no candidates are lost. These numbers describe the fixed mock,
not Jev performance. The reports separate contextual review from image-driven
review because every evaluation fixture intentionally has no image.

No live accuracy result is available: `TYPESAFE_API_KEY` was absent in the checked
environment. The main failure patterns to inspect after human labeling and a
live run are namesake over-association, sparse-context overconfidence, contradictory
support, instruction-following from page text, and provider failures. Current
automated tests verify conservative application behavior and failure preservation;
they cannot establish how reliably the live model recognizes those patterns.
