"""Mock search backend: wraps MockInternetClient (spec 1.1, --mock and tests).

The client is synchronous, so each call runs in a worker thread. Reverse searches
send the bare fixture ID (`alex-front`, never `mock://alex-front`) with the
scenario and MOCK_DELAY_MS; page fetches send the original URL with the scenario
only, so the delay applies to searches alone. Mock HTTP errors map to
BackendError from their status and headers; any HTML in an error payload is
ignored.
"""

from __future__ import annotations

import asyncio
import socket

from pydantic import ValidationError

from mock_internet.client import MockHTTPError, MockInternetClient
from pipeline.contracts import CaptureImage, FetchedPage
from pipeline.platforms import normalize_url
from pipeline.search.base import BackendError, status_error


class MockSearchBackend:
    name = "mock"
    search_endpoint = "/api/reverse-image"  # run_log endpoint paths
    fetch_endpoint = "/fetch"

    def __init__(self, base_url: str, *, scenario: str = "default", delay_ms: int = 0, timeout: float = 10.0):
        self.client = MockInternetClient(base_url, timeout=timeout)
        self.origin = self.client.base_url  # the origin the server writes into its pages
        self.scenario = scenario
        self.delay_ms = delay_ms

    async def reverse_image(self, image: CaptureImage) -> dict:
        if not image.mock_fixture:
            raise BackendError("invalid_request")
        options = {"scenario": self.scenario}
        if self.delay_ms:
            options["delay_ms"] = self.delay_ms
        return await asyncio.to_thread(self._call, self.client.reverse_image, image.mock_fixture, **options)

    async def fetch(self, url: str) -> FetchedPage:
        payload = await asyncio.to_thread(self._call, self.client.fetch, url, scenario=self.scenario)
        try:
            return FetchedPage(
                requested_url=url,
                final_url=normalize_url(payload["url"]) or url,
                fetched_url=payload["local_url"],
                status_code=payload["status_code"],
                content_type=payload["content_type"],
                html=payload["html"],
            )
        except (KeyError, TypeError, ValidationError):
            raise BackendError("invalid_response") from None

    async def release(self, image: CaptureImage) -> None:
        return None  # nothing is hosted in mock mode

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _call(method, *args, **kwargs) -> dict:
        try:
            return method(*args, **kwargs)
        except MockHTTPError as error:
            raise status_error(error.status, error.headers) from None
        except (TimeoutError, socket.timeout):
            raise BackendError("timeout") from None
        except OSError:
            raise BackendError("connection_error") from None
        except RuntimeError:  # not the mock server, or not JSON
            raise BackendError("invalid_response") from None
