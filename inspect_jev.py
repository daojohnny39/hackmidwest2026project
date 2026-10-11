"""Show exactly what Jev receives and returns for one mock capture (read-only dev tool).

    python inspect_jev.py <capture> [--scenario default] [--jev fake|typesafe]

<capture> is alex, sam, casey, or unknown (tests/seed_evidence.collect). --jev fake (the
default) needs no key and makes no network call. --jev typesafe makes ONE real, paid
TypeSafe call with TYPESAFE_API_KEY from the environment or .env.

Prints the grouping state and its size, every pair question with its answer, the
call's model, error, and usage, and the profiles the answers produced. Writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from math import comb

from pipeline.config import MOCK_SCENARIOS, ConfigError, load_settings
from pipeline.contracts import ProfilesBundle
from pipeline.jev import MAX_INPUT_CHARS, Jev
from pipeline.stage2_profiles import (
    JOIN_THRESHOLD,
    MAX_ACCOUNTS,
    build_graphs,
    extract_accounts,
    format_profiles,
    group_accounts,
    jev_state,
)
from tests.seed_evidence import collect

CAPTURES = ("alex", "sam", "casey", "unknown")


def build_jev(mode: str) -> Jev:
    if mode == "fake":
        return Jev(load_settings(backend="mock", environ={"JEV_MODE": "fake"}, dotenv_path=None))
    # Real route: the key comes from the environment or .env; LLM_MODE is forced off so only Jev is enabled.
    environ = {**os.environ, "JEV_MODE": "typesafe", "LLM_MODE": "off"}
    try:
        settings = load_settings(backend="mock", models=True, environ=environ)
    except ConfigError as exc:
        sys.exit(f"Can't use --jev typesafe: {exc}\nSave a key with `python -m pipeline set-key typesafe`.")
    return Jev(settings)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", choices=CAPTURES)
    parser.add_argument("--scenario", default="default", choices=MOCK_SCENARIOS)
    parser.add_argument("--jev", default="fake", choices=("fake", "typesafe"))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # names may be non-ASCII; Windows consoles default to cp1252

    jev = build_jev(args.jev)
    raw = collect(args.capture, args.scenario)
    accounts, mentions, extract_flags = extract_accounts(raw)

    # 1. The state Jev receives.
    state = jev_state(accounts)
    state_chars = len(json.dumps(state, ensure_ascii=False))
    section(f"Jev state: {args.capture}/{args.scenario}, jev={args.jev}")
    print(json.dumps(state, indent=2, ensure_ascii=False))
    print(f"\naccounts: {len(accounts)} (cap {MAX_ACCOUNTS})" + (f", flags: {extract_flags}" if extract_flags else ""))
    print(f"state size: {state_chars:,} chars, ~{state_chars // 4:,} tokens")

    # 2. Group, recording the exact request so its questions and full size can be shown.
    sent: list[tuple[dict, dict]] = []
    ask = jev.ask

    def recording_ask(state, questions, **kwargs):
        sent.append((state, questions))
        return ask(state, questions, **kwargs)

    jev.ask = recording_ask
    profiles, calls, group_flags = group_accounts(accounts, jev)

    section("Jev call")
    if not calls:
        print("no call: no plausible pairs were left after mutual links (nothing to ask)")
    for call, (_, questions) in zip(calls, sent):
        request_chars = len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False))
        print(f"provider: {call.provider}  model: {call.model}  error: {call.error}")
        print(f"usage: {call.usage or '(none reported)'}")
        print(f"request size: {request_chars:,} chars, ~{request_chars // 4:,} tokens (limit {MAX_INPUT_CHARS:,} chars)")
        print(f"pairs asked: {len(questions)} of {comb(len(accounts), 2)} possible "
              f"(max {comb(MAX_ACCOUNTS, 2)} at {MAX_ACCOUNTS} accounts)")
        first = next(iter(questions.values()))
        print(f"question template: {json.dumps(first, ensure_ascii=False)}")

        print(f"\nanswers (* = at or above join threshold {JOIN_THRESHOLD:.2f}):")
        if call.error:
            print(f"  none: the call failed ({call.error}), so grouping fell back to the rules")
        scored = sorted(((answer["noul"], qid) for qid, answer in call.answers.items()), key=lambda s: (-s[0], s[1]))
        for probability, qid in scored:
            mark = "*" if probability >= JOIN_THRESHOLD else " "
            print(f"  {mark} {probability:.3f}  {qid}")

    # 3. What the answers produced; pair decisions show which answers actually merged accounts.
    section("Profiles")
    bundle = ProfilesBundle(run_id=raw.run_id, accounts=accounts, mentions=mentions, profiles=profiles,
                            graphs=build_graphs(accounts, profiles), jev_calls=calls, flags=group_flags)
    for line in format_profiles(bundle) or ["(none)"]:
        print(line)
    for profile in profiles:
        for decision in profile.pair_decisions:
            probability = "" if decision.probability is None else f" {decision.probability:.3f}"
            print(f"  {profile.profile_id} joined {decision.a}+{decision.b} by {decision.method}{probability}")
    if group_flags:
        print(f"flags: {', '.join(group_flags)}")


if __name__ == "__main__":
    main()
