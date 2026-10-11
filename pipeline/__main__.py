"""CLI: python3 -m pipeline {run,stage,smoke-search,serve,purge,set-key}.

Search uses backend V3 with V5's engines (Trace face search and SerpApi's Yandex and Bing, faces
checked by Amazon Rekognition) unless
SEARCH_BACKEND=facecheck picks FaceCheck face search, SEARCH_BACKEND=serpapi picks Yandex
alone through SerpApi, or `run --mock` (or SEARCH_BACKEND=mock) picks the local mock internet.
`set-key {trace,serpapi,google-vision,facecheck,typesafe,openrouter,anthropic}` prompts for a key
with hidden input and saves it to .env; Trace, SerpApi, Google Cloud Vision, FaceCheck and
TypeSafe keys are checked first.
`set-key aws` saves an AWS access key pair to ~/.aws/credentials after a free STS check.
`serve` runs the website's HTTP API on 127.0.0.1 (server.py): each uploaded photo runs stages 0-1
and comes back as a list of sources with a confidence level. `serve --fake-db` is dev mode: it
searches the team's fake image database (FAKE_DB_URL) instead of the internet, and marks every site
benign except one fake fraud site (search/fakedb_backend.py).

Exit codes: 0 done, 1 refused (consent, capture, or artifact error),
2 bad configuration or usage, 3 stopped because a requested stage isn't built yet.

Each stage of a run appends to runs/<run_id>/trace.jsonl (trace.py): agent reasoning,
searches, fetches, model and Jev calls, and the cause of every error.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

from pipeline import aws_credentials
from pipeline.artifacts import (
    ArtifactError, create_run, invalidate_from, log_event, purge_run, read_artifact, resolve_run_dir, write_artifact,
)
from pipeline.capture import CaptureError, ConsentError, load_capture, recheck_consent
from pipeline.config import (
    DEFAULT_SEARCH_BACKEND, LIVE_BACKENDS, REPO_ROOT, SECRET_ENV, SECRET_PATTERN, ConfigError,
    Settings, load_settings, set_env_value,
)
from pipeline.contracts import CaptureArtifact
from pipeline.jev import KeyCheckFailed as TypeSafeCheckFailed, KeyRejected as TypeSafeKeyRejected, check_typesafe_key
from pipeline.hosting import remote_deleter, require_hosting
from pipeline.serpapi_account import AccountCheckFailed, KeyRejected, account_quota, describe_quota
from pipeline.trace import TRACE_FILE, tracing
from pipeline import stage1_collect, stage2_profiles, stage3_verify, stage4_summarize

STAGE_NAMES = {0: "capture", 1: "search and scrape", 2: "profiles and graph", 3: "verification", 4: "summary"}

# Stage number -> runner(run_dir, settings, refresh) -> console view text. A runner
# reads its prerequisites from the run folder and writes its own artifact; it may
# raise ArtifactError, CaptureError or ConfigError. Each section adds its entry here.
StageRunner = Callable[[Path, Settings, bool], str]
STAGE_RUNNERS: dict[int, StageRunner] = {}
STAGE_RUNNERS[1] = stage1_collect.run
STAGE_RUNNERS[2] = stage2_profiles.run
STAGE_RUNNERS[3] = stage3_verify.run
STAGE_RUNNERS[4] = stage4_summarize.run_stage
DEFAULT_RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_CAPTURES_DIR = REPO_ROOT / "captures"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
ENV_TEMPLATE = REPO_ROOT / ".env.example"
SERVICE_NAMES = {"trace": "Trace", "facecheck": "FaceCheck", "serpapi": "SerpApi", "google-vision": "Google Cloud",
                 "typesafe": "TypeSafe", "openrouter": "OpenRouter",
                 "anthropic": "Anthropic"}
# The .env mode that puts a saved model key to use; real modes also need --models.
KEY_MODES = {"typesafe": "JEV_MODE=typesafe", "openrouter": "JEV_MODE=openrouter", "anthropic": "LLM_MODE=anthropic"}

OK, REFUSED, BAD_CONFIG, NOT_BUILT = 0, 1, 2, 3


def fail(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


def not_built(stage: int) -> int:
    return fail(f"Stage {stage} ({STAGE_NAMES[stage]}) is not built yet.", NOT_BUILT)


def run_stages(run_dir: Path, settings: Settings, stages: range, *, refresh: bool) -> int:
    """Run each stage in order, printing its view; stop at the first one not built or failing.
    Each stage writes its trace to run_dir/trace.jsonl, including any exception that stops it."""
    for number in stages:
        runner = STAGE_RUNNERS.get(number)
        if runner is None:
            return not_built(number)
        try:
            with tracing(run_dir, number, name=STAGE_NAMES[number], settings=settings.describe(), refresh=refresh):
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
        if args.until >= 1:
            require_search(settings)  # before the run folder exists, let alone an upload
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
        llm_effort=settings.llm_effort,
        jev_model=settings.jev_model,
    )
    with tracing(run_dir, 0, name=STAGE_NAMES[0], command="run", settings=settings.describe(), until=args.until,
                 capture_dir=str(loaded.capture_dir), images=[image.model_dump(mode="json") for image in bundle.images],
                 warnings=loaded.warnings):
        write_artifact(run_dir, 0, artifact)
        log_event(run_dir, stage=0, backend=settings.search_backend, status="ok")

    print(f"Run {run_id}")
    print(f"  folder: {run_dir}")
    print(f"  trace: {run_dir / TRACE_FILE}")
    print(f"  subject: {'; '.join(bundle.subject.details()) or '(no details; photos only)'} "
          f"(consent confirmed {bundle.consent.confirmed_at.isoformat()})")
    llm = (f"{settings.llm_mode} ({settings.llm_model}, effort {settings.llm_effort})"
           if settings.llm_mode != "off" else "off")
    engines = f" ({', '.join(settings.rekognition_engines)})" if settings.search_backend == "rekognition" else ""
    print(f"  search: {settings.search_backend}{engines}, scenario {settings.mock_scenario}; "
          f"jev: {settings.jev_mode}; llm: {llm}")
    for image in bundle.images:
        source = image.mock_fixture if image.mock_fixture else image.file
        print(f"  {image.image_id}: {image.angle} ({source})")
    print("Stage 0 done: wrote 00_capture.json")
    return run_stages(run_dir, settings, range(1, args.until + 1), refresh=args.refresh)


def require_search(settings: Settings) -> None:
    """Fail on missing image hosting or AWS credentials, or an unreachable fake database, before a search
    would need them."""
    if settings.needs_hosting:
        require_hosting(settings)
    if settings.search_backend in ("rekognition", "fakedb"):
        from pipeline.search.rekognition_backend import require_rekognition

        require_rekognition(settings)
    if settings.search_backend == "fakedb":
        from pipeline.search.fakedb_backend import check_database

        check_database(settings.fake_db_url)


def cmd_stage(args: argparse.Namespace) -> int:
    try:
        run_dir = resolve_run_dir(args.runs_dir, args.run)
        artifact = read_artifact(run_dir, 0)
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    try:
        # The run keeps the backend it was made with, so a live run needs external consent.
        recheck_consent(artifact, live=artifact.backend != "mock", models=args.models)
    except ConsentError as exc:
        return fail(f"Refused (consent): {exc}", REFUSED)
    except CaptureError as exc:
        return fail(f"Refused (capture): {exc}", REFUSED)
    if args.number not in STAGE_RUNNERS:
        return not_built(args.number)
    try:
        # Stage 1 keeps the run's search backend and mock scenario. Stages 2-4 only read saved
        # evidence, so they load search settings as mock and need no search key; they read the
        # run's backend from 00_capture.json. Model modes come from .env behind --models.
        if args.number == 1 and artifact.backend != "mock":
            settings = settings_for(args, backend=artifact.backend)
        else:
            settings = settings_for(args, backend="mock", scenario=artifact.scenario if artifact.backend == "mock" else None)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    invalidate_from(run_dir, args.number)
    return run_stages(run_dir, settings, range(args.number, args.number + 1), refresh=args.refresh)


def cmd_smoke_search(args: argparse.Namespace) -> int:
    """One live reverse search on one image (spec 1.1): a stage-0 bundle with only that image,
    then deterministic stage 1. Never calls a model. It costs one SerpApi search per engine plus
    about $0.001 per Rekognition comparison (V3), 3 FaceCheck credits (free with
    FACECHECK_DEMO=true), or one SerpApi search (serpapi)."""
    try:
        settings = settings_for(args, backend=args.backend)
        require_search(settings)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    try:
        loaded = load_capture(args.capture, live=True, models=args.models)
    except ConsentError as exc:
        return fail(f"Refused (consent): {exc}", REFUSED)
    except CaptureError as exc:
        return fail(f"Refused (capture): {exc}", REFUSED)
    image = next((image for image in loaded.bundle.images if image.image_id == args.image_id), None)
    if image is None:
        return fail(f"Refused (capture): no image {args.image_id!r} in this capture.", REFUSED)

    settings = dataclasses.replace(settings, llm_mode="off", llm_model=None, llm_effort=None)
    bundle = loaded.bundle.model_copy(update={"images": [image]})
    try:
        run_id, run_dir = create_run(args.runs_dir, bundle.session_id)
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    with tracing(run_dir, 0, name=STAGE_NAMES[0], command="smoke-search", settings=settings.describe(),
                 capture_dir=str(loaded.capture_dir), images=[image.model_dump(mode="json")]):
        write_artifact(run_dir, 0, CaptureArtifact(
            run_id=run_id, capture=bundle, capture_dir=str(loaded.capture_dir), backend=args.backend,
            scenario="default", llm_mode="off", jev_mode=settings.jev_mode, jev_model=settings.jev_model))
        log_event(run_dir, stage=0, backend=args.backend, status="ok", code="smoke_search")
    print(f"Smoke search {run_id}: {image.image_id} ({image.angle}) from {loaded.capture_dir}")
    print(f"  trace: {run_dir / TRACE_FILE}")
    code = run_stages(run_dir, settings, range(1, 2), refresh=args.refresh)
    if code == OK:
        print("Ask the participant which hits, if any, are really theirs; record that in the brain notes.")
        print(f"Delete this run's data when done: python3 -m pipeline purge --run {run_dir}")
    return code


def cmd_serve(args: argparse.Namespace) -> int:
    """The website's HTTP API (server.py) until Ctrl-C. Settings and credentials are checked first, so a
    bad configuration stops here instead of failing every search."""
    try:
        settings = settings_for(args, backend="fakedb" if args.fake_db else "mock" if args.mock else None)
        require_search(settings)
    except ConfigError as exc:
        return fail(f"Configuration error: {exc}", BAD_CONFIG)
    import logging

    import uvicorn

    from pipeline.server import create_app

    # Only the server's own log goes to the console; tracing() sends stage events to each run's trace.jsonl.
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    server_log = logging.getLogger("pipeline.server")
    server_log.addHandler(console)
    server_log.setLevel(logging.INFO)
    server_log.propagate = False
    app = create_app(settings, runs_dir=args.runs_dir, captures_dir=args.captures_dir)
    llm = (f"{settings.llm_mode} ({settings.llm_model}, effort {settings.llm_effort})"
           if settings.llm_mode != "off" else "off")
    engines = f" ({', '.join(settings.rekognition_engines)})" if settings.search_backend == "rekognition" else ""
    if settings.search_backend == "fakedb":
        engines = f" (dev mode: fake image database at {settings.fake_db_url})"
    print(f"Search API on http://127.0.0.1:{args.port}/api/search")
    print(f"  search: {settings.search_backend}{engines}; llm: {llm}")
    if settings.search_backend == "fakedb":
        from pipeline.search.fakedb_backend import FRAUD_SITE

        print(f"  threat levels: dev mode (every site benign except {FRAUD_SITE})")
    print(f"  photos: {args.captures_dir}; runs: {args.runs_dir}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return OK


def cmd_purge(args: argparse.Namespace) -> int:
    try:
        # Purge needs no consent or search key; AWS_REGION only matters if images were uploaded.
        region = load_settings(backend="mock", models=True).aws_region
    except ConfigError:
        region = None
    try:
        report = purge_run(args.runs_dir, args.run, include_capture=args.include_capture,
                           captures_dir=args.captures_dir, delete_remote=remote_deleter(region))
    except ArtifactError as exc:
        return fail(f"Refused (run): {exc}", REFUSED)
    for message in report.messages:
        print(message)
    if report.run_deleted:
        print(f"Deleted {report.run_dir}")
    clean = report.run_deleted and not report.remote_failures and (report.capture_deleted or not args.include_capture)
    return OK if clean else REFUSED


class InputRefused(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def read_hidden(prompt: str) -> str:
    """One line read without echo; raises InputRefused if it can't be hidden or the user cancels."""
    try:
        # getpass falls back to visible input with only a warning; refuse instead, before anything is read.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt).strip()
    except getpass.GetPassWarning:
        raise InputRefused("This terminal can't hide what you type, so set-key won't read the key here. "
                           "Run it from an interactive terminal; nothing was saved.", REFUSED) from None
    except (EOFError, KeyboardInterrupt):
        print()
        raise InputRefused("Cancelled; nothing was saved.", REFUSED) from None


def cmd_set_key(args: argparse.Namespace) -> int:
    if args.service == "aws":
        return set_aws_keys(args)
    if args.region:
        return fail("--region applies to `set-key aws` only.", BAD_CONFIG)
    name, service = SECRET_ENV[args.service], SERVICE_NAMES[args.service]
    try:
        value = read_hidden(f"Paste your {service} API key (input is hidden), then press Enter: ")
    except InputRefused as exc:
        return fail(str(exc), exc.code)
    if not value:
        return fail("No key entered; nothing was saved.", BAD_CONFIG)
    if not SECRET_PATTERN.fullmatch(value):
        return fail("That doesn't look like an API key (expected only letters, digits, '.', '_' and '-'); "
                    "nothing was saved.", BAD_CONFIG)

    if args.service == "facecheck" and args.check:
        # /api/info returns the credit balance and runs no search.
        from pipeline.search.facecheck_backend import InfoCheckFailed, TokenRejected, account_info, describe_info

        try:
            print(f"FaceCheck accepted the token: {describe_info(account_info(value))}.")
        except TokenRejected as exc:
            return fail(f"FaceCheck rejected the token ({exc}); nothing was saved.", REFUSED)
        except InfoCheckFailed as exc:
            print(f"Warning: couldn't check the token with FaceCheck ({exc}); saving it anyway.")
    if args.service == "trace" and args.check:
        # GET /account is free and runs no search.
        from pipeline.search.trace_face import CheckFailed, account_info, describe_account
        from pipeline.search.trace_face import KeyRejected as TraceKeyRejected

        try:
            print(f"Trace accepted the key: {describe_account(account_info(value))}.")
        except TraceKeyRejected as exc:
            return fail(f"Trace rejected the key ({exc}); nothing was saved.", REFUSED)
        except CheckFailed as exc:
            print(f"Warning: couldn't check the key with Trace ({exc}); saving it anyway.")
    if args.service == "google-vision" and args.check:
        # An empty annotate request: Google checks the key but analyzes no image.
        from pipeline.search.cloud_vision import CheckFailed as VisionCheckFailed, KeyRejected as VisionKeyRejected
        from pipeline.search.cloud_vision import check_key

        try:
            print(f"Google accepted the key; {check_key(value)}.")
        except VisionKeyRejected as exc:
            return fail(f"Google rejected the key ({exc}); nothing was saved.", REFUSED)
        except VisionCheckFailed as exc:
            print(f"Warning: couldn't check the key with Google ({exc}); saving it anyway. If the Cloud Vision "
                  "API isn't enabled for the key's project, enable it in the Google Cloud console.")
    if args.service == "serpapi" and args.check:
        try:
            print(f"SerpApi accepted the key: {describe_quota(account_quota(value))}.")
        except KeyRejected as exc:
            return fail(f"SerpApi rejected the key ({exc}); nothing was saved.", REFUSED)
        except AccountCheckFailed as exc:
            print(f"Warning: couldn't check the key with SerpApi ({exc}); saving it anyway.")
    if args.service == "typesafe" and args.check:
        # Lists models: an authenticated request that makes no Jev decision.
        try:
            models = check_typesafe_key(value)
            print(f"TypeSafe accepted the key; models available: {', '.join(models) or 'none listed'}.")
        except TypeSafeKeyRejected as exc:
            return fail(f"TypeSafe rejected the key ({exc}); nothing was saved.", REFUSED)
        except TypeSafeCheckFailed as exc:
            print(f"Warning: couldn't check the key with TypeSafe ({exc}); saving it anyway.")

    set_env_value(args.env_file, name, value, template=ENV_TEMPLATE)
    print(f"Saved {name} to {args.env_file}")
    if args.service in KEY_MODES:
        print(f"To use it, set {KEY_MODES[args.service]} in .env and add --models to `run` or `stage`; "
              "those calls are paid.")
    if args.service == "trace":
        from pipeline.search.trace_face import CREDIT_PACKS

        print(f"Each Trace search costs 1 credit, one per image (packs: {CREDIT_PACKS}). Backend V3 uses Trace "
              "while REKOGNITION_ENGINES includes trace, as it does by default.")
    if args.service == "google-vision":
        print("V6 uses it: set REKOGNITION_ENGINES=trace,yandex_images,cloud_vision in .env. Each image is one "
              "web detection request; the first 1,000 a month are free, then $3.50 per 1,000.")
    if args.service == "facecheck":
        print("Each FaceCheck search costs 3 credits ($0.30). Set FACECHECK_DEMO=true in .env for free demo "
              "searches, which scan only 100,000 faces.")
    if os.environ.get(name, "").strip():
        print(f"Note: {name} is also exported in this shell, and exported values win over .env. "
              f"Run `unset {name}` to use the saved key.")
    return OK


def set_aws_keys(args: argparse.Namespace) -> int:
    """Check an AWS access key pair with STS, save it as the default profile, and optionally set AWS_REGION."""
    if args.region and not aws_credentials.REGION.fullmatch(args.region):
        return fail(f"{args.region!r} isn't an AWS region name (for example us-east-1); nothing was saved.",
                    BAD_CONFIG)
    try:
        key_id = read_hidden("Paste your AWS access key ID (input is hidden), then press Enter: ")
        if aws_credentials.TEMPORARY_KEY_ID.fullmatch(key_id):
            return fail("That's a temporary key ID (ASIA...), which needs a session token and expires. "
                        "Create a long-term access key (AKIA...) for an IAM user; nothing was saved.", BAD_CONFIG)
        if not aws_credentials.ACCESS_KEY_ID.fullmatch(key_id):
            return fail("That doesn't look like an AWS access key ID (20 characters starting with AKIA); "
                        "nothing was saved.", BAD_CONFIG)
        secret = read_hidden("Paste your AWS secret access key (input is hidden), then press Enter: ")
    except InputRefused as exc:
        return fail(str(exc), exc.code)
    if not aws_credentials.SECRET_ACCESS_KEY.fullmatch(secret):
        return fail("That doesn't look like an AWS secret access key (40 characters); nothing was saved.",
                    BAD_CONFIG)

    if args.check:
        try:
            identity = aws_credentials.check_keys(key_id, secret, args.region)
            print(f"AWS accepted the keys: account {identity.account}, {identity.arn}.")
            if identity.is_root:
                print("Warning: these are the account's root-user keys, which can do anything in the account. "
                      "AWS recommends an IAM user's keys instead; delete the root keys once you have those.")
        except aws_credentials.KeyRejected as exc:
            return fail(f"AWS rejected the keys ({exc}); nothing was saved.", REFUSED)
        except aws_credentials.KeyCheckFailed as exc:
            print(f"Warning: couldn't check the keys with AWS ({exc}); saving them anyway.")

    path = args.credentials_file or aws_credentials.default_credentials_file()
    replaced = aws_credentials.save_keys(path, key_id, secret)
    print(f"{'Replaced' if replaced else 'Saved'} the [default] profile in {path} "
          f"(key ending {key_id[-4:]}). Rekognition (backend V3), S3 hosting and Bedrock read it from there.")
    if args.region:
        set_env_value(args.env_file, "AWS_REGION", args.region, template=ENV_TEMPLATE)
        print(f"Saved AWS_REGION={args.region} to {args.env_file}")
    exported = [name for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "AWS_REGION")
                if os.environ.get(name, "").strip() and (name != "AWS_REGION" or args.region)]
    if exported:
        print(f"Note: {', '.join(exported)} {'is' if len(exported) == 1 else 'are'} also exported in this shell "
              f"and win over the saved values. Run `unset {' '.join(exported)}` to use them.")
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m pipeline", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def gates(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--models", action="store_true", help="allow paid model calls for the modes set in .env")
        sub.add_argument("--refresh", action="store_true", help="bypass this run's search cache (live search only)")

    def runs_dir(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help=argparse.SUPPRESS)

    run = commands.add_parser("run", help="run the pipeline on a capture folder")
    run.add_argument("--capture", type=Path, required=True)
    run.add_argument("--until", type=int, choices=range(5), default=4, metavar="N", help="stop after stage N (0-4)")
    run.add_argument("--mock", action="store_true",
                     help="search the local mock internet instead of the live backend (no search credits)")
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

    smoke = commands.add_parser("smoke-search", help="one live reverse-image search on one image")
    smoke.add_argument("--capture", type=Path, required=True)
    smoke.add_argument("--image-id", required=True)
    smoke.add_argument("--backend", choices=LIVE_BACKENDS, default=DEFAULT_SEARCH_BACKEND,
                       help=f"live search backend (default {DEFAULT_SEARCH_BACKEND})")
    gates(smoke)
    runs_dir(smoke)
    smoke.set_defaults(handler=cmd_smoke_search)

    serve = commands.add_parser("serve", help="run the website's search API on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    mode = serve.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true",
                      help="search the local mock internet instead of the live backend (no search credits)")
    mode.add_argument("--fake-db", action="store_true",
                      help="dev mode: search the team's fake image database (FAKE_DB_URL) instead of the internet")
    serve.add_argument("--models", action="store_true", help="allow paid model calls for the modes set in .env")
    serve.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES_DIR, help=argparse.SUPPRESS)
    runs_dir(serve)
    serve.set_defaults(handler=cmd_serve)

    purge = commands.add_parser("purge", help="delete a run's data and any uploaded images")
    purge.add_argument("--run", required=True)
    purge.add_argument("--include-capture", action="store_true",
                       help="also delete the source capture folder under captures/")
    purge.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES_DIR, help=argparse.SUPPRESS)
    runs_dir(purge)
    purge.set_defaults(handler=cmd_purge)

    set_key = commands.add_parser("set-key", help="prompt for an API key (hidden input) and save it to .env "
                                                  "(aws: to ~/.aws/credentials)")
    set_key.add_argument("service", choices=[*SECRET_ENV, "aws"], help="which key to save")
    set_key.add_argument("--no-check", dest="check", action="store_false",
                         help="save without checking the key (Trace's free /account, FaceCheck's free /api/info, "
                              "SerpApi's free Account API, TypeSafe's model list, AWS STS GetCallerIdentity)")
    set_key.add_argument("--region", help="aws only: also save this AWS_REGION to .env (for example us-east-1)")
    set_key.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help=argparse.SUPPRESS)
    set_key.add_argument("--credentials-file", type=Path, default=None, help=argparse.SUPPRESS)
    set_key.set_defaults(handler=cmd_set_key)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
