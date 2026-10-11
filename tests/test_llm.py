"""Strands model factory (spec 0.1). Models are constructed only; no request is sent."""

import os
from unittest.mock import patch

from pipeline.config import ConfigError, load_settings
from pipeline.llm import ADAPTIVE_MAX_TOKENS, CLIENT_RETRIES, CLIENT_TIMEOUT_S, build_model
from tests.support import OfflineTestCase

ANTHROPIC = {"LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": "sk-test-not-real"}


def settings(models=True, **env):
    return load_settings(models=models, environ={"SEARCH_BACKEND": "mock", **env}, dotenv_path=None)


class BuildModelTests(OfflineTestCase):
    def test_anthropic(self):
        model = build_model(settings(**ANTHROPIC, LLM_MODEL="claude-haiku-4-5"), max_tokens=512)
        self.assertEqual(type(model).__name__, "AnthropicModel")
        self.assertEqual(model.get_config()["model_id"], "claude-haiku-4-5")
        self.assertEqual(model.get_config()["max_tokens"], 512)
        self.assertIsNone(model.get_config().get("params"))

    def test_sonnet_5_5_default_gets_thinking_room_and_low_effort(self):
        model = build_model(settings(**ANTHROPIC), max_tokens=512)
        config = model.get_config()
        self.assertEqual(config["model_id"], "claude-sonnet-5-5")
        self.assertEqual(config["max_tokens"], ADAPTIVE_MAX_TOKENS)
        request = model.format_request([{"role": "user", "content": [{"text": "hi"}]}])
        self.assertEqual(request["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", request)
        self.assertNotIn("thinking", request)  # adaptive thinking is the default; "disabled" is a 400

    def test_sonnet_5_5_never_forces_a_tool(self):
        # Sonnet 5.5 rejects tool_choice "any"/"tool" with a 400; Strands forces the structured-output tool.
        model = build_model(settings(**ANTHROPIC))
        messages = [{"role": "user", "content": [{"text": "hi"}]}]
        spec = {"name": "Triage", "description": "d", "inputSchema": {"json": {"type": "object"}}}
        for forced in ({"tool": {"name": "Triage"}}, {"any": {}}):
            request = model.format_request(messages, [spec], tool_choice=forced)
            self.assertEqual(request["tool_choice"], {"type": "auto"})
        self.assertNotIn("tool_choice", model.format_request(messages, [spec]))

    def test_bedrock_uses_its_own_model_id(self):
        # Env credentials stop boto3 from probing the instance metadata endpoint.
        fake = {"AWS_ACCESS_KEY_ID": "test-not-real", "AWS_SECRET_ACCESS_KEY": "test-not-real"}
        with patch.dict(os.environ, fake):
            model = build_model(settings(LLM_MODE="bedrock", AWS_REGION="us-east-1", LLM_MODEL="bedrock-model-id"))
        self.assertEqual(type(model).__name__, "BedrockModel")
        self.assertEqual(model.get_config()["model_id"], "bedrock-model-id")
        # Same budget as the Anthropic client: one try plus CLIENT_RETRIES retries.
        client_config = model.client.meta.config
        self.assertEqual(client_config.retries["total_max_attempts"], CLIENT_RETRIES + 1)
        self.assertEqual(client_config.read_timeout, CLIENT_TIMEOUT_S)

    def test_refuses_without_a_real_mode(self):
        with self.assertRaisesRegex(ConfigError, "deterministic"):
            build_model(settings(models=False))
        with self.assertRaisesRegex(ConfigError, "deterministic"):
            build_model(settings(models=True))
