"""Name-first presence check (prototype): search a name, then rate each page's risk.

    python probe_name_search.py "Jun Example" --consent
    python probe_name_search.py "Jun Example" --extra "UMKC" --consent
    python probe_name_search.py "Jun Example" --extra "Kansas City" --consent --jev typesafe

Only search yourself, or someone who said yes: results are real pages about real
people, and people with the same name show up too.

One SerpApi Google search (1 credit, SERPAPI_KEY from .env), then risk scoring with
pipeline/risk.py: rules only by default (free), or --jev typesafe for ONE real Jev call
(it can also mark pages that are about a different person with the same name).
Nothing is saved to disk.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

from pipeline.risk import PresenceItem, score_presence

REPO_ROOT = Path(__file__).resolve().parent


def google_results(query: str, key: str, count: int) -> list[dict]:
    response = httpx.get("https://serpapi.com/search.json", timeout=60, params={
        "engine": "google", "q": query, "num": count, "hl": "en", "api_key": key})
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or payload.get("error"):
        if "hasn't returned any results" in str(payload.get("error", "")):
            return []
        sys.exit(f"search failed: HTTP {response.status_code} {payload.get('error', '')}")
    return [row for row in payload.get("organic_results", []) if isinstance(row, dict) and row.get("link")]


def build_jev(mode: str):
    if mode == "off":
        return None
    from pipeline.config import ConfigError, load_settings
    from pipeline.jev import Jev

    if mode == "fake":
        return Jev(load_settings(backend="mock", environ={"JEV_MODE": "fake"}, dotenv_path=None))
    try:
        return Jev(load_settings(backend="mock", models=True,
                                 environ={**os.environ, "JEV_MODE": "typesafe", "LLM_MODE": "off"}))
    except ConfigError as exc:
        sys.exit(f"Can't use --jev typesafe: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help='the full name in quotes, e.g. "Jun Example"')
    parser.add_argument("--extra", default="", help="words that narrow it down: city, school, employer")
    parser.add_argument("--count", type=int, default=20, help="how many results (default 20)")
    parser.add_argument("--consent", action="store_true", help="this is you, or the person agreed")
    parser.add_argument("--jev", default="off", choices=("off", "fake", "typesafe"))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.consent:
        sys.exit("Refusing: add --consent to confirm this is you or the person agreed to be searched.")

    key = dotenv_values(REPO_ROOT / ".env").get("SERPAPI_KEY")
    if not key:
        sys.exit("Need SERPAPI_KEY in .env")

    query = f'"{args.name}" {args.extra}'.strip()
    rows = google_results(query, key, args.count)
    print(f"search: {query}  ->  {len(rows)} results")
    items = [PresenceItem(item_id=f"p{i + 1}", url=row["link"], title=row.get("title", ""),
                          text=row.get("snippet", "")) for i, row in enumerate(rows)]
    if not items:
        print("nothing found: low exposure for this name (or try --extra with a city or school)")
        return

    report = score_presence(args.name, items, build_jev(args.jev))
    print(f"\n{report.subject}: overall {report.overall_level.upper()}, exposure score {report.overall_score}/100")
    if report.jev_errors:
        print(f"Jev failed ({', '.join(report.jev_errors)}); rules decided alone")
    for r in report.items:
        flag = "  [not this person]" if r.excluded else ""
        print(f"  {r.level.upper():6} {r.site[:30]:30} {r.kind:16} by {r.method}{flag}")
        for reason in r.reasons:
            print(f"         - {reason}")
    print("\nCheck by hand which pages are really about the person; same-name strangers are common.")


if __name__ == "__main__":
    main()
