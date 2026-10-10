"""CLI: python3 -m pipeline {run,stage,smoke-search,purge,set-key}.

Search uses SerpApi unless `run --mock` (or SEARCH_BACKEND=mock) picks the local
mock internet. `set-key serpapi` prompts for the key with hidden input and saves
it to .env.

Exit codes: 0 done, 1 refused (consent, capture, or artifact error),
2 bad configuration or usage, 3 stopped because a requested stage isn't built yet.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path

from pipeline.artifacts import (
    ArtifactError, create_run, invalidate_from, log_event, purge_run, read_artifact, resolve_run_dir, write_artifact,
)
from pipeline.capture import CaptureError, ConsentError, load_capture, recheck_consent
from pipeline.config import REPO_ROOT, SECRET_ENV, SECRET_PATTERN, ConfigError, Settings, load_settings, set_env_value
from pipeline.contracts import CaptureArtifact
from pipeline.serpapi_account import AccountCheckFailed, KeyRejected, account_quota, describe_quota

STAGE_NAMES = {0: "capture", 1: "search and scrape", 2: "profiles and graph", 3: "verification", 4: "summary"}

# Stage number -> runner(run_dir, settings, refresh) -> console view text. A runner
# reads its prerequisites from the run folder and writes its own artifact; it may
# raise ArtifactError, CaptureError or ConfigError. Each section adds its entry here.
StageRunner = Callable[[Path, Settings, bool], str]
STAGE_RUNNERS: dict[int, StageRunner] = {}
DEFAULT_RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_CAPTURES_DIR = REPO_ROOT / "captures"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
ENV_TEMPLATE = REPO_ROOT / ".env.example"
SERVICE_NAMES = {"serpapi": "SerpApi", "typesafe": "TypeSafe", "openrouter": "OpenRouter", "anthropic": "Anthropic"}

OK, REFUSED, BAD_CONFIG, NOT_BUILT = 0, 1, 2, 3


def fail(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


def not_built(stage: int) -> int:
    return fail(f"Stage {stage} ({STAGE_NAMES[stage]}) is not built yet.", NOT_BUILT)


def run_stages(run_dir: Path, settings: Settings, stages: range, *, refresh: bool) -> int:
    """Run each stage in order, printing its view; stop at the first one not built or failing."""
    for number in stages:
        runner = STAGE_RUNNERS.get(number)
        if runner is None:
            return not_built(number)
        try:
            view = runner(run_dir, settings, refresh)
        except ConfigError as exc:
            return fail(f"Configuration error (stage {number}): {exc}", BAD_CONFIG)
        except ConsentError as exc:
            return fail(f"Refused (consent, stage {number}): {exc}", REFUSED)
        except (ArtifactError, CaptureError) as exc:
            return fail(f"Refused (stage {number}): {exc}", REFUSED)
        if view:
            print(view)
        print(f"Stage {number} done.")
    return OK


def settings_for(args: argparse.Namespace, *, backend: str | None = None, scenario: str | None = None) -> Settings:
    settings = load_settings(backend=backend, models=args.models, scenario=scenario)
    if args.models and not settings.real_models_enabled:
        print("--models given, but JEV_MODE and LLM_MODE are fake/off: no real model calls will be made.")
    return settings


def cmd_run(args: argparse.Namespace) -> int:
    try:
        settings = settings_for(args, backend="mock" if args.mock else None, scenario=args.scenario)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    try:
        loaded = load_capture(args.capture, live=settings.live, models=args.models)
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
    return run_stages(run_dir, settings, range(1, args.until + 1), refresh=args.refresh)


def cmd_stage(args: argparse.Namespace) -> int:
    try:
        run_dir = resolve_run_dir(args.runs_dir, args.run)
        artifact = read_artifact(run_dir, 0)
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    try:
        # The run keeps the backend it was made with, so a SerpApi run needs external consent.
        recheck_consent(artifact, live=artifact.backend == "serpapi", models=args.models)
    except ConsentError as exc:
        return fail(f"Refused (consent): {exc}", REFUSED)
    except CaptureError as exc:
        return fail(f"Refused (capture): {exc}", REFUSED)
    if args.number not in STAGE_RUNNERS:
        return not_built(args.number)
    try:
        # Stage 1 keeps the run's search backend and mock scenario. Stages 2-4 only read saved
        # evidence, so they load search settings as mock and need no SerpApi key; they read the
        # run's backend from 00_capture.json. Model modes come from .env behind --models.
        if args.number == 1 and artifact.backend == "serpapi":
            settings = settings_for(args, backend="serpapi")
        else:
            settings = settings_for(args, backend="mock", scenario=artifact.scenario if artifact.backend == "mock" else None)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    invalidate_from(run_dir, args.number)
    return run_stages(run_dir, settings, range(args.number, args.number + 1), refresh=args.refresh)


def cmd_smoke_search(args: argparse.Namespace) -> int:
    try:
        settings_for(args, backend="serpapi")
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


def cmd_set_key(args: argparse.Namespace) -> int:
    name, service = SECRET_ENV[args.service], SERVICE_NAMES[args.service]
    try:
        value = getpass.getpass(f"Paste your {service} API key (input is hidden), then press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return fail("Cancelled; nothing was saved.", REFUSED)
    if not value:
        return fail("No key entered; nothing was saved.", BAD_CONFIG)
    if not SECRET_PATTERN.fullmatch(value):
        return fail("That doesn't look like an API key (expected only letters, digits, '.', '_' and '-'); "
                    "nothing was saved.", BAD_CONFIG)

    if args.service == "serpapi" and args.check:
        try:
            print(f"SerpApi accepted the key: {describe_quota(account_quota(value))}.")
        except KeyRejected as exc:
            return fail(f"SerpApi rejected the key ({exc}); nothing was saved.", REFUSED)
        except AccountCheckFailed as exc:
            print(f"Warning: couldn't check the key with SerpApi ({exc}); saving it anyway.")

    set_env_value(args.env_file, name, value, template=ENV_TEMPLATE)
    print(f"Saved {name} to {args.env_file}")
    if os.environ.get(name, "").strip():
        print(f"Note: {name} is also exported in this shell, and exported values win over .env. "
              f"Run `unset {name}` to use the saved key.")
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m pipeline", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def gates(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--models", action="store_true", help="allow paid model calls for the modes set in .env")
        sub.add_argument("--refresh", action="store_true", help="bypass this run's search cache (SerpApi only)")

    def runs_dir(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help=argparse.SUPPRESS)

    run = commands.add_parser("run", help="run the pipeline on a capture folder")
    run.add_argument("--capture", type=Path, required=True)
    run.add_argument("--until", type=int, choices=range(5), default=4, metavar="N", help="stop after stage N (0-4)")
    run.add_argument("--mock", action="store_true",
                     help="search the local mock internet instead of SerpApi (no search credits)")
    run.add_argument("--scenario", help="mock scenario (overrides MOCK_SCENARIO; mock search only)")
    gates(run)
    runs_dir(run)
    run.set_defaults(handler=cmd_run)

    stage = commands.add_parser("stage", help="re-run one stage of a saved run")
    stage.add_argument("number", type=int, choices=range(1, 5), metavar="N", help="stage 1-4")
    stage.add_argument("--run", required=True)
    gates(stage)
    runs_dir(stage)
    stage.set_defaults(handler=cmd_stage)

    smoke = commands.add_parser("smoke-search", help="one SerpApi reverse-image search on one image")
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

    set_key = commands.add_parser("set-key", help="prompt for an API key (hidden input) and save it to .env")
    set_key.add_argument("service", choices=SECRET_ENV, help="which key to save")
    set_key.add_argument("--no-check", dest="check", action="store_false",
                         help="save without checking a SerpApi key against SerpApi's free Account API")
    set_key.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help=argparse.SUPPRESS)
    set_key.set_defaults(handler=cmd_set_key)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
