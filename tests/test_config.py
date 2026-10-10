"""Settings precedence, gates, validation, and secret handling (spec 0.3)."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from pipeline.config import MOCK_SCENARIOS, REPO_ROOT, TRUST_TABLE, ConfigError, Settings, load_settings
from tests.support import OfflineTestCase

SECRETS = {
    "SERPAPI_KEY": "serp-SECRET-1111",
    "TYPESAFE_API_KEY": "ts-SECRET-2222",
    "OPENROUTER_API_KEY": "or-SECRET-3333",
    "ANTHROPIC_API_KEY": "sk-ant-SECRET-4444",
}
LIVE = {"SERPAPI_KEY": SECRETS["SERPAPI_KEY"], "IMAGE_BUCKET": "hm-bucket", "AWS_REGION": "us-east-1"}


def load(env=None, **options):
    """Load from an explicit environment and no .env unless a path is given."""
    options.setdefault("dotenv_path", None)
    return load_settings(environ=env or {}, **options)


class ConfigTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def write_dotenv(self, text):
        path = self.folder / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def assertConfigError(self, pattern, env=None, **options):
        with self.assertRaisesRegex(ConfigError, pattern) as caught:
            load(env, **options)
        for secret in SECRETS.values():
            self.assertNotIn(secret, str(caught.exception))
        return caught.exception


class DefaultsTests(ConfigTestCase):
    def test_empty_environment_is_offline(self):
        settings = load()
        self.assertEqual(settings.search_backend, "mock")
        self.assertEqual(settings.mock_internet_url, "http://127.0.0.1:8765")
        self.assertEqual(settings.mock_scenario, "default")
        self.assertEqual(settings.mock_delay_ms, 0)
        self.assertEqual((settings.jev_mode, settings.jev_model), ("fake", None))
        self.assertEqual((settings.llm_mode, settings.llm_model), ("off", None))
        self.assertFalse(settings.live or settings.models or settings.real_models_enabled)

    def test_environ_none_reads_os_environ(self):
        with patch.dict(os.environ, {"MOCK_SCENARIO": "stale", "MOCK_DELAY_MS": "25"}):
            settings = load_settings(dotenv_path=None)
        self.assertEqual((settings.mock_scenario, settings.mock_delay_ms), ("stale", 25))

    def test_empty_strings_count_as_unset(self):
        env = {"SEARCH_BACKEND": "", "MOCK_SCENARIO": " ", "JEV_MODE": "", "LLM_MODE": "", "MOCK_DELAY_MS": ""}
        settings = load(env)
        self.assertEqual((settings.search_backend, settings.jev_mode, settings.llm_mode), ("mock", "fake", "off"))
        self.assertEqual((settings.mock_scenario, settings.mock_delay_ms), ("default", 0))

    def test_settings_are_frozen(self):
        with self.assertRaises(AttributeError):
            load().live = True

    def test_import_loads_no_model_or_aws_sdk(self):
        code = ("import sys, pipeline.config; "
                "print(sorted(m for m in ('strands', 'anthropic', 'typesafe_sdk', 'boto3', 'botocore') if m in sys.modules))")
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


class DotenvTests(ConfigTestCase):
    def test_dotenv_fills_missing_variables_only(self):
        path = self.write_dotenv("MOCK_SCENARIO=ambiguous\nMOCK_DELAY_MS=40\n")
        settings = load({"MOCK_SCENARIO": "empty"}, dotenv_path=path)
        self.assertEqual(settings.mock_scenario, "empty")  # exported value wins
        self.assertEqual(settings.mock_delay_ms, 40)  # filled from .env

    def test_dotenv_fills_exported_empty_value(self):
        path = self.write_dotenv("MOCK_SCENARIO=stale\n")
        self.assertEqual(load({"MOCK_SCENARIO": ""}, dotenv_path=path).mock_scenario, "stale")

    def test_blank_dotenv_secrets_count_as_unset(self):
        path = self.write_dotenv("JEV_MODE=typesafe\nTYPESAFE_API_KEY=\n")
        self.assertConfigError("TYPESAFE_API_KEY", models=True, dotenv_path=path)

    def test_dotenv_does_not_mutate_os_environ(self):
        path = self.write_dotenv("MOCK_SCENARIO=ambiguous\nANTHROPIC_API_KEY=sk-ant-SECRET-4444\n")
        before = dict(os.environ)
        self.assertEqual(load_settings(dotenv_path=path).mock_scenario, "ambiguous")
        self.assertEqual(dict(os.environ), before)

    def test_dotenv_values_are_not_interpolated(self):
        path = self.write_dotenv("LLM_MODE=anthropic\nANTHROPIC_API_KEY=abc${HOME}def\n")
        self.assertEqual(load(models=True, dotenv_path=path).anthropic_api_key, "abc${HOME}def")

    def test_dotenv_path_none_skips_dotenv(self):
        self.write_dotenv("MOCK_SCENARIO=ambiguous\n")
        self.assertEqual(load(dotenv_path=None).mock_scenario, "default")

    def test_missing_dotenv_file_is_ignored(self):
        self.assertEqual(load(dotenv_path=self.folder / "absent.env").mock_scenario, "default")

    def test_env_example_loads_offline(self):
        settings = load(dotenv_path=REPO_ROOT / ".env.example")
        self.assertEqual((settings.search_backend, settings.jev_mode, settings.llm_mode), ("mock", "fake", "off"))


class SearchBackendTests(ConfigTestCase):
    def test_live_selects_serpapi(self):
        settings = load({**LIVE, "SEARCH_BACKEND": "mock"}, live=True)
        self.assertEqual(settings.search_backend, "serpapi")
        self.assertEqual(settings.serpapi_key, SECRETS["SERPAPI_KEY"])
        self.assertEqual((settings.image_bucket, settings.aws_region), ("hm-bucket", "us-east-1"))

    def test_serpapi_without_live_is_rejected(self):
        self.assertConfigError("SEARCH_BACKEND=serpapi.*--live", {**LIVE, "SEARCH_BACKEND": "serpapi"})

    def test_serpapi_from_dotenv_without_live_is_rejected(self):
        path = self.write_dotenv("SEARCH_BACKEND=serpapi\n")
        self.assertConfigError("--live", dotenv_path=path)

    def test_unknown_backend(self):
        self.assertConfigError("SEARCH_BACKEND", {"SEARCH_BACKEND": "google"})

    def test_live_requires_each_credential(self):
        for name in LIVE:
            with self.subTest(missing=name):
                env = {key: value for key, value in LIVE.items() if key != name}
                self.assertConfigError(rf"{name} \(for --live\)", env, live=True)

    def test_live_reports_all_missing_credentials(self):
        error = self.assertConfigError("SERPAPI_KEY", live=True)
        self.assertIn("IMAGE_BUCKET", str(error))
        self.assertIn("AWS_REGION", str(error))

    def test_serpapi_key_dropped_without_live(self):
        self.assertIsNone(load(LIVE).serpapi_key)


class ScenarioTests(ConfigTestCase):
    def test_env_scenarios(self):
        for name in MOCK_SCENARIOS:
            with self.subTest(name):
                self.assertEqual(load({"MOCK_SCENARIO": name}).mock_scenario, name)

    def test_cli_scenario_overrides_env(self):
        self.assertEqual(load({"MOCK_SCENARIO": "stale"}, scenario="ambiguous").mock_scenario, "ambiguous")

    def test_unknown_scenarios(self):
        self.assertConfigError("MOCK_SCENARIO", {"MOCK_SCENARIO": "chaos"})
        self.assertConfigError("unknown scenario", scenario="chaos")

    def test_scenario_rejected_in_live_mode(self):
        self.assertConfigError("--scenario.*--live", LIVE, live=True, scenario="ambiguous")

    def test_live_scenario_is_default(self):
        self.assertEqual(load({**LIVE, "MOCK_SCENARIO": "empty"}, live=True).mock_scenario, "default")


class MockSettingsTests(ConfigTestCase):
    def test_delay_bounds(self):
        for raw, expected in (("0", 0), ("5000", 5000), (" 250 ", 250)):
            with self.subTest(raw):
                self.assertEqual(load({"MOCK_DELAY_MS": raw}).mock_delay_ms, expected)
        for raw in ("-1", "5001", "1.5", "fast"):
            with self.subTest(raw):
                self.assertConfigError("MOCK_DELAY_MS", {"MOCK_DELAY_MS": raw})

    def test_loopback_urls_accepted(self):
        for url in ("http://127.0.0.1:9999", "http://localhost:8765/", "http://[::1]:8765"):
            with self.subTest(url):
                self.assertEqual(load({"MOCK_INTERNET_URL": url}).mock_internet_url, url.rstrip("/"))

    def test_non_loopback_urls_rejected(self):
        for url in (
            "https://127.0.0.1:8765", "http://example.com:8765", "http://10.0.0.5:8765",
            "http://127.0.0.1:8765/api", "http://127.0.0.1:8765?x=1", "http://127.0.0.1:99999",
            "http://127.0.0.1:port", "127.0.0.1:8765",
        ):
            with self.subTest(url):
                self.assertConfigError("MOCK_INTERNET_URL", {"MOCK_INTERNET_URL": url})

    def test_url_error_does_not_echo_userinfo(self):
        error = self.assertConfigError("MOCK_INTERNET_URL", {"MOCK_INTERNET_URL": "http://u:hunter2@127.0.0.1:8765"})
        self.assertNotIn("hunter2", str(error))


class ModelGateTests(ConfigTestCase):
    def test_real_modes_need_models_flag(self):
        cases = (
            ({"JEV_MODE": "typesafe", "TYPESAFE_API_KEY": SECRETS["TYPESAFE_API_KEY"]}, "JEV_MODE=typesafe"),
            ({"JEV_MODE": "openrouter", "OPENROUTER_API_KEY": SECRETS["OPENROUTER_API_KEY"]}, "JEV_MODE=openrouter"),
            ({"LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": SECRETS["ANTHROPIC_API_KEY"]}, "LLM_MODE=anthropic"),
            ({"LLM_MODE": "bedrock", "LLM_MODEL": "br-model", "AWS_REGION": "us-east-1"}, "LLM_MODE=bedrock"),
        )
        for env, pattern in cases:
            with self.subTest(pattern):
                self.assertConfigError(f"{pattern}.*--models", env)

    def test_live_alone_does_not_permit_models(self):
        self.assertConfigError("--models", {**LIVE, "LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": "k"}, live=True)

    def test_models_flag_with_offline_modes_is_allowed(self):
        settings = load(models=True)
        self.assertTrue(settings.models)
        self.assertFalse(settings.real_models_enabled)
        self.assertEqual(load({"JEV_MODE": "off"}, models=True).jev_mode, "off")

    def test_unknown_modes(self):
        self.assertConfigError("JEV_MODE", {"JEV_MODE": "gpt"}, models=True)
        self.assertConfigError("LLM_MODE", {"LLM_MODE": "openai"}, models=True)


class JevTests(ConfigTestCase):
    def test_typesafe_defaults(self):
        settings = load({"JEV_MODE": "typesafe", "TYPESAFE_API_KEY": SECRETS["TYPESAFE_API_KEY"]}, models=True)
        self.assertEqual((settings.jev_model, settings.typesafe_api_key), ("jev-latest", SECRETS["TYPESAFE_API_KEY"]))
        self.assertTrue(settings.real_models_enabled)

    def test_openrouter_defaults(self):
        settings = load({"JEV_MODE": "openrouter", "OPENROUTER_API_KEY": SECRETS["OPENROUTER_API_KEY"]}, models=True)
        self.assertEqual(settings.jev_model, "typesafe/jev-1.13")
        self.assertEqual(settings.openrouter_api_key, SECRETS["OPENROUTER_API_KEY"])

    def test_explicit_jev_model(self):
        env = {"JEV_MODE": "openrouter", "OPENROUTER_API_KEY": "k", "JEV_MODEL": "~typesafe/jev-latest"}
        self.assertEqual(load(env, models=True).jev_model, "~typesafe/jev-latest")

    def test_route_keys_required(self):
        self.assertConfigError(r"TYPESAFE_API_KEY \(for JEV_MODE=typesafe\)", {"JEV_MODE": "typesafe"}, models=True)
        self.assertConfigError(r"OPENROUTER_API_KEY \(for JEV_MODE=openrouter\)", {"JEV_MODE": "openrouter"}, models=True)

    def test_offline_modes_have_no_model_or_keys(self):
        for mode in ("fake", "off"):
            with self.subTest(mode):
                settings = load({**SECRETS, "JEV_MODE": mode, "JEV_MODEL": "jev-latest"}, models=True)
                self.assertIsNone(settings.jev_model)
                self.assertIsNone(settings.typesafe_api_key)
                self.assertIsNone(settings.openrouter_api_key)


class LlmTests(ConfigTestCase):
    def test_anthropic_defaults(self):
        settings = load({"LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": SECRETS["ANTHROPIC_API_KEY"]}, models=True)
        self.assertEqual(settings.llm_model, "claude-sonnet-5-5")
        self.assertEqual(settings.anthropic_api_key, SECRETS["ANTHROPIC_API_KEY"])
        self.assertTrue(settings.real_models_enabled)

    def test_anthropic_explicit_model(self):
        env = {"LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "claude-haiku-4-5-20251001"}
        self.assertEqual(load(env, models=True).llm_model, "claude-haiku-4-5-20251001")

    def test_anthropic_needs_key(self):
        self.assertConfigError(r"ANTHROPIC_API_KEY \(for LLM_MODE=anthropic\)", {"LLM_MODE": "anthropic"}, models=True)

    def test_bedrock_needs_region_and_model_but_not_anthropic_key(self):
        settings = load({"LLM_MODE": "bedrock", "AWS_REGION": "us-west-2", "LLM_MODEL": "br-model"}, models=True)
        self.assertEqual((settings.llm_model, settings.aws_region), ("br-model", "us-west-2"))
        self.assertIsNone(settings.anthropic_api_key)
        self.assertConfigError(r"AWS_REGION \(for LLM_MODE=bedrock\)", {"LLM_MODE": "bedrock", "LLM_MODEL": "m"}, models=True)
        error = self.assertConfigError(r"LLM_MODEL \(for LLM_MODE=bedrock\)", {"LLM_MODE": "bedrock", "AWS_REGION": "r"}, models=True)
        self.assertNotIn("ANTHROPIC_API_KEY", str(error))

    def test_shared_aws_region_is_reported_for_both_uses(self):
        error = self.assertConfigError("AWS_REGION", {**LIVE, "AWS_REGION": "", "LLM_MODE": "bedrock", "LLM_MODEL": "m"},
                                       live=True, models=True)
        self.assertIn("AWS_REGION (for --live, LLM_MODE=bedrock)", str(error))

    def test_off_has_no_model_or_key(self):
        settings = load({**SECRETS, "LLM_MODEL": "claude-sonnet-5-5"}, models=True)
        self.assertIsNone(settings.llm_model)
        self.assertIsNone(settings.anthropic_api_key)


class TrustTests(ConfigTestCase):
    def test_trust_table(self):
        settings = load()
        self.assertEqual([settings.trust_for(n) for n in range(6)], [0, 50, 95, 99, 99, 99])
        self.assertEqual(TRUST_TABLE, {0: 0, 1: 50, 2: 95, 3: 99})
        with self.assertRaises(ValueError):
            settings.trust_for(-1)


class SecretTests(ConfigTestCase):
    def everything_enabled(self, **extra):
        env = {**LIVE, **SECRETS, "JEV_MODE": "typesafe", "LLM_MODE": "anthropic", **extra}
        return load(env, live=True, models=True)

    def test_repr_and_describe_hide_secrets(self):
        for settings in (self.everything_enabled(), self.everything_enabled(JEV_MODE="openrouter")):
            text = repr(settings) + repr(settings.describe())
            for secret in SECRETS.values():
                self.assertNotIn(secret, text)
            self.assertNotIn("api_key", repr(settings))
            self.assertFalse(any("key" in name for name in settings.describe()))

    def test_describe_summarizes_non_secret_settings(self):
        summary = self.everything_enabled().describe()
        self.assertEqual(summary["search_backend"], "serpapi")
        self.assertEqual(summary["jev_model"], "jev-latest")
        self.assertEqual(summary["llm_model"], "claude-sonnet-5-5")
        self.assertTrue(summary["real_models_enabled"])
        self.assertEqual(summary["image_bucket"], "hm-bucket")

    def test_secret_fields_exist_but_are_hidden(self):
        hidden = {name for name, spec in Settings.__dataclass_fields__.items() if not spec.repr}
        self.assertEqual(hidden, {"serpapi_key", "typesafe_api_key", "openrouter_api_key", "anthropic_api_key"})

    def test_errors_never_contain_secrets(self):
        cases = (
            ({**SECRETS, "JEV_MODE": "typesafe"}, {}),
            ({**SECRETS, "LLM_MODE": "anthropic"}, {}),
            ({**SECRETS, "SEARCH_BACKEND": "serpapi"}, {}),
            ({**SECRETS, "JEV_MODE": "typesafe", "MOCK_DELAY_MS": "x"}, {"models": True}),
            ({**SECRETS, "IMAGE_BUCKET": "b"}, {"live": True}),
            ({**SECRETS, **LIVE}, {"live": True, "scenario": "stale"}),
        )
        for env, options in cases:
            with self.subTest(env=sorted(env), options=options):
                self.assertConfigError(".", env, **options)
