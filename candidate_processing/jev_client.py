"""Server-side TypeSafe SDK adapter; one retry policy and a total deadline."""

import asyncio
from dataclasses import dataclass
from importlib.metadata import version
import math
import os
import re
from typing import Protocol

from typesafe_sdk import (
    AsyncTypeSafeClient, Choice, ChoiceAnswer, RetryPolicy,
    TypeSafeAPIConnectionError, TypeSafeAPIError, TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError, TypeSafeError,
)

from .contracts import Failure

JEV_DEADLINE_SECONDS = 10.0


class JevFailure(Exception):
    """Only application-owned messages cross the provider boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.failure = Failure(stage="classification", code=code, message=message, retryable=retryable)
        super().__init__(code)


@dataclass(frozen=True)
class JevDecision:
    model: str
    choices: dict[str, str]


class JevProvider(Protocol):
    sdk_version: str

    async def decide(self, state: dict, questions: dict[str, Choice]) -> JevDecision: ...


class JevClient:
    """Reuse within an async context manager. Import/initialization makes no requests.

    The transport seam supports SDK-level offline tests. Candidate data cannot
    choose the endpoint, credentials, model, timeout or retry settings.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 transport=None, deadline_seconds: float = JEV_DEADLINE_SECONDS):
        if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float))
                or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= JEV_DEADLINE_SECONDS):
            raise ValueError("Jev deadline must be greater than zero and at most 10 seconds")
        self.sdk_version = f"typesafe-sdk-{version('typesafe-sdk')}"
        self.deadline_seconds = deadline_seconds
        self._client = None
        self._closed = False
        self._configuration_error = None
        key = (os.environ.get("TYPESAFE_API_KEY", "") if api_key is None else api_key).strip()
        if not key:
            self._configuration_error = JevFailure("jev_credentials_missing", "Server-side Jev credentials are not configured.")
            return
        selected_model = model if model is not None else os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest").strip() or "jev-latest"
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", selected_model):
            self._configuration_error = JevFailure("jev_configuration_invalid", "Server-side Jev configuration is invalid.")
            return
        try:
            self._client = AsyncTypeSafeClient(
                api_key=key, model=selected_model, base_url="https://api.typesafe.ai",
                transport=transport, timeout=min(4.0, deadline_seconds),
                retry=RetryPolicy(max_retries=1, timeout=deadline_seconds,
                                  backoff_initial=0.25, backoff_max=0.5),
            )
        except (TypeSafeError, ValueError):
            self._configuration_error = JevFailure("jev_configuration_invalid", "Server-side Jev configuration is invalid.")

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError("JevClient is closed")
        return self

    async def __aexit__(self, *_):
        self._closed = True
        if self._client is not None:
            await self._client.aclose()

    async def decide(self, state: dict, questions: dict[str, Choice]) -> JevDecision:
        if self._closed:
            raise JevFailure("jev_client_closed", "The Jev client has been closed.")
        if self._configuration_error:
            raise self._configuration_error
        try:
            # SDK retry timeout only bounds retry scheduling. This also bounds
            # in-flight I/O, backoff and response handling across both attempts.
            async with asyncio.timeout(self.deadline_seconds):
                response = await self._client.system_one(state=state, questions=questions)
                if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", response.model)
                        or set(response.answers) != set(questions)):
                    raise ValueError("Invalid model or answer keys")
                choices = {}
                for key, question in questions.items():
                    answer = response.answers[key]
                    if not isinstance(answer, ChoiceAnswer) or answer.choice not in question.criteria:
                        raise ValueError("Invalid answer type or choice")
                    probabilities = answer.probabilities
                    if (set(probabilities) != set(question.criteria)
                            or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities.values())
                            or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.01)
                            or probabilities[answer.choice] < max(probabilities.values())
                            or not math.isfinite(answer.confidence) or not 0 <= answer.confidence <= 1):
                        raise ValueError("Invalid probability distribution")
                    # Provider probabilities describe a text decision. They are
                    # deliberately not exposed as identity confidence.
                    choices[key] = answer.choice
                return JevDecision(model=response.model, choices=choices)
        except (TimeoutError, TypeSafeAPITimeoutError):
            raise JevFailure("jev_timeout", "Jev classification exceeded its time limit.", retryable=True) from None
        except TypeSafeAPIResponseValidationError:
            raise JevFailure("jev_invalid_response", "Jev returned an invalid classification response.") from None
        except TypeSafeAPIConnectionError:
            raise JevFailure("jev_connection_failed", "Jev could not be reached after bounded retries.", retryable=True) from None
        except TypeSafeAPIError as error:
            if error.status in {401, 403}:
                raise JevFailure("jev_authentication_failed", "Jev rejected the server credentials or their permissions.") from None
            if error.status == 429:
                raise JevFailure("jev_rate_limited", "Jev rate limited the request after bounded retries.", retryable=True) from None
            if error.status == 408 or 500 <= error.status < 600:
                raise JevFailure("jev_service_unavailable", "Jev is unavailable after bounded retries.", retryable=True) from None
            raise JevFailure("jev_request_rejected", "Jev rejected the classification request.") from None
        except (ValueError, TypeError, TypeSafeError):
            raise JevFailure("jev_invalid_response", "Jev returned an invalid classification response.") from None
