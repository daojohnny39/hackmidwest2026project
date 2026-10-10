"""Strands model factory (spec 0.1). Models are constructed only; no request is sent."""

import os
from unittest.mock import patch

from pipeline.config import ConfigError, load_settings
from pipeline.llm import build_model
from tests.support import OfflineTestCase


def settings(models=True, **env):
    return load_settings(models=models, environ={"SEARCH_BACKEND": "mock", **env}, dotenv_path=None)


class BuildModelTests(OfflineTestCase):
    def test_anthropic(self):
        model = build_model(settings(LLM_MODE="anthropic", ANTHROPIC_API_KEY="sk-test-not-real"), max_tokens=512)
        self.assertEqual(type(model).__name__, "AnthropicModel")
        self.assertEqual(model.get_config()["model_id"], "claude-sonnet-5-5")
        self.assertEqual(model.get_config()["max_tokens"], 512)

    def test_bedrock_uses_its_own_model_id(self):
        # Env credentials stop boto3 from probing the instance metadata endpoint.
        fake = {"AWS_ACCESS_KEY_ID": "test-not-real", "AWS_SECRET_ACCESS_KEY": "test-not-real"}
        with patch.dict(os.environ, fake):
            model = build_model(settings(LLM_MODE="bedrock", AWS_REGION="us-east-1", LLM_MODEL="bedrock-model-id"))
        self.assertEqual(type(model).__name__, "BedrockModel")
        self.assertEqual(model.get_config()["model_id"], "bedrock-model-id")

    def test_refuses_without_a_real_mode(self):
        with self.assertRaisesRegex(ConfigError, "deterministic"):
            build_model(settings(models=False))
        with self.assertRaisesRegex(ConfigError, "deterministic"):
            build_model(settings(models=True))
