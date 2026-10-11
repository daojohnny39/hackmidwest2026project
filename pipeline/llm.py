"""Strands model factory for the agents in Sections 1 and 4 (spec 0.1, 0.3).

build_model(settings) returns a Strands Model for LLM_MODE=anthropic or bedrock.
Provider SDKs are imported only here and only when called, so offline runs need
no credentials. It never returns a default Agent() model: plain Agent() would
reach Bedrock. Client retries are kept low because callers bound each agent with
their own deadline and fallback.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from pipeline.config import ConfigError, Settings

if TYPE_CHECKING:
    from strands.models import Model

DEFAULT_MAX_TOKENS = 2048
CLIENT_TIMEOUT_S = 30.0
CLIENT_RETRIES = 1

# Claude models that think by default and return a 400 for a forced tool_choice ("any" or "tool").
# Strands forces the structured-output tool when an agent ends its turn without calling it, so these
# models get tool_choice "auto" instead; Strands' added reminder message still asks for the call.
# Thinking counts toward max_tokens, so they get room for it, and low effort keeps it short (Anthropic's
# suggested starting point for extraction and search work).
ADAPTIVE_THINKING_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1")
ADAPTIVE_MAX_TOKENS = 8192
ADAPTIVE_EFFORT = "low"


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

        client_args = {"api_key": settings.anthropic_api_key, "timeout": CLIENT_TIMEOUT_S,
                       "max_retries": CLIENT_RETRIES}
        if settings.llm_model in ADAPTIVE_THINKING_MODELS:
            return _auto_tool_choice_model()(
                client_args=client_args,
                model_id=settings.llm_model,
                max_tokens=max(max_tokens, ADAPTIVE_MAX_TOKENS),
                params={"output_config": {"effort": ADAPTIVE_EFFORT}},
            )
        return AnthropicModel(client_args=client_args, model_id=settings.llm_model, max_tokens=max_tokens)

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


@cache
def _auto_tool_choice_model() -> type:
    """AnthropicModel that sends tool_choice "auto" wherever Strands would force a tool."""
    from strands.models.anthropic import AnthropicModel

    class AutoToolChoiceAnthropicModel(AnthropicModel):
        @staticmethod
        def _format_tool_choice(tool_choice):
            forced = tool_choice is not None and ("any" in tool_choice or "tool" in tool_choice)
            return AnthropicModel._format_tool_choice({"auto": {}} if forced else tool_choice)

    return AutoToolChoiceAnthropicModel
