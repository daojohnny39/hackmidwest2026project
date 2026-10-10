"""Shared offline test harness for pipeline tests.

OfflineTestCase forces the mock/fake/off modes regardless of .env, clears every
credential variable, and makes any non-loopback socket connection fail.
"""

import ipaddress
import os
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CAPTURES = REPO_ROOT / "tests" / "fixtures" / "captures"

PIPELINE_ENV = (
    "SEARCH_BACKEND", "MOCK_INTERNET_URL", "MOCK_SCENARIO", "MOCK_DELAY_MS",
    "SERPAPI_KEY", "IMAGE_BUCKET", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_PROFILE", "JEV_MODE", "JEV_MODEL", "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY", "LLM_MODE", "LLM_MODEL", "ANTHROPIC_API_KEY",
)

OFFLINE_ENV = {"SEARCH_BACKEND": "mock", "JEV_MODE": "fake", "LLM_MODE": "off"}


class ExternalNetworkBlocked(RuntimeError):
    pass


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
        for name, guard in (("connect", _guarded_connect), ("connect_ex", _guarded_connect_ex)):
            patcher = patch.object(socket.socket, name, guard)
            patcher.start()
            self.addCleanup(patcher.stop)
