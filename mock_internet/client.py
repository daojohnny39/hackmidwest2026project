"""Local-only adapter for future agent search and scrape tools.

Uses a direct loopback HTTP connection, ignores proxy variables, and does not
follow redirects. A missing fixture or stopped server never triggers live search.
"""

import http.client
import ipaddress
import json
import os
from urllib.parse import urlencode, urlsplit


class MockHTTPError(RuntimeError):
    def __init__(self, status, payload, headers):
        self.status = status
        self.payload = payload
        self.headers = headers
        super().__init__(f"Mock internet HTTP {status}: {payload.get('error', payload.get('title', 'Request failed'))}")


class MockInternetClient:
    def __init__(self, base_url=None, *, timeout=10):
        parsed = urlsplit(base_url or os.environ.get("MOCK_INTERNET_URL", "http://127.0.0.1:8765"))
        host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        try:
            local = host and ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if (parsed.scheme != "http" or not local or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("MOCK_INTERNET_URL must be an HTTP loopback origin, e.g. http://127.0.0.1:8765")
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError("MOCK_INTERNET_URL port must be between 1 and 65535")
        self.host, self.port, self.timeout = host, port, timeout
        self.base_url = f"http://{'[' + host + ']' if ':' in host else host}:{port}"

    def _get(self, path, params=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("GET", path + ("?" + urlencode(params) if params else ""), headers={"Accept": "application/json"})
            response = connection.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError("Local server did not return mock JSON; check MOCK_INTERNET_URL") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("Local server returned an unexpected response")
            if response.getheader("X-Mock-Internet") != "true":
                raise RuntimeError("Local server is not the mock internet service")
            if response.status >= 300:
                raise MockHTTPError(response.status, payload, dict(response.getheaders()))
            return payload
        finally:
            connection.close()

    def search(self, query, **options):
        """Return organic_results. Options: num, start, site, platform, scenario, delay_ms."""
        return self._get("/search.json", {"q": query, **options})

    def reverse_image(self, image_id, **options):
        """Return canned image_results for a /fixtures ID; no image analysis occurs."""
        return self._get("/api/reverse-image", {"image_id": image_id, **options})

    def fetch(self, url, **options):
        """Fetch a synthetic .test URL or a local result URL from the database."""
        return self._get("/fetch", {"url": url, **options})

    def fixtures(self):
        return self._get("/fixtures")

    def stats(self):
        return self._get("/stats")

