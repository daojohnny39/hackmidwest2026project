"""Jev adapter (spec 2.0): typed questions in, validated typed answers out.

One `Jev` per stage. `ask()` sends one request and always returns a `JevCall`; it
never raises for provider, transport, or schema problems. On success every question
has a validated, normalized answer. On any failure `error` holds a sanitized code
and `answers` is empty, so the caller falls back for the whole decision.

Routes, picked by settings.jev_mode:
- fake: returns caller-supplied rule answers through the same validation path, labeled
  provider="fake" with no model. They are synthetic, not measured probabilities.
- typesafe: `typesafe_sdk.TypeSafeClient.system_one` (imported lazily).
- openrouter: POST https://openrouter.ai/api/alpha/decisions over httpx.
`off` has no client: callers use their rules directly.

Retries live in one place per route (the SDK's RetryPolicy, or the loop below) and
share the per-call deadline. Only metadata reaches run_log.jsonl.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from pathlib import Path

from pipeline.artifacts import log_event
from pipeline.config import Settings
from pipeline.contracts import JevCall

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
QUESTION_TYPES = ("noul", "choice", "score")
# Jev's documented context is 32K tokens for state plus questions. Stay well below it
# (roughly 4 characters per token).
MAX_INPUT_CHARS = 60_000
PROBABILITY_SUM_TOLERANCE = 0.05
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

Responder = Callable[[dict, dict], Mapping]


class JevAnswerError(ValueError):
    """A provider response that doesn't answer the questions as asked."""


# Question builders: wire-format dicts shared by both routes.


def noul(instructions: str, criteria: Mapping[str, str] | None = None) -> dict:
    question: dict = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        question["criteria"] = dict(criteria)
    return question


def choice(instructions: str, criteria: Mapping[str, str | None]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, criteria: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


def check_questions(questions: Mapping[str, dict]) -> None:
    """Reject malformed questions before any call; these are caller bugs, so they raise."""
    if not questions:
        raise ValueError("a Jev request needs at least one question")
    for qid, question in questions.items():
        if not isinstance(qid, str) or not qid:
            raise ValueError(f"question ID {qid!r} must be a nonempty string")
        kind = question.get("type")
        if kind not in QUESTION_TYPES:
            raise ValueError(f"question {qid}: type must be one of {', '.join(QUESTION_TYPES)}")
        if not isinstance(question.get("instructions"), str) or not question["instructions"].strip():
            raise ValueError(f"question {qid}: instructions are required")
        criteria = question.get("criteria")
        if kind == "noul" and criteria is not None and set(criteria) - {"true", "false"}:
            raise ValueError(f"question {qid}: noul criteria may only have 'true' and 'false'")
        if kind == "choice" and (not isinstance(criteria, Mapping) or len(criteria) < 2):
            raise ValueError(f"question {qid}: a choice needs at least two options")
        if kind == "score" and (not isinstance(criteria, list) or len(criteria) < 2):
            raise ValueError(f"question {qid}: a score needs at least two ordered levels")


# Answer validation (spec 2.0)


def _unit(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise JevAnswerError(f"{where} must be a finite number")
    if not 0 <= value <= 1:
        raise JevAnswerError(f"{where} must be between 0 and 1")
    return float(value)


def _distribution(raw: object, allowed: list[str], where: str) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise JevAnswerError(f"{where} probabilities are missing")
    probabilities = {}
    for key, value in raw.items():
        key = str(key)
        if key not in allowed:
            raise JevAnswerError(f"{where} gives a probability for unknown option {key!r}")
        probabilities[key] = _unit(value, f"{where} probability {key!r}")
    if abs(sum(probabilities.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
        raise JevAnswerError(f"{where} probabilities sum to {sum(probabilities.values()):.3f}, not 1")
    return {key: probabilities.get(key, 0.0) for key in allowed}


def validate_answer(qid: str, question: dict, answer: object) -> dict:
    """One provider answer -> the normalized dict stored in JevCall.answers."""
    if not isinstance(answer, Mapping):
        raise JevAnswerError(f"answer {qid} is not an object")
    kind = question["type"]
    if answer.get("type") != kind:
        raise JevAnswerError(f"answer {qid} has type {answer.get('type')!r}, expected {kind}")
    if kind == "noul":
        return {"type": "noul", "noul": _unit(answer.get("noul"), f"answer {qid} noul")}
    if kind == "choice":
        options = [str(option) for option in question["criteria"]]
        picked = answer.get("choice")
        if picked not in options:
            raise JevAnswerError(f"answer {qid} chose {picked!r}, which is not an option")
        return {
            "type": "choice",
            "choice": picked,
            "confidence": _unit(answer.get("confidence"), f"answer {qid} confidence"),
            "probabilities": _distribution(answer.get("probabilities"), options, f"answer {qid}"),
        }
    levels = [str(index) for index in range(len(question["criteria"]))]
    value = answer.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise JevAnswerError(f"answer {qid} score must be a finite number")
    if not 0 <= value <= len(levels) - 1:
        raise JevAnswerError(f"answer {qid} score {value} is outside levels 0-{len(levels) - 1}")
    return {
        "type": "score",
        "score": float(value),
        "confidence": _unit(answer.get("confidence"), f"answer {qid} confidence"),
        "probabilities": _distribution(answer.get("probabilities"), levels, f"answer {qid}"),
    }


def validate_answers(questions: Mapping[str, dict], answers: object) -> dict[str, dict]:
    """Every asked question answered exactly once with the right type and bounds."""
    if not isinstance(answers, Mapping):
        raise JevAnswerError("answers is not an object")
    if missing := sorted(set(questions) - set(answers)):
        raise JevAnswerError(f"no answer for {', '.join(missing)}")
    if extra := sorted(set(answers) - set(questions)):
        raise JevAnswerError(f"answers for questions that were not asked: {', '.join(map(str, extra))}")
    return {qid: validate_answer(qid, questions[qid], answers[qid]) for qid in questions}


def _usage(raw: object) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    }


def _status_code(status: int) -> str:
    if status in (400, 422):
        return "invalid_request"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "api_error"


class _CallFailed(Exception):
    def __init__(self, code: str, status: int | None = None, request_id: str | None = None, retries: int = 0):
        super().__init__(code)
        self.code, self.status, self.request_id, self.retries = code, status, request_id, retries


class Jev:
    """A Jev client for one stage. See the module docstring for routes and failure behavior."""

    def __init__(
        self,
        settings: Settings,
        *,
        run_dir: Path | None = None,
        stage: int = 2,
        responder: Responder | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        transport=None,
    ):
        if settings.jev_mode == "off":
            raise ValueError("JEV_MODE=off has no Jev client; use the rules directly")
        if settings.jev_mode in ("typesafe", "openrouter") and not settings.models:
            raise ValueError(f"JEV_MODE={settings.jev_mode} makes paid calls and needs --models")
        self.provider = settings.jev_mode
        self.model = settings.jev_model
        self._key = settings.typesafe_api_key if self.provider == "typesafe" else settings.openrouter_api_key
        self.run_dir, self.stage = run_dir, stage
        self.responder, self.transport = responder, transport
        self.timeout, self.max_retries = timeout, max_retries

    @property
    def real(self) -> bool:
        """True when answers come from the model, so a low probability may veto a merge."""
        return self.provider != "fake"

    def ask(self, state: dict, questions: dict[str, dict], *, fake_answers: Mapping[str, dict] | None = None) -> JevCall:
        check_questions(questions)
        started = time.monotonic()
        model = request_id = None
        status: int | str | None = None
        retries = 0
        try:
            size = len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False))
            if size > MAX_INPUT_CHARS:
                raise _CallFailed("input_too_large")
            if self.provider == "fake":
                raw: Mapping = {"model": None, "answers": dict(fake_answers or {}), "usage": {}}
            elif self.responder is not None:
                raw = self.responder(state, questions)
            elif self.provider == "typesafe":
                raw = self._typesafe(state, questions)
            else:
                raw, request_id, retries = self._openrouter(state, questions)
            if not isinstance(raw, Mapping):
                raise JevAnswerError("response is not an object")
            model = raw.get("model") if isinstance(raw.get("model"), str) else None
            request_id = request_id or (raw.get("id") if isinstance(raw.get("id"), str) else None)
            call = JevCall(
                provider=self.provider,
                model=model,
                request_id=request_id,
                answers=validate_answers(questions, raw.get("answers")),
                usage=_usage(raw.get("usage")),
            )
            status = "ok"
        except _CallFailed as failure:
            status = failure.status or failure.code
            retries = failure.retries
            call = JevCall(provider=self.provider, request_id=failure.request_id or request_id, error=failure.code)
        except JevAnswerError:
            status = "invalid_response"
            call = JevCall(provider=self.provider, model=model, request_id=request_id, error="invalid_response")
        except Exception as exc:  # an SDK, transport, or responder failure must not end the stage
            code, http_status, sdk_request_id = _sdk_error(exc)
            status = http_status or code
            call = JevCall(provider=self.provider, request_id=sdk_request_id, error=code)
        if self.run_dir is not None:
            log_event(
                self.run_dir,
                stage=self.stage,
                backend=f"jev:{self.provider}",
                endpoint={"typesafe": TYPESAFE_ENDPOINT, "openrouter": OPENROUTER_URL}.get(self.provider),
                status=status,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                retries=retries,
                code=call.error,
                model=call.model,
                usage=call.usage or None,
            )
        return call

    def _typesafe(self, state: dict, questions: dict[str, dict]) -> dict:
        from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

        builders = {"noul": Noul, "choice": Choice, "score": Score}
        sdk_questions = {
            qid: builders[q["type"]](**{k: v for k, v in q.items() if k != "type"}) for qid, q in questions.items()
        }
        retry = RetryPolicy(max_retries=self.max_retries, timeout=self.timeout)
        options = {"api_key": self._key, "model": self.model, "retry": retry, "timeout": self.timeout}
        if self.transport is not None:
            options["transport"] = self.transport
        with TypeSafeClient(**options) as client:
            response = client.system_one(state=state, questions=sdk_questions)
        return response.model_dump(mode="json")

    def _openrouter(self, state: dict, questions: dict[str, dict]) -> tuple[dict, str | None, int]:
        import httpx

        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        deadline = time.monotonic() + self.timeout
        request_id = None
        with httpx.Client(transport=self.transport, follow_redirects=False) as client:
            for attempt in range(self.max_retries + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _CallFailed("timeout", request_id=request_id, retries=attempt)
                try:
                    response = client.post(OPENROUTER_URL, json=body, headers=headers, timeout=remaining)
                except httpx.TimeoutException:
                    raise _CallFailed("timeout", retries=attempt) from None
                except httpx.HTTPError:
                    raise _CallFailed("connection_error", retries=attempt) from None
                request_id = response.headers.get("x-request-id") or request_id
                if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                    wait = _retry_after(response.headers.get("retry-after"))
                    if wait is None:
                        wait = min(0.5 * 2**attempt, 4.0) * (1 - 0.25 * random.random())
                    if wait >= deadline - time.monotonic():
                        raise _CallFailed(_status_code(response.status_code), response.status_code, request_id, attempt)
                    time.sleep(wait)
                    continue
                if response.status_code >= 400:
                    raise _CallFailed(_status_code(response.status_code), response.status_code, request_id, attempt)
                try:
                    payload = response.json()
                except ValueError:
                    raise _CallFailed("invalid_response", response.status_code, request_id, attempt) from None
                return payload, request_id, attempt
        raise _CallFailed("timeout", request_id=request_id, retries=self.max_retries)  # not reached


class KeyRejected(Exception):
    """TypeSafe refused the key."""


class KeyCheckFailed(Exception):
    """TypeSafe couldn't be reached or answered unexpectedly; the key may still be valid."""


def check_typesafe_key(api_key: str, *, transport=None) -> list[str]:
    """List TypeSafe's models with this key: an authenticated GET that sends no decision request.

    Returns the model names. Errors carry the failure type or HTTP status, never the key.
    """
    from typesafe_sdk import (
        RetryPolicy, TypeSafeAPIError, TypeSafeAuthenticationError, TypeSafeClient, TypeSafeError,
        TypeSafePermissionDeniedError,
    )

    options = {"api_key": api_key, "retry": RetryPolicy(max_retries=0), "timeout": 10.0}
    if transport is not None:
        options["transport"] = transport
    try:
        with TypeSafeClient(**options) as client:
            listing = client.models.list()
    except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
        raise KeyRejected(f"HTTP {exc.status}") from None
    except TypeSafeAPIError as exc:
        raise KeyCheckFailed(f"HTTP {exc.status}") from None
    except TypeSafeError as exc:
        raise KeyCheckFailed(type(exc).__name__) from None
    return [model.name for model in listing.models]


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _sdk_error(exc: Exception) -> tuple[str, int | None, str | None]:
    """Map an SDK or responder exception to (code, HTTP status, request ID) without its message."""
    try:
        import typesafe_sdk as sdk
    except ImportError:  # pragma: no cover - the SDK is a pinned dependency
        sdk = None
    if sdk is not None:
        if isinstance(exc, sdk.TypeSafeAPIResponseValidationError):
            return "invalid_response", getattr(exc, "status", None), getattr(exc, "request_id", None)
        if isinstance(exc, sdk.TypeSafeAPIError):
            return _status_code(exc.status), exc.status, getattr(exc, "request_id", None)
        if isinstance(exc, sdk.TypeSafeAPITimeoutError):
            return "timeout", None, None
        if isinstance(exc, sdk.TypeSafeAPIConnectionError):
            return "connection_error", None, None
    if isinstance(exc, TimeoutError):
        return "timeout", None, None
    if isinstance(exc, ConnectionError):
        return "connection_error", None, None
    return "unavailable", None, None
