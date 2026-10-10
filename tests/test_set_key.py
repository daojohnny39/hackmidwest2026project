"""`python -m pipeline set-key`, the .env writer, and the SerpApi, TypeSafe, and AWS key checks.

The prompt, SerpApi, TypeSafe, and AWS STS are patched or stubbed; nothing leaves the machine.
"""

import configparser
import fnmatch
import getpass
import io
import logging
import os
import stat
import tempfile
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boto3
import httpx
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from dotenv import dotenv_values

from pipeline import __main__ as cli
from pipeline import aws_credentials
from pipeline.__main__ import BAD_CONFIG, OK, REFUSED, main
from pipeline.config import REPO_ROOT, load_settings, set_env_value
from pipeline.jev import KeyCheckFailed as TypeSafeCheckFailed, KeyRejected as TypeSafeKeyRejected
from pipeline.serpapi_account import AccountCheckFailed, KeyRejected, account_quota, describe_quota
from tests.support import OfflineTestCase

KEY = "0123abcdSECRETkey9876"
QUOTA = {"plan_name": "Free Plan", "searches_per_month": 250, "total_searches_left": 250,
         "account_rate_limit_per_hour": 50}
MODELS = ["jev-1.13.0"]


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

    def test_existing_readable_file_becomes_private(self):
        # POSIX filesystems only; on /mnt/c Windows ACLs apply instead.
        self.env_file.write_text("JEV_MODE=fake\n")
        self.env_file.chmod(0o644)
        set_env_value(self.env_file, "SERPAPI_KEY", KEY)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)

    def test_failed_replace_keeps_old_file_and_temp_name_is_gitignored(self):
        self.env_file.write_text("SERPAPI_KEY=old\n")
        with patch("pipeline.config.os.replace", side_effect=OSError("disk full")) as replace:
            with self.assertRaises(OSError):
                set_env_value(self.env_file, "SERPAPI_KEY", KEY)
        temp = Path(replace.call_args.args[0])
        self.assertTrue(fnmatch.fnmatch(temp.name, ".env.*"), temp.name)  # .gitignore ignores `.env.*`
        self.assertFalse(temp.exists())
        self.assertEqual(self.env_file.read_text(), "SERPAPI_KEY=old\n")

    def test_dotenv_syntax_beyond_simple_lines(self):
        cases = (
            # A quoted key is the same variable; python-dotenv would let it win.
            ("SERPAPI_KEY=old\n'SERPAPI_KEY'=stale\n", f"SERPAPI_KEY={KEY}\n"),
            # Lines inside another quoted value are not assignments.
            ('IMAGE_BUCKET="keep\nSERPAPI_KEY=not-an-assignment\nend"\nSERPAPI_KEY=old\n',
             f'IMAGE_BUCKET="keep\nSERPAPI_KEY=not-an-assignment\nend"\nSERPAPI_KEY={KEY}\n'),
            # CRLF files keep CRLF, including appended lines.
            ("SERPAPI_KEY=old # note\r\nJEV_MODE=fake\r\n", f"SERPAPI_KEY={KEY}\r\nJEV_MODE=fake\r\n"),
            ("JEV_MODE=fake\r\n", f"JEV_MODE=fake\r\nSERPAPI_KEY={KEY}\r\n"),
        )
        for before, after in cases:
            with self.subTest(before=before):
                self.env_file.write_bytes(before.encode())
                set_env_value(self.env_file, "SERPAPI_KEY", KEY)
                self.assertEqual(self.env_file.read_bytes().decode(), after)
                self.assertEqual(dotenv_values(self.env_file, interpolate=False)["SERPAPI_KEY"], KEY)

    def test_unparseable_lines_are_kept(self):
        self.env_file.write_text("not an assignment\nSERPAPI_KEY=old\n")
        with self.assertLogs("dotenv.main", level="WARNING"):  # python-dotenv names the line number only
            set_env_value(self.env_file, "SERPAPI_KEY", KEY)
        self.assertEqual(self.env_file.read_text(), f"not an assignment\nSERPAPI_KEY={KEY}\n")

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
    def set_key(self, *argv, entered=KEY, quota=QUOTA, models=MODELS):
        """Run set-key with patched prompt and key checks; return (code, out, err, SerpApi check mock).

        The TypeSafe check mock is left on self.typesafe_checked.
        """
        prompt = patch.object(cli.getpass, "getpass",
                              side_effect=entered if isinstance(entered, BaseException) or callable(entered) else None,
                              return_value=entered)
        check = patch.object(cli, "account_quota",
                             side_effect=quota if isinstance(quota, BaseException) else None,
                             return_value=quota)
        typesafe = patch.object(cli, "check_typesafe_key",
                                side_effect=models if isinstance(models, BaseException) else None,
                                return_value=models)
        out, err = io.StringIO(), io.StringIO()
        with prompt, check as checked, typesafe as self.typesafe_checked, redirect_stdout(out), redirect_stderr(err):
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
        for service, name, mode in (("openrouter", "OPENROUTER_API_KEY", "JEV_MODE=openrouter"),
                                    ("anthropic", "ANTHROPIC_API_KEY", "LLM_MODE=anthropic")):
            with self.subTest(service=service):
                code, out, _, checked = self.set_key(service)
                self.assertEqual(code, OK)
                checked.assert_not_called()
                self.typesafe_checked.assert_not_called()
                self.assertEqual(dotenv_values(self.env_file)[name], KEY)
                self.assertIn(f"set {mode} in .env and add --models", out)

    def typesafe_saved(self):
        return dotenv_values(self.env_file).get("TYPESAFE_API_KEY") if self.env_file.exists() else None

    def test_saves_a_checked_typesafe_key(self):
        code, out, _, checked = self.set_key("typesafe")
        self.assertEqual(code, OK)
        self.typesafe_checked.assert_called_once_with(KEY)
        checked.assert_not_called()
        self.assertIn("TypeSafe accepted the key; models available: jev-1.13.0.", out)
        self.assertIn("set JEV_MODE=typesafe in .env and add --models", out)
        self.assertEqual(self.typesafe_saved(), KEY)
        self.assertEqual(dotenv_values(self.env_file)["JEV_MODE"], "fake")  # set-key never switches the mode

    def test_rejected_typesafe_key_is_not_saved(self):
        code, _, err, _ = self.set_key("typesafe", models=TypeSafeKeyRejected("HTTP 401"))
        self.assertEqual(code, REFUSED)
        self.assertIn("TypeSafe rejected the key (HTTP 401); nothing was saved.", err)
        self.assertIsNone(self.typesafe_saved())

    def test_unreachable_typesafe_check_still_saves(self):
        code, out, _, _ = self.set_key("typesafe", models=TypeSafeCheckFailed("TypeSafeAPIConnectionError"))
        self.assertEqual(code, OK)
        self.assertIn("couldn't check the key with TypeSafe", out)
        self.assertEqual(self.typesafe_saved(), KEY)

    def test_no_check_skips_typesafe(self):
        code, _, _, _ = self.set_key("typesafe", "--no-check")
        self.assertEqual(code, OK)
        self.typesafe_checked.assert_not_called()
        self.assertEqual(self.typesafe_saved(), KEY)

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

    def test_refuses_when_input_cannot_be_hidden(self):
        def visible_fallback(prompt):
            # What getpass does when it can't turn off echo: warn, then read visibly.
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return KEY
        code, _, err, checked = self.set_key("serpapi", entered=visible_fallback)
        self.assertEqual(code, REFUSED)
        self.assertIn("can't hide", err)
        checked.assert_not_called()
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

    def test_request_url_is_not_logged(self):
        def logging_get(url, params, timeout):
            # httpx logs every request URL, query string included, at INFO.
            logging.getLogger("httpx").info("HTTP Request: GET %s?api_key=%s", url, params["api_key"])
            return httpx.Response(200, json=QUOTA, request=httpx.Request("GET", url))
        with patch("pipeline.serpapi_account.httpx.get", side_effect=logging_get):
            with self.assertNoLogs("httpx", level="DEBUG"):
                self.assertEqual(account_quota(KEY), QUOTA)
        self.assertFalse(logging.getLogger("httpx").disabled)

    def test_describe_quota(self):
        self.assertEqual(describe_quota(QUOTA), "Free Plan, 250 searches left (250 per month), limit 50 per hour")
        self.assertEqual(describe_quota({}), "unknown plan")


# AWS's documented example keys (https://docs.aws.amazon.com/IAM/latest/UserGuide/security-creds.html).
AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
IDENTITY = aws_credentials.CallerIdentity(account="123456789012", arn="arn:aws:iam::123456789012:user/pipeline")


class SetAwsKeysTests(EnvFileCase):
    def setUp(self):
        super().setUp()
        self.credentials = self.folder / "aws" / "credentials"

    def set_key(self, *argv, entered=(AWS_KEY_ID, AWS_SECRET), identity=IDENTITY, file=True):
        """Run `set-key aws` with patched prompts and STS check; return (code, out, err, check mock)."""
        prompt = patch.object(cli.getpass, "getpass", side_effect=entered if callable(entered) else list(entered))
        check = patch.object(cli.aws_credentials, "check_keys",
                             side_effect=identity if isinstance(identity, BaseException) else None,
                             return_value=identity)
        target = ["--credentials-file", str(self.credentials)] if file else []
        out, err = io.StringIO(), io.StringIO()
        with prompt, check as checked, redirect_stdout(out), redirect_stderr(err):
            code = main(["set-key", "aws", *argv, *target, "--env-file", str(self.env_file)])
        for secret in (AWS_SECRET, AWS_KEY_ID):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue(), checked

    def saved(self, path=None):
        parser = configparser.RawConfigParser()
        parser.read(path or self.credentials)
        return {section: dict(parser[section]) for section in parser.sections()}

    def test_saves_checked_keys_where_boto3_reads_them(self):
        code, out, _, checked = self.set_key()
        self.assertEqual(code, OK)
        checked.assert_called_once_with(AWS_KEY_ID, AWS_SECRET, None)
        self.assertIn("account 123456789012", out)
        self.assertIn("key ending MPLE", out)
        self.assertEqual(self.saved(), {"default": {"aws_access_key_id": AWS_KEY_ID,
                                                    "aws_secret_access_key": AWS_SECRET}})
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.credentials.parent.stat().st_mode), 0o700)
        self.assertEqual([path.name for path in self.credentials.parent.iterdir()], ["credentials"])
        self.assertFalse(self.env_file.exists())
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(self.credentials)
        found = boto3.Session().get_credentials()
        self.assertEqual((found.access_key, found.secret_key), (AWS_KEY_ID, AWS_SECRET))

    def test_default_path_follows_aws_shared_credentials_file(self):
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(self.folder / "elsewhere")
        self.assertEqual(self.set_key("--no-check", file=False)[0], OK)
        self.assertEqual(self.saved(self.folder / "elsewhere")["default"]["aws_access_key_id"], AWS_KEY_ID)

    def test_region_is_checked_and_saved_to_env(self):
        code, out, _, checked = self.set_key("--region", "us-east-2")
        self.assertEqual(code, OK)
        checked.assert_called_once_with(AWS_KEY_ID, AWS_SECRET, "us-east-2")
        self.assertEqual(dotenv_values(self.env_file)["AWS_REGION"], "us-east-2")
        self.assertIn("Saved AWS_REGION=us-east-2", out)

    def test_bad_region_is_refused_before_any_prompt(self):
        code, _, err, checked = self.set_key("--region", "Ohio", entered=())
        self.assertEqual(code, BAD_CONFIG)
        self.assertIn("isn't an AWS region", err)
        checked.assert_not_called()
        self.assertFalse(self.credentials.exists())

    def test_region_flag_is_for_aws_only(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["set-key", "serpapi", "--region", "us-east-1", "--env-file", str(self.env_file)])
        self.assertEqual(code, BAD_CONFIG)
        self.assertFalse(self.env_file.exists())

    def test_rejected_keys_are_not_saved(self):
        code, _, err, _ = self.set_key(identity=aws_credentials.KeyRejected("InvalidClientTokenId"))
        self.assertEqual(code, REFUSED)
        self.assertIn("AWS rejected the keys (InvalidClientTokenId)", err)
        self.assertFalse(self.credentials.exists())

    def test_unreachable_check_still_saves(self):
        code, out, _, _ = self.set_key(identity=aws_credentials.KeyCheckFailed("EndpointConnectionError"))
        self.assertEqual(code, OK)
        self.assertIn("couldn't check the keys", out)
        self.assertIn("default", self.saved())

    def test_no_check_skips_sts(self):
        code, _, _, checked = self.set_key("--no-check")
        self.assertEqual(code, OK)
        checked.assert_not_called()
        self.assertIn("default", self.saved())

    def test_malformed_or_temporary_keys_save_nothing(self):
        cases = [(("ASIAIOSFODNN7EXAMPLE",), "temporary key ID"), (("not-a-key-id",), "access key ID"),
                 ((AWS_KEY_ID, "short/secret"), "secret access key"), (("",), "access key ID")]
        for entered, message in cases:
            with self.subTest(message=message, entered=entered[0]):
                code, _, err, checked = self.set_key(entered=entered)
                self.assertEqual(code, BAD_CONFIG)
                self.assertIn(message, err)
                checked.assert_not_called()
                self.assertFalse(self.credentials.exists())

    def test_replaces_default_and_keeps_other_profiles(self):
        self.credentials.parent.mkdir()
        self.credentials.write_text("[default]\naws_access_key_id = AKIAOLDOLDOLDOLDOLD1\n"
                                    "aws_secret_access_key = old\naws_session_token = stale\n\n"
                                    "[work]\naws_access_key_id = AKIAWORKWORKWORKWORK\n")
        code, out, _, _ = self.set_key()
        self.assertEqual(code, OK)
        self.assertIn("Replaced the [default] profile", out)
        self.assertEqual(self.saved(), {
            "default": {"aws_access_key_id": AWS_KEY_ID, "aws_secret_access_key": AWS_SECRET},
            "work": {"aws_access_key_id": "AKIAWORKWORKWORKWORK"},
        })

    def test_warns_about_root_keys(self):
        root = aws_credentials.CallerIdentity(account="123456789012", arn="arn:aws:iam::123456789012:root")
        code, out, _, _ = self.set_key(identity=root)
        self.assertEqual(code, OK)
        self.assertIn("root-user keys", out)

    def test_cancel_or_visible_input_saves_nothing(self):
        def visible_fallback(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return AWS_KEY_ID
        for entered, message in ((KeyboardInterrupt(),), "Cancelled"), (visible_fallback, "can't hide"):
            with self.subTest(message=message):
                code, _, err, _ = self.set_key(entered=entered)
                self.assertEqual(code, REFUSED)
                self.assertIn(message, err)
                self.assertFalse(self.credentials.exists())

    def test_warns_when_exported_aws_variables_would_win(self):
        os.environ["AWS_ACCESS_KEY_ID"] = "AKIAEXPORTEDEXPORTED"
        code, out, _, _ = self.set_key()
        self.assertEqual(code, OK)
        self.assertIn("unset AWS_ACCESS_KEY_ID", out)


class CheckAwsKeysTests(OfflineTestCase):
    def stubbed(self):
        client = boto3.client("sts", region_name="us-east-1", aws_access_key_id=AWS_KEY_ID,
                              aws_secret_access_key=AWS_SECRET)
        stubber = Stubber(client)
        stubber.activate()
        self.addCleanup(stubber.deactivate)
        return client, stubber

    def check(self, client):
        return aws_credentials.check_keys(AWS_KEY_ID, AWS_SECRET, client=client)

    def test_returns_account_and_arn(self):
        client, stubber = self.stubbed()
        stubber.add_response("get_caller_identity", {"UserId": "AIDAEXAMPLE", "Account": "123456789012",
                                                     "Arn": "arn:aws:iam::123456789012:user/pipeline"})
        self.assertEqual(self.check(client), IDENTITY)
        self.assertFalse(IDENTITY.is_root)

    def test_auth_errors_reject_the_keys(self):
        for code in ("InvalidClientTokenId", "SignatureDoesNotMatch"):
            with self.subTest(code=code):
                client, stubber = self.stubbed()
                stubber.add_client_error("get_caller_identity", service_error_code=code, http_status_code=403)
                with self.assertRaisesRegex(aws_credentials.KeyRejected, f"^{code}$"):
                    self.check(client)

    def test_other_failures_do_not_reject_the_keys(self):
        client, stubber = self.stubbed()
        stubber.add_client_error("get_caller_identity", service_error_code="Throttling", http_status_code=400)
        with self.assertRaisesRegex(aws_credentials.KeyCheckFailed, "^Throttling$"):
            self.check(client)
        client, _ = self.stubbed()
        with patch.object(client, "get_caller_identity",
                          side_effect=EndpointConnectionError(endpoint_url="https://sts.us-east-1.amazonaws.com")):
            with self.assertRaisesRegex(aws_credentials.KeyCheckFailed, "^EndpointConnectionError$"):
                self.check(client)

    def test_real_client_is_built_from_the_given_keys(self):
        # boto3.client is mocked, so no request is sent (and sockets are blocked anyway).
        with patch("boto3.client") as make_client:
            make_client.return_value.get_caller_identity.return_value = {"Account": "1", "Arn": "arn:aws:iam::1:root"}
            identity = aws_credentials.check_keys(AWS_KEY_ID, AWS_SECRET, "eu-west-1")
        self.assertTrue(identity.is_root)
        _, kwargs = make_client.call_args
        self.assertEqual((kwargs["region_name"], kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"]),
                         ("eu-west-1", AWS_KEY_ID, AWS_SECRET))
