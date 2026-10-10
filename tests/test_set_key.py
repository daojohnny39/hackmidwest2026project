"""`python -m pipeline set-key`, the .env writer, and the SerpApi key check.

The prompt and SerpApi are patched; nothing leaves the machine.
"""

import io
import os
import stat
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from dotenv import dotenv_values

from pipeline import __main__ as cli
from pipeline.__main__ import BAD_CONFIG, OK, REFUSED, main
from pipeline.config import REPO_ROOT, load_settings, set_env_value
from pipeline.serpapi_account import AccountCheckFailed, KeyRejected, account_quota, describe_quota
from tests.support import OfflineTestCase

KEY = "0123abcdSECRETkey9876"
QUOTA = {"plan_name": "Free Plan", "searches_per_month": 250, "total_searches_left": 250,
         "account_rate_limit_per_hour": 50}


class EnvFileCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.env_file = self.folder / ".env"


class SetEnvValueTests(EnvFileCase):
    def test_new_file_starts_from_template(self):
        set_env_value(self.env_file, "SERPAPI_KEY", KEY, template=REPO_ROOT / ".env.example")
        values = dotenv_values(self.env_file)
        self.assertEqual(values["SERPAPI_KEY"], KEY)
        self.assertEqual(values["SEARCH_BACKEND"], "serpapi")
        self.assertEqual(values["JEV_MODE"], "fake")

    def test_new_file_without_template(self):
        set_env_value(self.env_file, "SERPAPI_KEY", KEY)
        self.assertEqual(self.env_file.read_text(), f"SERPAPI_KEY={KEY}\n")

    def test_replaces_in_place_and_drops_duplicates(self):
        self.env_file.write_text("# keys\nSERPAPI_KEY=old\nJEV_MODE=fake\nexport SERPAPI_KEY=older\n# SERPAPI_KEY=note\n")
        set_env_value(self.env_file, "SERPAPI_KEY", KEY)
        self.assertEqual(self.env_file.read_text(), f"# keys\nSERPAPI_KEY={KEY}\nJEV_MODE=fake\n# SERPAPI_KEY=note\n")

    def test_appends_when_missing(self):
        self.env_file.write_text("JEV_MODE=fake")
        set_env_value(self.env_file, "TYPESAFE_API_KEY", KEY)
        self.assertEqual(dotenv_values(self.env_file), {"JEV_MODE": "fake", "TYPESAFE_API_KEY": KEY})

    def test_file_is_private_and_no_temp_files_remain(self):
        set_env_value(self.env_file, "SERPAPI_KEY", KEY)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [".env"])

    def test_rejects_unsafe_values_without_echoing_them(self):
        for value in ("", "has space", "quote\"d", "hash#tag", "two\nlines", "a=b"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    set_env_value(self.env_file, "SERPAPI_KEY", value)
                if value:
                    self.assertNotIn(value, str(caught.exception))
        self.assertFalse(self.env_file.exists())

    def test_rejects_unknown_settings(self):
        with self.assertRaises(ValueError):
            set_env_value(self.env_file, "PATH", "x")

    def test_saved_key_is_loaded(self):
        set_env_value(self.env_file, "SERPAPI_KEY", KEY, template=REPO_ROOT / ".env.example")
        settings = load_settings(backend="serpapi", environ={}, dotenv_path=self.env_file)
        self.assertEqual(settings.serpapi_key, KEY)


class SetKeyCommandTests(EnvFileCase):
    def set_key(self, *argv, entered=KEY, quota=QUOTA):
        """Run set-key with a patched prompt and Account API; return (code, out, err, check mock)."""
        prompt = patch.object(cli.getpass, "getpass",
                              side_effect=entered if isinstance(entered, BaseException) else None,
                              return_value=entered)
        check = patch.object(cli, "account_quota",
                             side_effect=quota if isinstance(quota, BaseException) else None,
                             return_value=quota)
        out, err = io.StringIO(), io.StringIO()
        with prompt, check as checked, redirect_stdout(out), redirect_stderr(err):
            code = main(["set-key", *argv, "--env-file", str(self.env_file)])
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue(), checked

    def saved(self):
        return dotenv_values(self.env_file).get("SERPAPI_KEY") if self.env_file.exists() else None

    def test_saves_a_checked_serpapi_key(self):
        code, out, _, checked = self.set_key("serpapi", entered=f"  {KEY}\n")
        self.assertEqual(code, OK)
        checked.assert_called_once_with(KEY)
        self.assertIn("SerpApi accepted the key: Free Plan, 250 searches left", out)
        self.assertEqual(self.saved(), KEY)
        self.assertEqual(dotenv_values(self.env_file)["SEARCH_BACKEND"], "serpapi")  # from .env.example

    def test_rejected_key_is_not_saved(self):
        code, _, err, _ = self.set_key("serpapi", quota=KeyRejected("HTTP 401"))
        self.assertEqual(code, REFUSED)
        self.assertIn("rejected", err)
        self.assertIsNone(self.saved())

    def test_unreachable_check_still_saves(self):
        code, out, _, _ = self.set_key("serpapi", quota=AccountCheckFailed("ConnectError"))
        self.assertEqual(code, OK)
        self.assertIn("couldn't check", out)
        self.assertEqual(self.saved(), KEY)

    def test_no_check_skips_serpapi(self):
        code, _, _, checked = self.set_key("serpapi", "--no-check")
        self.assertEqual(code, OK)
        checked.assert_not_called()
        self.assertEqual(self.saved(), KEY)

    def test_other_services_are_saved_without_a_check(self):
        code, _, _, checked = self.set_key("typesafe")
        self.assertEqual(code, OK)
        checked.assert_not_called()
        self.assertEqual(dotenv_values(self.env_file)["TYPESAFE_API_KEY"], KEY)

    def test_empty_or_malformed_input_saves_nothing(self):
        for entered in ("", "   ", "not a key", "key#comment"):
            with self.subTest(entered=entered):
                code, _, _, checked = self.set_key("serpapi", entered=entered)
                self.assertEqual(code, BAD_CONFIG)
                checked.assert_not_called()
                self.assertFalse(self.env_file.exists())

    def test_cancel_saves_nothing(self):
        for interrupt in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interrupt=type(interrupt).__name__):
                code, _, err, _ = self.set_key("serpapi", entered=interrupt)
                self.assertEqual(code, REFUSED)
                self.assertIn("Cancelled", err)
                self.assertFalse(self.env_file.exists())

    def test_warns_when_an_exported_key_would_win(self):
        os.environ["SERPAPI_KEY"] = "exported-key"
        code, out, _, _ = self.set_key("serpapi")
        self.assertEqual(code, OK)
        self.assertIn("unset SERPAPI_KEY", out)


class AccountQuotaTests(OfflineTestCase):
    def respond(self, status=200, body=None, error=None):
        def fake_get(url, params, timeout):
            self.assertEqual(url, "https://serpapi.com/account.json")
            self.assertEqual(params, {"api_key": KEY})
            if error:
                raise error
            request = httpx.Request("GET", url, params=params)
            if isinstance(body, str):
                return httpx.Response(status, text=body, request=request)
            return httpx.Response(status, json=body, request=request)
        return patch("pipeline.serpapi_account.httpx.get", side_effect=fake_get)

    def test_keeps_only_quota_fields(self):
        body = {**QUOTA, "api_key": KEY, "account_email": "someone@example.com", "account_id": "a1"}
        with self.respond(body=body):
            self.assertEqual(account_quota(KEY), QUOTA)

    def test_rejections(self):
        for status, body in ((401, {"error": "Invalid API key."}), (403, {}),
                             (200, {"error": f"Key {KEY} is suspended"})):
            with self.subTest(status=status), self.respond(status, body):
                with self.assertRaises(KeyRejected) as caught:
                    account_quota(KEY)
                self.assertNotIn(KEY, str(caught.exception))

    def test_failures_do_not_reject_the_key(self):
        cases = (
            {"error": httpx.ConnectError("boom")},
            {"status": 500, "body": {}},
            {"body": "<html>"},
            {"body": ["not", "a", "dict"]},
        )
        for case in cases:
            with self.subTest(case=case), self.respond(**case):
                with self.assertRaises(AccountCheckFailed) as caught:
                    account_quota(KEY)
                self.assertNotIn(KEY, str(caught.exception))

    def test_describe_quota(self):
        self.assertEqual(describe_quota(QUOTA), "Free Plan, 250 searches left (250 per month), limit 50 per hour")
        self.assertEqual(describe_quota({}), "unknown plan")
