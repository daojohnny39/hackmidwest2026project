"""CLI: python3 -m pipeline {run,stage,smoke-search,purge}.

Exit codes: 0 done, 1 refused (consent, capture, or artifact error),
2 bad configuration or usage, 3 stopped because a requested stage isn't built yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.artifacts import ArtifactError, create_run, log_event, purge_run, read_artifact, resolve_run_dir, write_artifact
from pipeline.capture import CaptureError, ConsentError, load_capture, recheck_consent
from pipeline.config import REPO_ROOT, ConfigError, Settings, load_settings
from pipeline.contracts import CaptureArtifact

LAST_BUILT_STAGE = 0
STAGE_NAMES = {0: "capture", 1: "search and scrape", 2: "profiles and graph", 3: "verification", 4: "summary"}
DEFAULT_RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_CAPTURES_DIR = REPO_ROOT / "captures"

OK, REFUSED, BAD_CONFIG, NOT_BUILT = 0, 1, 2, 3


def fail(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


def not_built(stage: int) -> int:
    return fail(f"Stage {stage} ({STAGE_NAMES[stage]}) is not built yet.", NOT_BUILT)


def settings_for(args: argparse.Namespace, *, scenario: str | None = None) -> Settings:
    settings = load_settings(live=args.live, models=args.models, scenario=scenario)
    if args.models and not settings.real_models_enabled:
        print("--models given, but JEV_MODE and LLM_MODE are fake/off: no real model calls will be made.")
    return settings


def cmd_run(args: argparse.Namespace) -> int:
    try:
        settings = settings_for(args, scenario=args.scenario)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    try:
        loaded = load_capture(args.capture, live=args.live, models=args.models)
    except ConsentError as exc:
        return fail(f"Refused (consent): {exc}", REFUSED)
    except CaptureError as exc:
        return fail(f"Refused (capture): {exc}", REFUSED)
    for warning in loaded.warnings:
        print(f"Warning: {warning}")

    bundle = loaded.bundle
    try:
        run_id, run_dir = create_run(args.runs_dir, bundle.session_id)
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    artifact = CaptureArtifact(
        run_id=run_id,
        capture=bundle,
        capture_dir=str(loaded.capture_dir),
        backend=settings.search_backend,
        scenario=settings.mock_scenario,
        llm_mode=settings.llm_mode,
        jev_mode=settings.jev_mode,
        llm_model=settings.llm_model,
        jev_model=settings.jev_model,
    )
    write_artifact(run_dir, 0, artifact)
    log_event(run_dir, stage=0, backend=settings.search_backend, status="ok")

    print(f"Run {run_id}")
    print(f"  folder: {run_dir}")
    print(f"  participant: {bundle.consent.participant} (consent confirmed {bundle.consent.confirmed_at.isoformat()})")
    print(f"  search: {settings.search_backend}, scenario {settings.mock_scenario}; "
          f"jev: {settings.jev_mode}; llm: {settings.llm_mode}")
    for image in bundle.images:
        source = image.mock_fixture if image.mock_fixture else image.file
        print(f"  {image.image_id}: {image.angle} ({source})")
    print("Stage 0 done: wrote 00_capture.json")

    if args.until > LAST_BUILT_STAGE:
        return not_built(LAST_BUILT_STAGE + 1)
    return OK


def cmd_stage(args: argparse.Namespace) -> int:
    try:
        run_dir = resolve_run_dir(args.runs_dir, args.run)
        artifact = read_artifact(run_dir, 0)
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    try:
        recheck_consent(artifact, live=args.live, models=args.models)
    except ConsentError as exc:
        return fail(f"Refused (consent): {exc}", REFUSED)
    except CaptureError as exc:
        return fail(f"Refused (capture): {exc}", REFUSED)
    return not_built(args.number)


def cmd_smoke_search(args: argparse.Namespace) -> int:
    if not args.live:
        return fail("smoke-search makes a real SerpApi call; pass --live to allow it.", BAD_CONFIG)
    try:
        settings_for(args)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    try:
        loaded = load_capture(args.capture, live=True, models=args.models)
    except ConsentError as exc:
        return fail(f"Refused (consent): {exc}", REFUSED)
    except CaptureError as exc:
        return fail(f"Refused (capture): {exc}", REFUSED)
    if args.image_id not in {image.image_id for image in loaded.bundle.images}:
        return fail(f"Refused (capture): no image {args.image_id!r} in this capture.", REFUSED)
    return not_built(1)


def cmd_purge(args: argparse.Namespace) -> int:
    try:
        report = purge_run(args.runs_dir, args.run, include_capture=args.include_capture,
                           captures_dir=args.captures_dir)
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    for message in report.messages:
        print(message)
    if report.run_deleted:
        print(f"Deleted {report.run_dir}")
    clean = report.run_deleted and not report.remote_failures and (report.capture_deleted or not args.include_capture)
    return OK if clean else REFUSED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m pipeline", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def gates(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--live", action="store_true", help="allow real image hosting and SerpApi search")
        sub.add_argument("--models", action="store_true", help="allow paid model calls for the modes set in .env")
        sub.add_argument("--refresh", action="store_true", help="bypass this run's search cache (live only)")

    def runs_dir(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help=argparse.SUPPRESS)

    run = commands.add_parser("run", help="run the pipeline on a capture folder")
    run.add_argument("--capture", type=Path, required=True)
    run.add_argument("--until", type=int, choices=range(5), default=4, metavar="N", help="stop after stage N (0-4)")
    run.add_argument("--scenario", help="mock scenario (overrides MOCK_SCENARIO; mock only)")
    gates(run)
    runs_dir(run)
    run.set_defaults(handler=cmd_run)

    stage = commands.add_parser("stage", help="re-run one stage of a saved run")
    stage.add_argument("number", type=int, choices=range(1, 5), metavar="N", help="stage 1-4")
    stage.add_argument("--run", required=True)
    gates(stage)
    runs_dir(stage)
    stage.set_defaults(handler=cmd_stage)

    smoke = commands.add_parser("smoke-search", help="one live reverse-image search on one image")
    smoke.add_argument("--capture", type=Path, required=True)
    smoke.add_argument("--image-id", required=True)
    gates(smoke)
    runs_dir(smoke)
    smoke.set_defaults(handler=cmd_smoke_search)

    purge = commands.add_parser("purge", help="delete a run's data and any uploaded images")
    purge.add_argument("--run", required=True)
    purge.add_argument("--include-capture", action="store_true",
                       help="also delete the source capture folder under captures/")
    purge.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES_DIR, help=argparse.SUPPRESS)
    runs_dir(purge)
    purge.set_defaults(handler=cmd_purge)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
