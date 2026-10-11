"""Pipeline settings from the environment, .env, and CLI switches (spec 0.3).

Precedence is CLI options, then exported variables, then .env; empty values count
as unset. Search defaults to backend V3 (rekognition) with V5's engines: Trace face search and
SerpApi's Yandex and Bing, every result checked by Amazon Rekognition. It needs TRACE_API_KEY for
Trace, SERPAPI_KEY for the SerpApi engines, GOOGLE_VISION_API_KEY for Cloud Vision (V6), AWS_REGION,
and AWS credentials.
FaceCheck (needs FACECHECK_API_KEY) and SerpApi's Yandex alone (needs SERPAPI_KEY) are
the other live backends. The mock internet is opt-in (`backend="mock"`, `--mock`, or
SEARCH_BACKEND=mock). Real Jev/LLM modes still need `models=True`. Secrets stay out of repr, describe(), and every error
message. No model or AWS SDK is imported here.
"""

from __future__ import annotations

import io
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from mock_internet.client import MockInternetClient
from pipeline.platforms import PLATFORM_DOMAINS

REPO_ROOT = Path(__file__).resolve().parent.parent

MOCK_SCENARIOS = ("default", "ambiguous", "stale", "duplicates", "empty", "rate_limited", "server_error")
TRUST_TABLE = {0: 0, 1: 50, 2: 95, 3: 99}  # key 3 means "3 or more" (spec 2c)

SEARCH_BACKENDS = ("rekognition", "facecheck", "serpapi", "mock")
DEFAULT_SEARCH_BACKEND = "rekognition"
# Search the real internet; the run needs external_processing consent.
LIVE_BACKENDS = ("rekognition", "facecheck", "serpapi")
IMAGE_HOSTS = ("tunnel", "s3")  # how SerpApi runs give the search engine a public image URL (hosting.py)
# Engines backend V3 can search: Trace takes an upload, the SerpApi engines need a hosted image URL,
# and Cloud Vision takes the image's bytes.
SERPAPI_ENGINES = ("yandex_images", "google_lens", "bing_reverse_image")
REKOGNITION_ENGINES = ("trace", *SERPAPI_ENGINES, "cloud_vision")
# V5 replaces V3's Google Lens with Bing. V6 is trace,yandex_images,cloud_vision; V3 and V4 were
# trace,yandex_images,google_lens.
DEFAULT_REKOGNITION_ENGINES = ("trace", "yandex_images", "bing_reverse_image")
DEFAULT_MIN_SIMILARITY = 80.0  # CompareFaces' own default
# Platforms REKOGNITION_PLATFORMS may keep; unset keeps every site. V3_1 is linkedin,facebook.
SEARCH_PLATFORMS = tuple(dict.fromkeys(PLATFORM_DOMAINS.values()))
DEFAULT_IMAGE_HOST = "tunnel"
JEV_MODES = ("fake", "typesafe", "openrouter", "off")
LLM_MODES = ("off", "anthropic", "bedrock")
JEV_DEFAULT_MODELS = {"typesafe": "jev-latest", "openrouter": "typesafe/jev-1.13"}
LLM_DEFAULT_MODELS = {"anthropic": "claude-opus-5-5"}  # Bedrock IDs differ, so it has no default
# Effort for the Claude models that think by default (llm.ADAPTIVE_THINKING_MODELS). V4 is Opus 5.5 at
# medium; V3 ran Sonnet 5.5 at low (LLM_MODEL=claude-sonnet-5-5, LLM_EFFORT=low).
LLM_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_LLM_EFFORT = "medium"
MAX_MOCK_DELAY_MS = 5000

ENV_VARS = (
    "SEARCH_BACKEND", "MOCK_INTERNET_URL", "MOCK_SCENARIO", "MOCK_DELAY_MS", "SERPAPI_KEY",
    "FACECHECK_API_KEY", "FACECHECK_DEMO", "TRACE_API_KEY", "GOOGLE_VISION_API_KEY", "REKOGNITION_ENGINES",
    "REKOGNITION_MIN_SIMILARITY", "REKOGNITION_PLATFORMS",
    "IMAGE_HOST", "IMAGE_BUCKET", "AWS_REGION", "JEV_MODE", "JEV_MODEL", "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY", "LLM_MODE", "LLM_MODEL", "LLM_EFFORT", "ANTHROPIC_API_KEY",
)


# `python -m pipeline set-key <name>` stores these in .env.
SECRET_ENV = {
    "serpapi": "SERPAPI_KEY",
    "facecheck": "FACECHECK_API_KEY",
    "trace": "TRACE_API_KEY",
    "google-vision": "GOOGLE_VISION_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
# Characters a key may use so it can sit unquoted on one .env line.
SECRET_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class ConfigError(ValueError):
    """Invalid or incomplete settings. Messages name variables, never secret values."""


@dataclass(frozen=True)
class Settings:
    search_backend: Literal["mock", "serpapi", "facecheck", "rekognition"]
    mock_internet_url: str
    mock_scenario: str
    mock_delay_ms: int
    serpapi_key: str | None = field(repr=False)
    facecheck_api_key: str | None = field(repr=False)
    facecheck_demo: bool
    trace_api_key: str | None = field(repr=False)
    google_vision_api_key: str | None = field(repr=False)
    rekognition_engines: tuple[str, ...]
    rekognition_min_similarity: float
    rekognition_platforms: tuple[str, ...]  # empty: every site
    image_host: Literal["tunnel", "s3"]
    image_bucket: str | None
    aws_region: str | None
    jev_mode: Literal["fake", "typesafe", "openrouter", "off"]
    jev_model: str | None
    typesafe_api_key: str | None = field(repr=False)
    openrouter_api_key: str | None = field(repr=False)
    llm_mode: Literal["off", "anthropic", "bedrock"]
    llm_model: str | None
    llm_effort: str | None
    anthropic_api_key: str | None = field(repr=False)
    live: bool
    models: bool

    def trust_for(self, count: int) -> int:
        if count < 0:
            raise ValueError(f"connection count must be >= 0, got {count}")
        return TRUST_TABLE[min(count, max(TRUST_TABLE))]

    @property
    def serpapi_engines(self) -> tuple[str, ...]:
        """The SerpApi engines this run searches: Yandex alone for V1, the configured ones for V3-V6."""
        if self.search_backend == "serpapi":
            return ("yandex_images",)
        if self.search_backend == "rekognition":
            return tuple(engine for engine in self.rekognition_engines if engine in SERPAPI_ENGINES)
        return ()

    @property
    def needs_hosting(self) -> bool:
        """Whether a search hosts the image for SerpApi (hosting.py)."""
        return bool(self.serpapi_engines)

    @property
    def real_models_enabled(self) -> bool:
        return self.jev_mode in JEV_DEFAULT_MODELS or self.llm_mode != "off"

    def describe(self) -> dict:
        """Non-secret summary for printing and run logs."""
        summary = {f.name: getattr(self, f.name) for f in fields(self) if f.repr}
        summary["real_models_enabled"] = self.real_models_enabled
        return summary


def load_settings(
    *,
    backend: str | None = None,
    models: bool = False,
    scenario: str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = REPO_ROOT / ".env",
) -> Settings:
    env = _read_env(os.environ if environ is None else environ, dotenv_path)

    if backend is None:
        backend = _choice(env, "SEARCH_BACKEND", SEARCH_BACKENDS, DEFAULT_SEARCH_BACKEND)
    elif backend not in SEARCH_BACKENDS:
        raise ConfigError(f"unknown search backend {backend!r}; expected one of {', '.join(SEARCH_BACKENDS)}")
    live = backend in LIVE_BACKENDS

    mock_url = env.get("MOCK_INTERNET_URL", "http://127.0.0.1:8765")
    try:
        MockInternetClient(mock_url)  # same rule the mock client enforces; makes no connection
    except ValueError:
        # Never echo the URL: it could carry credentials in its userinfo.
        raise ConfigError(
            "MOCK_INTERNET_URL must be an HTTP loopback origin with a valid port, e.g. http://127.0.0.1:8765"
        ) from None

    if scenario is not None:
        if live:
            raise ConfigError("--scenario applies to mock search only; add --mock to use it")
        if scenario not in MOCK_SCENARIOS:
            raise ConfigError(f"unknown scenario {scenario!r}; expected one of {', '.join(MOCK_SCENARIOS)}")
    # MOCK_SCENARIO is validated only when it's used, so a CLI scenario or live search overrides a bad one.
    if live:
        mock_scenario = "default"
    elif scenario is not None:
        mock_scenario = scenario
    else:
        mock_scenario = _choice(env, "MOCK_SCENARIO", MOCK_SCENARIOS, "default")

    delay = _delay(env.get("MOCK_DELAY_MS", "0"))
    image_host = _choice(env, "IMAGE_HOST", IMAGE_HOSTS, DEFAULT_IMAGE_HOST)
    facecheck_demo = _flag(env, "FACECHECK_DEMO")
    engines = _engines(env.get("REKOGNITION_ENGINES", ",".join(DEFAULT_REKOGNITION_ENGINES)))
    min_similarity = _similarity(env.get("REKOGNITION_MIN_SIMILARITY", str(DEFAULT_MIN_SIMILARITY)))
    platforms = _platforms(env.get("REKOGNITION_PLATFORMS", ""))

    jev_mode = _choice(env, "JEV_MODE", JEV_MODES, "fake")
    llm_mode = _choice(env, "LLM_MODE", LLM_MODES, "off")
    if not models:
        real = [f"JEV_MODE={jev_mode}"] if jev_mode in JEV_DEFAULT_MODELS else []
        real += [f"LLM_MODE={llm_mode}"] if llm_mode != "off" else []
        if real:
            raise ConfigError(
                f"{' and '.join(real)} makes paid model calls; pass --models to allow them, "
                "or set JEV_MODE=fake and LLM_MODE=off"
            )

    required: dict[str, list[str]] = {}
    # Hosting settings (ssh for tunnel, IMAGE_BUCKET for s3) are checked by
    # hosting.require_hosting before a SerpApi search, and AWS credentials by
    # rekognition_backend.require_rekognition, not here.
    uses_serpapi = backend == "serpapi" or (backend == "rekognition" and any(e in SERPAPI_ENGINES for e in engines))
    uses_trace = backend == "rekognition" and "trace" in engines
    uses_vision = backend == "rekognition" and "cloud_vision" in engines
    if uses_serpapi:
        required.setdefault("SERPAPI_KEY", []).append("SerpApi search")
    if uses_trace:
        required.setdefault("TRACE_API_KEY", []).append("Trace face search")
    if uses_vision:
        required.setdefault("GOOGLE_VISION_API_KEY", []).append("Cloud Vision web detection")
    if backend == "rekognition":
        required.setdefault("AWS_REGION", []).append("Rekognition face matching")
    elif backend == "facecheck":
        required.setdefault("FACECHECK_API_KEY", []).append("FaceCheck search")
    if jev_mode == "typesafe":
        required.setdefault("TYPESAFE_API_KEY", []).append("JEV_MODE=typesafe")
    elif jev_mode == "openrouter":
        required.setdefault("OPENROUTER_API_KEY", []).append("JEV_MODE=openrouter")
    if llm_mode == "anthropic":
        required.setdefault("ANTHROPIC_API_KEY", []).append("LLM_MODE=anthropic")
    elif llm_mode == "bedrock":
        for name in ("AWS_REGION", "LLM_MODEL"):
            required.setdefault(name, []).append("LLM_MODE=bedrock")
    missing = [f"{name} (for {', '.join(why)})" for name, why in required.items() if name not in env]
    if missing:
        hint = ""
        if backend == "rekognition":
            saves = [f"the {service} key with `python -m pipeline set-key {command}`"
                     for service, command, name in (("Trace", "trace", "TRACE_API_KEY"),
                                                    ("SerpApi", "serpapi", "SERPAPI_KEY"),
                                                    ("Google Cloud Vision", "google-vision", "GOOGLE_VISION_API_KEY"))
                     if name in required and name not in env]
            if "AWS_REGION" not in env:
                saves.append("AWS keys with `python -m pipeline set-key aws --region <region>`")
            hint = (f" Save {' and '.join(saves)}, change REKOGNITION_ENGINES, set SEARCH_BACKEND=facecheck or "
                    "serpapi for another backend, or pass --mock for the mock internet.")
        elif "SERPAPI_KEY" in required and "SERPAPI_KEY" not in env:
            hint = " Save the SerpApi key with `python -m pipeline set-key serpapi`, or pass --mock for the mock internet."
        elif "FACECHECK_API_KEY" in required and "FACECHECK_API_KEY" not in env:
            hint = (" Save the FaceCheck API token with `python -m pipeline set-key facecheck`, set "
                    "SEARCH_BACKEND=rekognition or serpapi for another backend, or pass --mock for the mock internet.")
        raise ConfigError(
            f"missing required settings: {'; '.join(missing)}. Set them in the environment or .env.{hint}"
        )

    # Keys for routes that are not enabled are dropped, so nothing downstream can use them by accident.
    return Settings(
        search_backend=backend,
        mock_internet_url=mock_url.rstrip("/"),
        mock_scenario=mock_scenario,
        mock_delay_ms=delay,
        serpapi_key=env.get("SERPAPI_KEY") if uses_serpapi else None,
        facecheck_api_key=env.get("FACECHECK_API_KEY") if backend == "facecheck" else None,
        facecheck_demo=facecheck_demo,
        trace_api_key=env.get("TRACE_API_KEY") if uses_trace else None,
        google_vision_api_key=env.get("GOOGLE_VISION_API_KEY") if uses_vision else None,
        rekognition_engines=engines,
        rekognition_min_similarity=min_similarity,
        rekognition_platforms=platforms,
        image_host=image_host,
        image_bucket=env.get("IMAGE_BUCKET"),
        aws_region=env.get("AWS_REGION"),
        jev_mode=jev_mode,
        jev_model=env.get("JEV_MODEL", JEV_DEFAULT_MODELS[jev_mode]) if jev_mode in JEV_DEFAULT_MODELS else None,
        typesafe_api_key=env.get("TYPESAFE_API_KEY") if jev_mode == "typesafe" else None,
        openrouter_api_key=env.get("OPENROUTER_API_KEY") if jev_mode == "openrouter" else None,
        llm_mode=llm_mode,
        llm_model=env.get("LLM_MODEL", LLM_DEFAULT_MODELS.get(llm_mode)) if llm_mode != "off" else None,
        # Validated only when a model runs, like MOCK_SCENARIO.
        llm_effort=_choice(env, "LLM_EFFORT", LLM_EFFORTS, DEFAULT_LLM_EFFORT) if llm_mode != "off" else None,
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") if llm_mode == "anthropic" else None,
        live=live,
        models=models,
    )


def _read_env(environ: Mapping[str, str], dotenv_path: Path | None) -> dict[str, str]:
    """Pipeline variables only; .env fills what the environment leaves unset or empty."""
    values: dict[str, str] = {}
    if dotenv_path is not None:
        # interpolate=False: keys may contain "$", and expansion would read os.environ.
        for name, value in dotenv_values(dotenv_path, interpolate=False).items():
            if name in ENV_VARS and value and value.strip():
                values[name] = value.strip()
    for name in ENV_VARS:
        value = environ.get(name)
        if value and value.strip():
            values[name] = value.strip()
    return values


def _choice(env: Mapping[str, str], name: str, allowed: tuple[str, ...], default: str) -> str:
    value = env.get(name, default)
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} is not supported; expected one of {', '.join(allowed)}")
    return value


def _flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "false").lower()
    if value not in ("true", "false", "1", "0", "yes", "no"):
        raise ConfigError(f"{name}={value!r} is not supported; expected true or false")
    return value in ("true", "1", "yes")


def _engines(raw: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip() for name in raw.split(",") if name.strip()))
    if not names or any(name not in REKOGNITION_ENGINES for name in names):
        raise ConfigError(f"REKOGNITION_ENGINES={raw!r} is not supported; expected a comma-separated list of "
                          f"{', '.join(REKOGNITION_ENGINES)}")
    return names


def _platforms(raw: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip().lower() for name in raw.split(",") if name.strip()))
    if any(name not in SEARCH_PLATFORMS for name in names) or (raw.strip() and not names):
        raise ConfigError(f"REKOGNITION_PLATFORMS={raw!r} is not supported; expected a comma-separated list of "
                          f"{', '.join(SEARCH_PLATFORMS)}, or nothing for every site")
    return names


def _similarity(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not 0 <= value <= 100:  # also false for NaN
        raise ConfigError(f"REKOGNITION_MIN_SIMILARITY must be a number from 0 to 100, got {raw!r}")
    return value


def _delay(raw: str) -> int:
    try:
        delay = int(raw)
    except ValueError:
        raise ConfigError(f"MOCK_DELAY_MS must be an integer from 0 to {MAX_MOCK_DELAY_MS}, got {raw!r}") from None
    if not 0 <= delay <= MAX_MOCK_DELAY_MS:
        raise ConfigError(f"MOCK_DELAY_MS must be from 0 to {MAX_MOCK_DELAY_MS}, got {delay}")
    return delay


def set_env_value(path: Path, name: str, value: str, *, template: Path | None = None) -> None:
    """Set NAME=value in a .env file, keeping the rest of the file byte for byte.

    A missing file starts from `template` (normally .env.example). python-dotenv's own
    parser finds the assignments, so quoted keys, multi-line values, comments, and CRLF
    line endings survive. The first assignment of NAME is replaced and later ones are
    dropped, since python-dotenv would let a later one win. The result is parsed back
    before the file is replaced atomically.
    """
    if name not in ENV_VARS:
        raise ValueError(f"{name} is not a pipeline setting")
    if not SECRET_PATTERN.fullmatch(value):
        # Never echo the value: it is usually a secret.
        raise ValueError(f"{name} may contain only letters, digits, '.', '_' and '-'")

    text = ""
    for source in (path, template):
        if source is not None and source.exists():
            with open(source, encoding="utf-8", newline="") as stream:
                text = stream.read()
            break
    newline = "\r\n" if "\r\n" in text else "\n"
    parts, placed = [], False
    for binding in parse_stream(io.StringIO(text)):
        original = binding.original.string
        if binding.key != name:
            parts.append(original)
        elif not placed:
            ending = original[len(original.rstrip("\r\n")):]  # keep the line's own LF or CRLF
            parts.append(f"{name}={value}{ending}")
            placed = True
    if not placed:
        if parts and not parts[-1].endswith("\n"):
            parts.append(newline)
        parts.append(f"{name}={value}{newline}")
    updated = "".join(parts)
    if dotenv_values(stream=io.StringIO(updated), interpolate=False).get(name) != value:
        raise ValueError(f"couldn't update {name} in {path.name} safely; edit it by hand")

    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp asks for mode 0600, which protects the key on Linux filesystems. On a Windows
    # drive (/mnt/c) Linux modes aren't enforced and the file inherits the folder's Windows
    # ACLs. The temp name matches .gitignore's `.env.*`, so a crash can't leave an
    # unignored copy of the key.
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.set-key-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
