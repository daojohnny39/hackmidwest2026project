"""SerpApi Account API: check a key and read its search quota without spending a search.

The Account API is free and doesn't count toward the monthly quota
(https://serpapi.com/account-api). Its response echoes the key and the account
email, so only the quota fields below are kept. The request URL carries the key,
so errors report the failure type, never the exception text.
"""

from __future__ import annotations

import httpx

ACCOUNT_URL = "https://serpapi.com/account.json"
TIMEOUT_S = 10.0
QUOTA_FIELDS = ("plan_name", "searches_per_month", "total_searches_left", "account_rate_limit_per_hour")


class KeyRejected(Exception):
    """SerpApi refused the key."""


class AccountCheckFailed(Exception):
    """SerpApi couldn't be reached or answered unexpectedly; the key may still be valid."""


def account_quota(api_key: str) -> dict:
    """Return the allowlisted quota fields for this key."""
    try:
        response = httpx.get(ACCOUNT_URL, params={"api_key": api_key}, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise AccountCheckFailed(type(exc).__name__) from None
    if response.status_code in (401, 403):
        raise KeyRejected(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise AccountCheckFailed(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise AccountCheckFailed("response was not JSON") from None
    if not isinstance(data, dict):
        raise AccountCheckFailed("unexpected response")
    if data.get("error"):
        raise KeyRejected(str(data["error"]).replace(api_key, "***"))
    return {name: data[name] for name in QUOTA_FIELDS if name in data}


def describe_quota(quota: dict) -> str:
    parts = [str(quota.get("plan_name") or "unknown plan")]
    if "total_searches_left" in quota:
        left = f"{quota['total_searches_left']} searches left"
        if "searches_per_month" in quota:
            left += f" ({quota['searches_per_month']} per month)"
        parts.append(left)
    if "account_rate_limit_per_hour" in quota:
        parts.append(f"limit {quota['account_rate_limit_per_hour']} per hour")
    return ", ".join(parts)
