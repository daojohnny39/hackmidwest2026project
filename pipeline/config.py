"""Pipeline settings from the environment, .env, and CLI switches (spec 0.3).

Precedence is CLI options, then exported variables, then .env; empty values count
as unset. Search defaults to FaceCheck and needs FACECHECK_API_KEY; SerpApi
(Yandex) is the other live backend and needs SERPAPI_KEY. The mock internet is
opt-in (`backend="mock"`, `--mock`, or SEARCH_BACKEND=mock). Real Jev/LLM modes
still need `models=True`. Secrets stay out of repr, describe(), and every error
message. No model or AWS SDK is imported here.
"""

from __future__ import annotations

import io
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

REPO_ROOT = Path(__file__).resolve().parent.parent

MOCK_SCENARIOS = ("default", "ambiguous", "stale", "duplicates", "empty", "rate_limited", "server_error")
TRUST_TABLE = {0: 0, 1: 50, 2: 95, 3: 99}  # key 3 means "3 or more" (spec 2c)

SEARCH_BACKENDS = ("facecheck", "serpapi", "mock")
DEFAULT_SEARCH_BACKEND = "facecheck"
LIVE_BACKENDS = ("facecheck", "serpapi")  # search the real internet; the run needs external_processing consent
IMAGE_HOSTS = ("tunnel", "s3")  # how SerpApi runs give Yandex a public image URL (hosting.py)
DEFAULT_IMAGE_HOST = "tunnel"
JEV_MODES = ("fake", "typesafe", "openrouter", "off")
LLM_MODES = ("off", "anthropic", "bedrock")
JEV_DEFAULT_MODELS = {"typesafe": "jev-latest", "openrouter": "typesafe/jev-1.13"}
LLM_DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5"}  # Bedrock IDs differ, so it has no default
MAX_MOCK_DELAY_MS = 5000

ENV_VARS = (
    "SEARCH_BACKEND", "MOCK_INTERNET_URL", "MOCK_SCENARIO", "MOCK_DELAY_MS", "SERPAPI_KEY",
    "FACECHECK_API_KEY", "FACECHECK_DEMO", "IMAGE_HOST", "IMAGE_BUCKET", "AWS_REGION", "JEV_MODE", "JEV_MODEL", "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY", "LLM_MODE", "LLM_MODEL", "ANTHROPIC_API_KEY",
)


# `python -m pipeline set-key <name>` stores these in .env.
SECRET_ENV = {
    "serpapi": "SERPAPI_KEY",
    "facecheck": "FACECHECK_API_KEY",
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
    search_backend: Literal["mock", "serpapi", "facecheck"]
    mock_internet_url: str
    mock_scenario: str
    mock_delay_ms: int
    serpapi_key: str | None = field(repr=False)
    facecheck_api_key: str | None = field(repr=False)
    facecheck_demo: bool
    image_host: Literal["tunnel", "s3"]
    image_bucket: str | None
    aws_region: str | None
    jev_mode: Literal["fake", "typesafe", "openrouter", "off"]
    jev_model: str | None
    typesafe_api_key: str | None = field(repr=False)
    openrouter_api_key: str | None = field(repr=False)
    llm_mode: Literal["off", "anthropic", "bedrock"]
    llm_model: str | None
    anthropic_api_key: str | None = field(repr=False)
    live: bool
    models: bool

    def trust_for(self, count: int) -> int:
        if count < 0:
            raise ValueError(f"connection count must be >= 0, got {count}")
        return TRUST_TABLE[min(count, max(TRUST_TABLE))]

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
    # hosting.require_hosting before a SerpApi search, not here.
    if backend == "serpapi":
        required.setdefault("SERPAPI_KEY", []).append("SerpApi search")
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
        if "SERPAPI_KEY" in required and "SERPAPI_KEY" not in env:
            hint = " Save the SerpApi key with `python -m pipeline set-key serpapi`, or pass --mock for the mock internet."
        elif "FACECHECK_API_KEY" in required and "FACECHECK_API_KEY" not in env:
            hint = (" Save the FaceCheck API token with `python -m pipeline set-key facecheck`, set "
                    "SEARCH_BACKEND=serpapi for Yandex, or pass --mock for the mock internet.")
        raise ConfigError(
            f"missing required settings: {'; '.join(missing)}. Set them in the environment or .env.{hint}"
        )

    # Keys for routes that are not enabled are dropped, so nothing downstream can use them by accident.
    return Settings(
        search_backend=backend,
        mock_internet_url=mock_url.rstrip("/"),
        mock_scenario=mock_scenario,
        mock_delay_ms=delay,
        serpapi_key=env.get("SERPAPI_KEY") if backend == "serpapi" else None,
        facecheck_api_key=env.get("FACECHECK_API_KEY") if backend == "facecheck" else None,
        facecheck_demo=facecheck_demo,
        image_host=image_host,
        image_bucket=env.get("IMAGE_BUCKET"),
        aws_region=env.get("AWS_REGION"),
        jev_mode=jev_mode,
        jev_model=env.get("JEV_MODEL", JEV_DEFAULT_MODELS[jev_mode]) if jev_mode in JEV_DEFAULT_MODELS else None,
        typesafe_api_key=env.get("TYPESAFE_API_KEY") if jev_mode == "typesafe" else None,
        openrouter_api_key=env.get("OPENROUTER_API_KEY") if jev_mode == "openrouter" else None,
        llm_mode=llm_mode,
        llm_model=env.get("LLM_MODEL", LLM_DEFAULT_MODELS.get(llm_mode)) if llm_mode != "off" else None,
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
