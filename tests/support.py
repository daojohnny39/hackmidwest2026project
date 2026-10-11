"""Shared offline test harness for pipeline tests.

OfflineTestCase forces the mock/fake/off modes regardless of .env, clears every
credential variable, stops load_settings() from reading the repo's .env (which
may hold real keys) and boto3 from reading ~/.aws, and makes any non-loopback
socket connection fail.
"""

import ipaddress
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mock_internet.server import create_server
from pipeline.config import load_settings
from pipeline.trace import TRACE_FILE

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CAPTURES = REPO_ROOT / "tests" / "fixtures" / "captures"

PIPELINE_ENV = (
    "SEARCH_BACKEND", "MOCK_INTERNET_URL", "MOCK_SCENARIO", "MOCK_DELAY_MS", "FACECHECK_API_KEY", "FACECHECK_DEMO",
    "REKOGNITION_ENGINES", "REKOGNITION_MIN_SIMILARITY", "SERPAPI_KEY", "IMAGE_HOST", "IMAGE_BUCKET", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "JEV_MODE", "JEV_MODEL",
    "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "LLM_MODE", "LLM_MODEL", "ANTHROPIC_API_KEY",
)

# boto3 reads ~/.aws (where `set-key aws` saves keys) unless these point elsewhere; os.devnull is never a file it loads.
# With no credentials it would also ask the EC2 instance metadata service, a network call.
OFFLINE_ENV = {"SEARCH_BACKEND": "mock", "JEV_MODE": "fake", "LLM_MODE": "off",
               "AWS_SHARED_CREDENTIALS_FILE": os.devnull, "AWS_CONFIG_FILE": os.devnull,
               "AWS_EC2_METADATA_DISABLED": "true"}


class ExternalNetworkBlocked(RuntimeError):
    pass


def read_trace(folder: Path) -> list[dict]:
    """The parsed lines of folder/trace.jsonl."""
    return [json.loads(line) for line in (Path(folder) / TRACE_FILE).read_text().splitlines()]


def _is_loopback(address):
    host = address[0] if isinstance(address, tuple) else address
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
        raise ExternalNetworkBlocked(f"test tried to reach {address!r}")
    return _real_connect(sock, address)


def _guarded_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
        raise ExternalNetworkBlocked(f"test tried to reach {address!r}")
    return _real_connect_ex(sock, address)


class OfflineTestCase(unittest.TestCase):
    """Base class: offline env, no credentials, loopback-only sockets."""

    def setUp(self):
        super().setUp()
        cleared = {key: value for key, value in os.environ.items() if key not in PIPELINE_ENV}
        env = patch.dict(os.environ, {**cleared, **OFFLINE_ENV}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        # Callers that don't pass dotenv_path (the CLI) would otherwise read the repo's real .env.
        dotenv = patch.dict(load_settings.__kwdefaults__, {"dotenv_path": None})
        dotenv.start()
        self.addCleanup(dotenv.stop)
        for name, guard in (("connect", _guarded_connect), ("connect_ex", _guarded_connect_ex)):
            patcher = patch.object(socket.socket, name, guard)
            patcher.start()
            self.addCleanup(patcher.stop)


class MockServerTestCase(OfflineTestCase):
    """OfflineTestCase plus one seeded mock internet per test class, on a free loopback port.

    self.mock_url is its origin, e.g. http://127.0.0.1:50123. Request counts are in
    self.mock_server.counts (route:/fetch, route:/api/reverse-image, ...).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._mock_folder = tempfile.TemporaryDirectory()
        cls.mock_server = create_server(port=0, db_path=Path(cls._mock_folder.name) / "mock.sqlite3")
        cls._mock_thread = threading.Thread(target=cls.mock_server.serve_forever, daemon=True)
        cls._mock_thread.start()
        cls.mock_url = cls.mock_server.base_url

    @classmethod
    def tearDownClass(cls):
        cls.mock_server.shutdown()
        cls.mock_server.server_close()
        cls._mock_thread.join()
        cls._mock_folder.cleanup()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        with self.mock_server.counts_lock:
            self.mock_server.counts.clear()

    def mock_count(self, route: str) -> int:
        with self.mock_server.counts_lock:
            return self.mock_server.counts[f"route:{route}"]
