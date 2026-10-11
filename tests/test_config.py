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
    "FACECHECK_API_KEY": "fc-SECRET-5555",
    "TRACE_API_KEY": "trk_live_SECRET-6666",
    "GOOGLE_VISION_API_KEY": "AIza-SECRET-7777",
}
FACECHECK = {"FACECHECK_API_KEY": SECRETS["FACECHECK_API_KEY"]}
LIVE = {"SERPAPI_KEY": SECRETS["SERPAPI_KEY"], "IMAGE_BUCKET": "hm-bucket", "AWS_REGION": "us-east-1"}
V3 = {"SERPAPI_KEY": SECRETS["SERPAPI_KEY"], "TRACE_API_KEY": SECRETS["TRACE_API_KEY"], "AWS_REGION": "us-east-1"}


def load(env=None, **options):
    """Load from an explicit environment and no .env unless a path is given.

    Mock search unless a test picks a backend; backend=None tests the env/default choice.
    """
    options.setdefault("dotenv_path", None)
    options.setdefault("backend", "mock")
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
    def test_default_backend_is_v3_with_v5_engines(self):
        settings = load({**V3, **FACECHECK}, backend=None)
        self.assertEqual(settings.search_backend, "rekognition")
        self.assertTrue(settings.live)
        self.assertEqual((settings.serpapi_key, settings.aws_region), (SECRETS["SERPAPI_KEY"], "us-east-1"))
        self.assertIsNone(settings.facecheck_api_key)
        self.assertEqual(settings.rekognition_engines, ("trace", "yandex_images", "bing_reverse_image"))
        self.assertEqual(settings.rekognition_min_similarity, 80.0)
        self.assertEqual((settings.jev_mode, settings.llm_mode), ("fake", "off"))
        self.assertFalse(settings.models or settings.real_models_enabled)

    def test_default_backend_needs_its_keys_and_region(self):
        error = self.assertConfigError(r"SERPAPI_KEY \(for SerpApi search\); TRACE_API_KEY \(for Trace face search\); "
                                       r"AWS_REGION \(for Rekognition face matching\)", backend=None)
        for hint in ("set-key trace", "set-key serpapi", "set-key aws --region", "REKOGNITION_ENGINES",
                     "SEARCH_BACKEND=facecheck", "--mock"):
            self.assertIn(hint, str(error))
        self.assertConfigError(r"missing required settings: AWS_REGION", {"SERPAPI_KEY": "k", "TRACE_API_KEY": "t"},
                               backend=None)

    def test_v3_needs_only_the_keys_of_its_engines(self):
        trace_only = load({"TRACE_API_KEY": "t", "AWS_REGION": "us-east-2", "REKOGNITION_ENGINES": "trace"},
                          backend="rekognition")
        self.assertEqual((trace_only.trace_api_key, trace_only.serpapi_key), ("t", None))
        self.assertEqual((trace_only.serpapi_engines, trace_only.needs_hosting), ((), False))
        serp_only = load({"SERPAPI_KEY": "s", "AWS_REGION": "us-east-2", "REKOGNITION_ENGINES": "google_lens"},
                         backend="rekognition")
        self.assertEqual((serp_only.trace_api_key, serp_only.serpapi_key), (None, "s"))
        self.assertEqual((serp_only.serpapi_engines, serp_only.needs_hosting), (("google_lens",), True))
        self.assertConfigError("TRACE_API_KEY", {"AWS_REGION": "us-east-2", "REKOGNITION_ENGINES": "trace"},
                               backend="rekognition")
        self.assertEqual(load(V3, backend="serpapi").serpapi_engines, ("yandex_images",))
        self.assertIsNone(load(V3, backend="serpapi").trace_api_key)
        self.assertFalse(load().needs_hosting)

    def test_v6_needs_the_google_vision_key(self):
        v6 = {**V3, "REKOGNITION_ENGINES": "trace,yandex_images,cloud_vision"}
        error = self.assertConfigError(r"missing required settings: GOOGLE_VISION_API_KEY \(for Cloud Vision web "
                                       r"detection\)", v6, backend="rekognition")
        self.assertIn("set-key google-vision", str(error))
        settings = load({**v6, "GOOGLE_VISION_API_KEY": SECRETS["GOOGLE_VISION_API_KEY"]}, backend="rekognition")
        self.assertEqual(settings.google_vision_api_key, SECRETS["GOOGLE_VISION_API_KEY"])
        self.assertEqual((settings.serpapi_engines, settings.needs_hosting), (("yandex_images",), True))
        # Cloud Vision takes the crop's bytes, so without a SerpApi engine nothing is hosted.
        alone = load({"GOOGLE_VISION_API_KEY": "g", "AWS_REGION": "us-east-2", "REKOGNITION_ENGINES": "cloud_vision"},
                     backend="rekognition")
        self.assertEqual((alone.serpapi_key, alone.trace_api_key, alone.needs_hosting), (None, None, False))
        # The key is dropped when no engine uses it.
        self.assertIsNone(load({**V3, "GOOGLE_VISION_API_KEY": "g"}, backend="rekognition").google_vision_api_key)
        self.assertConfigError("REKOGNITION_ENGINES='bing'", {**V3, "REKOGNITION_ENGINES": "bing"}, backend="rekognition")

    def test_mock_backend_is_offline(self):
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
        env = {"SEARCH_BACKEND": "", "MOCK_SCENARIO": " ", "JEV_MODE": "", "LLM_MODE": "", "MOCK_DELAY_MS": "",
               "FACECHECK_DEMO": "", "REKOGNITION_ENGINES": "", "REKOGNITION_MIN_SIMILARITY": " ", **V3}
        settings = load(env, backend=None)
        self.assertEqual((settings.search_backend, settings.jev_mode, settings.llm_mode), ("rekognition", "fake", "off"))
        self.assertFalse(settings.facecheck_demo)
        self.assertEqual((settings.rekognition_engines, settings.rekognition_min_similarity),
                         (("trace", "yandex_images", "bing_reverse_image"), 80.0))
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

    def test_env_example_selects_v3_and_needs_its_keys(self):
        example = REPO_ROOT / ".env.example"
        self.assertConfigError("SERPAPI_KEY.*AWS_REGION", backend=None, dotenv_path=example)
        settings = load(V3, backend=None, dotenv_path=example)
        self.assertEqual((settings.search_backend, settings.jev_mode, settings.llm_mode), ("rekognition", "fake", "off"))
        self.assertEqual((settings.rekognition_engines, settings.rekognition_min_similarity),
                         (("trace", "yandex_images", "bing_reverse_image"), 80.0))
        self.assertFalse(settings.facecheck_demo)


class SearchBackendTests(ConfigTestCase):
    def test_backend_option_selects_serpapi(self):
        settings = load({**LIVE, "SEARCH_BACKEND": "mock"}, backend="serpapi")
        self.assertEqual(settings.search_backend, "serpapi")
        self.assertEqual(settings.serpapi_key, SECRETS["SERPAPI_KEY"])
        self.assertEqual((settings.image_bucket, settings.aws_region), ("hm-bucket", "us-east-1"))

    def test_backend_option_selects_mock_over_env(self):
        settings = load({**LIVE, "SEARCH_BACKEND": "serpapi"}, backend="mock")
        self.assertEqual(settings.search_backend, "mock")
        self.assertFalse(settings.live)

    def test_env_and_dotenv_can_select_mock(self):
        self.assertEqual(load({"SEARCH_BACKEND": "mock"}, backend=None).search_backend, "mock")
        path = self.write_dotenv("SEARCH_BACKEND=mock\n")
        self.assertEqual(load(backend=None, dotenv_path=path).search_backend, "mock")

    def test_dotenv_supplies_serpapi_key(self):
        path = self.write_dotenv(f"SERPAPI_KEY={SECRETS['SERPAPI_KEY']}\n")
        self.assertEqual(load(backend="serpapi", dotenv_path=path).serpapi_key, SECRETS["SERPAPI_KEY"])

    def test_dotenv_supplies_facecheck_key(self):
        path = self.write_dotenv(f"FACECHECK_API_KEY={SECRETS['FACECHECK_API_KEY']}\nFACECHECK_DEMO=true\n")
        settings = load(backend="facecheck", dotenv_path=path)
        self.assertEqual((settings.facecheck_api_key, settings.facecheck_demo), (SECRETS["FACECHECK_API_KEY"], True))

    def test_env_can_still_select_serpapi(self):
        settings = load({**LIVE, **FACECHECK, "SEARCH_BACKEND": "serpapi"}, backend=None)
        self.assertEqual((settings.search_backend, settings.live), ("serpapi", True))
        self.assertIsNone(settings.facecheck_api_key)

    def test_facecheck_key_dropped_with_other_backends(self):
        self.assertIsNone(load(FACECHECK).facecheck_api_key)
        self.assertIsNone(load({**LIVE, **FACECHECK}, backend="serpapi").facecheck_api_key)

    def test_facecheck_demo_flag(self):
        for value, expected in (("true", True), ("1", True), ("YES", True), ("false", False), ("0", False)):
            with self.subTest(value=value):
                self.assertEqual(load({**FACECHECK, "FACECHECK_DEMO": value}, backend="facecheck").facecheck_demo,
                                 expected)
        self.assertConfigError("FACECHECK_DEMO", {**FACECHECK, "FACECHECK_DEMO": "maybe"}, backend="facecheck")

    def test_v3_keeps_the_serpapi_key_and_drops_facecheck(self):
        settings = load({**V3, **FACECHECK}, backend="rekognition")
        self.assertEqual((settings.serpapi_key, settings.facecheck_api_key), (SECRETS["SERPAPI_KEY"], None))
        self.assertIsNone(load({**V3, **FACECHECK}, backend="facecheck").serpapi_key)

    def test_rekognition_engines(self):
        for raw, expected in (("google_lens", ("google_lens",)), (" google_lens , yandex_images ", ("google_lens", "yandex_images")),
                              ("yandex_images,yandex_images", ("yandex_images",))):
            with self.subTest(raw=raw):
                self.assertEqual(load({**V3, "REKOGNITION_ENGINES": raw}, backend="rekognition").rekognition_engines,
                                 expected)
        for raw in ("bing", "yandex_images,bing", ","):
            with self.subTest(raw=raw):
                self.assertConfigError("REKOGNITION_ENGINES", {**V3, "REKOGNITION_ENGINES": raw}, backend="rekognition")

    def test_rekognition_platforms(self):
        self.assertEqual(load(V3, backend="rekognition").rekognition_platforms, ())
        for raw, expected in (("linkedin,facebook", ("linkedin", "facebook")),
                              (" Facebook , linkedin,facebook ", ("facebook", "linkedin")), (" ", ())):
            with self.subTest(raw=raw):
                settings = load({**V3, "REKOGNITION_PLATFORMS": raw}, backend="rekognition")
                self.assertEqual(settings.rekognition_platforms, expected)
        for raw in ("tiktok", "linkedin,tiktok", ","):
            with self.subTest(raw=raw):
                self.assertConfigError("REKOGNITION_PLATFORMS", {**V3, "REKOGNITION_PLATFORMS": raw},
                                       backend="rekognition")

    def test_rekognition_min_similarity(self):
        for raw, expected in (("0", 0.0), ("99.5", 99.5), ("100", 100.0)):
            with self.subTest(raw=raw):
                settings = load({**V3, "REKOGNITION_MIN_SIMILARITY": raw}, backend="rekognition")
                self.assertEqual(settings.rekognition_min_similarity, expected)
        for raw in ("-1", "101", "nan", "inf", "high"):
            with self.subTest(raw=raw):
                self.assertConfigError("REKOGNITION_MIN_SIMILARITY", {**V3, "REKOGNITION_MIN_SIMILARITY": raw},
                                       backend="rekognition")

    def test_scenario_rejected_with_facecheck(self):
        self.assertConfigError("--scenario.*--mock", FACECHECK, backend="facecheck", scenario="ambiguous")

    def test_unknown_backend(self):
        self.assertConfigError("SEARCH_BACKEND", {"SEARCH_BACKEND": "google"}, backend=None)
        self.assertConfigError("unknown search backend", backend="google")

    def test_serpapi_does_not_require_hosting_settings_at_load(self):
        settings = load({"SERPAPI_KEY": SECRETS["SERPAPI_KEY"]}, backend="serpapi")
        self.assertEqual((settings.image_bucket, settings.aws_region), (None, None))

    def test_image_host_defaults_to_tunnel(self):
        self.assertEqual(load(LIVE, backend="serpapi").image_host, "tunnel")
        self.assertEqual(load({**LIVE, "IMAGE_HOST": "s3"}, backend="serpapi").image_host, "s3")
        self.assertConfigError("IMAGE_HOST", {"IMAGE_HOST": "imgur"})

    def test_serpapi_key_dropped_with_mock(self):
        self.assertIsNone(load(LIVE).serpapi_key)


class ScenarioTests(ConfigTestCase):
    def test_env_scenarios(self):
        for name in MOCK_SCENARIOS:
            with self.subTest(name):
                self.assertEqual(load({"MOCK_SCENARIO": name}).mock_scenario, name)

    def test_cli_scenario_overrides_env(self):
        self.assertEqual(load({"MOCK_SCENARIO": "stale"}, scenario="ambiguous").mock_scenario, "ambiguous")

    def test_invalid_env_scenario_is_ignored_when_overridden(self):
        self.assertEqual(load({"MOCK_SCENARIO": "typo"}, scenario="stale").mock_scenario, "stale")
        self.assertEqual(load({**LIVE, "MOCK_SCENARIO": "typo"}, backend="serpapi").mock_scenario, "default")

    def test_unknown_scenarios(self):
        self.assertConfigError("MOCK_SCENARIO", {"MOCK_SCENARIO": "chaos"})
        self.assertConfigError("unknown scenario", scenario="chaos")

    def test_scenario_rejected_with_serpapi(self):
        self.assertConfigError("--scenario.*--mock", LIVE, backend="serpapi", scenario="ambiguous")

    def test_serpapi_scenario_is_default(self):
        self.assertEqual(load({**LIVE, "MOCK_SCENARIO": "empty"}, backend="serpapi").mock_scenario, "default")


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

    def test_serpapi_alone_does_not_permit_models(self):
        self.assertConfigError("--models", {**LIVE, "LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": "k"},
                               backend="serpapi")

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
        self.assertEqual((settings.llm_model, settings.llm_effort), ("claude-opus-5-5", "medium"))  # V4
        self.assertEqual(settings.anthropic_api_key, SECRETS["ANTHROPIC_API_KEY"])
        self.assertTrue(settings.real_models_enabled)

    def test_effort(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            env = {"LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_EFFORT": effort}
            self.assertEqual(load(env, models=True).llm_effort, effort)
        self.assertConfigError("LLM_EFFORT", {"LLM_MODE": "anthropic", "ANTHROPIC_API_KEY": "k",
                                              "LLM_EFFORT": "extreme"}, models=True)
        # Off runs no model, so a bad value is ignored there.
        self.assertIsNone(load({"LLM_EFFORT": "extreme"}).llm_effort)

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

    def test_off_has_no_model_or_key(self):
        settings = load({**SECRETS, "LLM_MODEL": "claude-sonnet-5-5", "LLM_EFFORT": "low"}, models=True)
        self.assertIsNone(settings.llm_model)
        self.assertIsNone(settings.llm_effort)
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
        return load(env, backend="serpapi", models=True)

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
        self.assertEqual((summary["llm_model"], summary["llm_effort"]), ("claude-opus-5-5", "medium"))
        self.assertTrue(summary["real_models_enabled"])
        self.assertEqual(summary["image_bucket"], "hm-bucket")

    def test_secret_fields_exist_but_are_hidden(self):
        hidden = {name for name, spec in Settings.__dataclass_fields__.items() if not spec.repr}
        self.assertEqual(hidden, {"serpapi_key", "facecheck_api_key", "trace_api_key", "google_vision_api_key",
                                  "typesafe_api_key", "openrouter_api_key", "anthropic_api_key"})

    def test_errors_never_contain_secrets(self):
        cases = (
            ({**SECRETS, "JEV_MODE": "typesafe"}, {}),
            ({**SECRETS, "LLM_MODE": "anthropic"}, {}),
            ({**SECRETS, "SEARCH_BACKEND": "bing"}, {"backend": None}),
            ({**SECRETS, "JEV_MODE": "typesafe", "MOCK_DELAY_MS": "x"}, {"models": True}),
            ({**SECRETS, "SERPAPI_KEY": ""}, {"backend": "serpapi"}),
            ({**SECRETS, **LIVE}, {"backend": "serpapi", "scenario": "stale"}),
        )
        for env, options in cases:
            with self.subTest(env=sorted(env), options=options):
                self.assertConfigError(".", env, **options)
