"""Pipeline settings from the environment, .env, and CLI switches (spec 0.3).

Precedence is CLI options, then exported variables, then .env; empty values count
as unset. Environment values alone never permit external calls: live search needs
`live=True` and real Jev/LLM modes need `models=True`. Secrets stay out of repr,
describe(), and every error message. No model or AWS SDK is imported here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

from mock_internet.client import MockInternetClient

REPO_ROOT = Path(__file__).resolve().parent.parent

MOCK_SCENARIOS = ("default", "ambiguous", "stale", "duplicates", "empty", "rate_limited", "server_error")
TRUST_TABLE = {0: 0, 1: 50, 2: 95, 3: 99}  # key 3 means "3 or more" (spec 2c)

SEARCH_BACKENDS = ("mock", "serpapi")
JEV_MODES = ("fake", "typesafe", "openrouter", "off")
LLM_MODES = ("off", "anthropic", "bedrock")
JEV_DEFAULT_MODELS = {"typesafe": "jev-latest", "openrouter": "typesafe/jev-1.13"}
LLM_DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5"}  # Bedrock IDs differ, so it has no default
MAX_MOCK_DELAY_MS = 5000

ENV_VARS = (
    "SEARCH_BACKEND", "MOCK_INTERNET_URL", "MOCK_SCENARIO", "MOCK_DELAY_MS", "SERPAPI_KEY",
    "IMAGE_BUCKET", "AWS_REGION", "JEV_MODE", "JEV_MODEL", "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY", "LLM_MODE", "LLM_MODEL", "ANTHROPIC_API_KEY",
)


class ConfigError(ValueError):
    """Invalid or incomplete settings. Messages name variables, never secret values."""


@dataclass(frozen=True)
class Settings:
    search_backend: Literal["mock", "serpapi"]
    mock_internet_url: str
    mock_scenario: str
    mock_delay_ms: int
    serpapi_key: str | None = field(repr=False)
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
    live: bool = False,
    models: bool = False,
    scenario: str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = REPO_ROOT / ".env",
) -> Settings:
    env = _read_env(os.environ if environ is None else environ, dotenv_path)

    backend = _choice(env, "SEARCH_BACKEND", SEARCH_BACKENDS, "mock")
    if backend == "serpapi" and not live:
        raise ConfigError("SEARCH_BACKEND=serpapi needs --live; without it the pipeline only uses the mock")
    if live:
        backend = "serpapi"

    mock_url = env.get("MOCK_INTERNET_URL", "http://127.0.0.1:8765")
    try:
        MockInternetClient(mock_url)  # same rule the mock client enforces; makes no connection
    except ValueError:
        # Never echo the URL: it could carry credentials in its userinfo.
        raise ConfigError(
            "MOCK_INTERNET_URL must be an HTTP loopback origin with a valid port, e.g. http://127.0.0.1:8765"
        ) from None

    env_scenario = _choice(env, "MOCK_SCENARIO", MOCK_SCENARIOS, "default")
    if scenario is not None:
        if live:
            raise ConfigError("--scenario applies to mock search only and cannot be used with --live")
        if scenario not in MOCK_SCENARIOS:
            raise ConfigError(f"unknown scenario {scenario!r}; expected one of {', '.join(MOCK_SCENARIOS)}")
    mock_scenario = "default" if live else scenario or env_scenario

    delay = _delay(env.get("MOCK_DELAY_MS", "0"))

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
    if live:
        for name in ("SERPAPI_KEY", "IMAGE_BUCKET", "AWS_REGION"):
            required.setdefault(name, []).append("--live")
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
        raise ConfigError(f"missing required settings: {'; '.join(missing)}. Set them in the environment or .env")

    # Keys for routes that are not enabled are dropped, so nothing downstream can use them by accident.
    return Settings(
        search_backend=backend,
        mock_internet_url=mock_url.rstrip("/"),
        mock_scenario=mock_scenario,
        mock_delay_ms=delay,
        serpapi_key=env.get("SERPAPI_KEY") if live else None,
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


def _delay(raw: str) -> int:
    try:
        delay = int(raw)
    except ValueError:
        raise ConfigError(f"MOCK_DELAY_MS must be an integer from 0 to {MAX_MOCK_DELAY_MS}, got {raw!r}") from None
    if not 0 <= delay <= MAX_MOCK_DELAY_MS:
        raise ConfigError(f"MOCK_DELAY_MS must be from 0 to {MAX_MOCK_DELAY_MS}, got {delay}")
    return delay
