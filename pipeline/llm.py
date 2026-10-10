"""Strands model factory for the agents in Sections 1 and 4 (spec 0.1, 0.3).

build_model(settings) returns a Strands Model for LLM_MODE=anthropic or bedrock.
Provider SDKs are imported only here and only when called, so offline runs need
no credentials. It never returns a default Agent() model: plain Agent() would
reach Bedrock. Client retries are kept low because callers bound each agent with
their own deadline and fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.config import ConfigError, Settings

if TYPE_CHECKING:
    from strands.models import Model

DEFAULT_MAX_TOKENS = 2048
CLIENT_TIMEOUT_S = 30.0
CLIENT_RETRIES = 1


def build_model(settings: Settings, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> Model:
    """A fresh Strands model for settings.llm_mode; raises ConfigError if no real model is allowed."""
    if settings.llm_mode == "off":
        raise ConfigError("LLM_MODE=off: no model to build; use the deterministic path")
    if not settings.models:
        raise ConfigError(f"LLM_MODE={settings.llm_mode} makes paid model calls; pass --models to allow them")
    if not settings.llm_model:
        raise ConfigError(f"LLM_MODEL is required for LLM_MODE={settings.llm_mode}")

    if settings.llm_mode == "anthropic":
        if not settings.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is required for LLM_MODE=anthropic")
        from strands.models.anthropic import AnthropicModel

        return AnthropicModel(
            client_args={"api_key": settings.anthropic_api_key, "timeout": CLIENT_TIMEOUT_S,
                         "max_retries": CLIENT_RETRIES},
            model_id=settings.llm_model,
            max_tokens=max_tokens,
        )

    if settings.llm_mode == "bedrock":
        if not settings.aws_region:
            raise ConfigError("AWS_REGION is required for LLM_MODE=bedrock")
        from botocore.config import Config
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=settings.llm_model,
            region_name=settings.aws_region,
            max_tokens=max_tokens,
            # botocore's max_attempts counts retries only, like Anthropic's max_retries.
            boto_client_config=Config(read_timeout=CLIENT_TIMEOUT_S, connect_timeout=10,
                                      retries={"max_attempts": CLIENT_RETRIES, "mode": "standard"}),
        )

    raise ConfigError(f"LLM_MODE={settings.llm_mode!r} is not supported")
