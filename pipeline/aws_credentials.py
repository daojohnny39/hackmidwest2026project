"""AWS access keys for `set-key aws`: a free check and a save to the shared credentials file.

The pipeline never reads AWS keys itself. S3 hosting (hosting.py) and Bedrock
(llm.py) use boto3's standard credential chain, so the keys go where that chain
looks: the [default] profile of ~/.aws/credentials (or AWS_SHARED_CREDENTIALS_FILE),
outside the repo, with mode 0600. The check is STS GetCallerIdentity, which is free
and needs no IAM permissions; it reports the account and ARN, never the keys.
Errors carry only an AWS error code or exception type.
"""

from __future__ import annotations

import configparser
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

ACCESS_KEY_ID = re.compile(r"AKIA[A-Z0-9]{16}")
TEMPORARY_KEY_ID = re.compile(r"ASIA[A-Z0-9]{16}")
SECRET_ACCESS_KEY = re.compile(r"[A-Za-z0-9/+=]{40}")
REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d")
PROFILE = "default"
CHECK_REGION = "us-east-1"  # STS answers GetCallerIdentity in every region; used when none is set
# AWS's answers for a key ID it doesn't know, a wrong secret, or a disabled or expired key.
REJECTED_CODES = {"InvalidClientTokenId", "SignatureDoesNotMatch", "UnrecognizedClientException",
                  "AuthFailure", "ExpiredToken"}


class KeyRejected(Exception):
    """AWS refused the keys."""


class KeyCheckFailed(Exception):
    """AWS couldn't be reached or answered unexpectedly; the keys may still be valid."""


@dataclass(frozen=True)
class CallerIdentity:
    account: str
    arn: str

    @property
    def is_root(self) -> bool:
        return self.arn.endswith(":root")


def default_credentials_file() -> Path:
    """The file boto3 reads: AWS_SHARED_CREDENTIALS_FILE if set, else ~/.aws/credentials."""
    return Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or "~/.aws/credentials").expanduser()


def check_keys(access_key_id: str, secret_access_key: str, region: str | None = None, *,
               client=None) -> CallerIdentity:
    """Ask STS who these keys belong to. `client` lets tests pass a stubbed STS client."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    if client is None:
        client = boto3.client(
            "sts", region_name=region or CHECK_REGION,
            aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1, "mode": "standard"}),
        )
    try:
        reply = client.get_caller_identity()
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "error")
        raise (KeyRejected if code in REJECTED_CODES else KeyCheckFailed)(code) from None
    except BotoCoreError as exc:
        raise KeyCheckFailed(type(exc).__name__) from None
    return CallerIdentity(account=str(reply["Account"]), arn=str(reply["Arn"]))


def save_keys(path: Path, access_key_id: str, secret_access_key: str, *, profile: str = PROFILE) -> bool:
    """Write the keys into `profile`, keeping other profiles; return True if the profile already existed.

    A stale session token in the profile is dropped. Comments in an existing file
    are not kept (configparser rewrites it). The file is replaced atomically with
    mode 0600, in a 0700 folder when the folder is new.
    """
    parser = configparser.RawConfigParser(default_section="__no_defaults__")
    if path.exists():
        parser.read(path, encoding="utf-8")
    existed = parser.has_section(profile)
    if not existed:
        parser.add_section(profile)
    parser.set(profile, "aws_access_key_id", access_key_id)
    parser.set(profile, "aws_secret_access_key", secret_access_key)
    parser.remove_option(profile, "aws_session_token")

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            parser.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return existed
