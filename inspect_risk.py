"""Show the Jev risk-scoring request and result on demo presence data (dev tool).

    python inspect_risk.py                    # rules + fake Jev, free, no network
    python inspect_risk.py --jev typesafe     # ONE real, paid TypeSafe call
    python inspect_risk.py --show-request     # also print the state + questions sent

The demo person is fictional. The data always holds one HIGH page (a people-search
listing with a phone and street address), so the demo never ends with "all low".
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pipeline.config import ConfigError, load_settings
from pipeline.jev import MAX_INPUT_CHARS, Jev
from pipeline.risk import PresenceItem, risk_questions, risk_state, score_presence

DEMO_SUBJECT = "Jordan Vale"
DEMO_ITEMS = [
    PresenceItem("p1", "https://www.linkedin.com/in/jordanvale",
                 "Jordan Vale - Product Designer", "Product designer in Kansas City. Previously at a design studio."),
    PresenceItem("p2", "https://github.com/jvale", "jvale (Jordan Vale)", "12 public repositories. UI kits and side projects."),
    PresenceItem("p3", "https://www.instagram.com/jordan.vale.kc",
                 "Jordan Vale (@jordan.vale.kc)",
                 "Weekend hikes, my sister Mia's graduation, morning runs along Brush Creek every day at 6am."),
    PresenceItem("p4", "https://x.com/jordanvale", "Jordan Vale on X", "Design takes and conference talks."),
    PresenceItem("p5", "https://www.truepeoplesearch.com/find/person/jordan-vale",
                 "Jordan Vale, age 29, Kansas City MO",
                 "Current address: 4127 Oak Hollow Drive, Kansas City, MO. Phone: (816) 555-0142. Relatives: Mia Vale."),
    PresenceItem("p6", "https://en.wikipedia.org/wiki/Jordan_Vale_(footballer)",
                 "Jordan Vale (footballer)", "English footballer born 1961 who played for Leeds."),
]


def build_jev(mode: str) -> Jev:
    if mode == "fake":
        return Jev(load_settings(backend="mock", environ={"JEV_MODE": "fake"}, dotenv_path=None))
    environ = {**os.environ, "JEV_MODE": "typesafe", "LLM_MODE": "off"}
    try:
        return Jev(load_settings(backend="mock", models=True, environ=environ))
    except ConfigError as exc:
        sys.exit(f"Can't use --jev typesafe: {exc}\nSave a key with `python -m pipeline set-key typesafe`.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jev", default="fake", choices=("fake", "typesafe", "off"))
    parser.add_argument("--show-request", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    state, questions = risk_state(DEMO_SUBJECT, DEMO_ITEMS), risk_questions(DEMO_ITEMS)
    size = len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False))
    print(f"request: {len(DEMO_ITEMS)} pages, {len(questions)} questions, {size:,} chars "
          f"(~{size // 4:,} tokens, limit {MAX_INPUT_CHARS:,} chars)")
    if args.show_request:
        print(json.dumps({"state": state, "questions": questions}, indent=2, ensure_ascii=False))

    jev = None if args.jev == "off" else build_jev(args.jev)
    report = score_presence(DEMO_SUBJECT, DEMO_ITEMS, jev)

    print(f"\n{report.subject}: overall {report.overall_level.upper()}, exposure score {report.overall_score}/100")
    if report.jev_errors:
        print(f"Jev failed ({', '.join(report.jev_errors)}); rules decided alone")
    for r in report.items:
        probs = ""
        if r.jev_probabilities:
            probs = "  P(low/med/high) = " + "/".join(f"{r.jev_probabilities.get(str(i), 0):.2f}" for i in range(3))
        out = "  [EXCLUDED: not this person]" if r.excluded else ""
        print(f"  {r.level.upper():6} {r.site:28} kind={r.kind:16} by {r.method}{probs}{out}")
        for reason in r.reasons:
            print(f"         - {reason}")


if __name__ == "__main__":
    main()
