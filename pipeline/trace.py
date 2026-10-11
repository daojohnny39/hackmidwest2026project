"""Per-run debug trace: runs/<run_id>/trace.jsonl.

run_log.jsonl stays metadata only (spec 0.5). The trace is the full record for finding
where a run went wrong: one JSON object per line, each with ts, stage, level, and event.

- stage_start and stage_end around every stage the CLI runs, with the settings, the
  duration, and the exception if the stage raised one.
- Each Strands agent's system prompt, every message added to its conversation (prompt,
  text, reasoning, tool calls, tool results, structured output), each model call's stop
  reason or error, each tool call's status and duration, and its usage at the end
  (watch_agent).
- Searches and page fetches with their URLs, Jev requests and answers, and every error the
  pipeline turns into a fallback or a code, with the exception chain behind it. An error
  raised `from None` still keeps its cause on __context__, and describe() records it.
- Log records from libraries (Strands, httpx, Anthropic, botocore, ...) at INFO and above.

The trace holds evidence (URLs, page text, names, prompts), so it lives in the run folder,
which is gitignored and deleted by `purge`. Every value goes through artifacts.scrub(),
the run_log scrubber. Outside tracing() nothing is written.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pipeline.artifacts import scrub

TRACE_FILE = "trace.jsonl"
MAX_TEXT = 20_000  # characters kept per string
MAX_CHAIN = 5  # exceptions recorded per error, outermost first
REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("pipeline.trace")


def event(name: str, /, **fields: object) -> None:
    """Record one trace event. Does nothing outside tracing()."""
    logger.info(name, extra={"trace": fields})


def error(name: str, exc: BaseException | None = None, /, **fields: object) -> None:
    """Record an error the pipeline handled (a fallback, a failed fetch, ...), with its exception chain."""
    logger.warning(name, exc_info=exc, extra={"trace": fields})


@contextmanager
def tracing(run_dir: Path, stage: int, /, **fields: object) -> Iterator[None]:
    """Append trace events and library log records to run_dir/trace.jsonl while one stage runs.

    Records stage_start (with fields) and stage_end (outcome and duration), and re-raises
    anything the stage raises after recording it.
    """
    root = logging.getLogger()
    handler = _TraceHandler(Path(run_dir) / TRACE_FILE, stage)
    added = [handler]
    if not root.handlers:
        # With no handler configured, Python prints library warnings to stderr; keep doing that.
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.addFilter(lambda record: not record.name.startswith("pipeline."))
        added.append(console)
    level = root.level
    root.setLevel(min(level, logging.INFO))
    for item in added:
        root.addHandler(item)
    started = time.monotonic()
    try:
        event("stage_start", **fields)
        yield
    except BaseException as exc:
        outcome = "error" if isinstance(exc, Exception) else "interrupted"
        logger.error("stage_end", exc_info=exc, extra={"trace": {"outcome": outcome, "duration_ms": _ms(started)}})
        raise
    else:
        event("stage_end", outcome="ok", duration_ms=_ms(started))
    finally:
        for item in added:
            root.removeHandler(item)
        root.setLevel(level)
        handler.close()


def watch_agent(agent: Any) -> None:
    """Trace one Strands agent: its prompts, messages, model calls, tool calls, and usage."""
    from strands.hooks import (
        AfterInvocationEvent, AfterModelCallEvent, AfterToolCallEvent, BeforeInvocationEvent, BeforeModelCallEvent,
        MessageAddedEvent,
    )

    name = agent.name
    started: dict[str, float] = {}

    def invocation_started(_: BeforeInvocationEvent) -> None:
        started["invocation"] = time.monotonic()
        event("agent_start", agent=name, model=_model_id(agent), tools=agent.tool_names,
              system_prompt=agent.system_prompt)

    def message_added(hook: MessageAddedEvent) -> None:
        event("agent_message", agent=name, role=hook.message.get("role"), content=hook.message.get("content"))

    def model_called(_: BeforeModelCallEvent) -> None:
        started["model"] = time.monotonic()

    def model_returned(hook: AfterModelCallEvent) -> None:
        duration = _ms(started.pop("model", time.monotonic()))
        if hook.exception is not None:
            error("model_call_failed", hook.exception, agent=name, duration_ms=duration)
        elif hook.stop_response is not None:
            event("model_call", agent=name, stop_reason=hook.stop_response.stop_reason, duration_ms=duration)

    def tool_returned(hook: AfterToolCallEvent) -> None:
        fields = {"agent": name, "tool": hook.tool_use.get("name"), "tool_use_id": hook.tool_use.get("toolUseId"),
                  "status": (hook.result or {}).get("status"),
                  "duration_ms": round(hook.duration * 1000, 1) if hook.duration is not None else None}
        if hook.exception is not None:
            error("tool_call_failed", hook.exception, **fields)
        else:
            event("tool_call", **fields)

    def invocation_ended(hook: AfterInvocationEvent) -> None:
        fields: dict[str, object] = {"agent": name, "duration_ms": _ms(started.pop("invocation", time.monotonic()))}
        result = hook.result
        if result is None:
            event("agent_end", outcome="no_result", **fields)  # it raised; the caller records the error
            return
        metrics = result.metrics
        event("agent_end", outcome="ok", stop_reason=result.stop_reason, cycles=metrics.cycle_count,
              usage=metrics.accumulated_usage, latency_ms=metrics.accumulated_metrics.get("latencyMs"),
              structured_output=result.structured_output, **fields)

    agent.add_hook(invocation_started, BeforeInvocationEvent)
    agent.add_hook(message_added, MessageAddedEvent)
    agent.add_hook(model_called, BeforeModelCallEvent)
    agent.add_hook(model_returned, AfterModelCallEvent)
    agent.add_hook(tool_returned, AfterToolCallEvent)
    agent.add_hook(invocation_ended, AfterInvocationEvent)


def describe(exc: BaseException) -> dict:
    """An exception and the ones behind it, outermost first, with where each was raised.

    Follows __cause__, else __context__ even when `raise ... from None` hid it, so a sanitized
    BackendError("connection_error") still shows the httpx error that caused it.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < MAX_CHAIN and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    return {
        "chain": [{"type": _type_name(item), "message": str(item), "at": _raised_at(item)} for item in chain],
        "traceback": "".join(line for item in reversed(chain) for line in traceback.format_exception(item, chain=False)),
    }


class _TraceHandler(logging.FileHandler):
    """Writes each record as one scrubbed JSON line; trace events keep their fields."""

    def __init__(self, path: Path, stage: int):
        super().__init__(path, encoding="utf-8", delay=True)
        self.stage = stage

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "stage": self.stage,
            "level": record.levelname.lower(),
        }
        fields = getattr(record, "trace", None)
        if isinstance(fields, dict):
            entry["event"] = record.msg
            entry.update((key, value) for key, value in fields.items() if value is not None)
        else:
            entry.update(event="log", logger=record.name, message=record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            entry["error"] = describe(record.exc_info[1])
        return json.dumps(_truncate(scrub(_plain(entry))), ensure_ascii=False)


def _plain(value: object) -> object:
    """A JSON-ready copy: models as dicts, bytes as their size, anything unknown as str."""
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _truncate(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return f"{value[:MAX_TEXT]}... [{len(value) - MAX_TEXT} more characters]"
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item) for item in value]
    return value


def _type_name(exc: BaseException) -> str:
    kind = type(exc)
    return kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"


def _raised_at(exc: BaseException) -> str | None:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return f"{_short_path(frame.filename)}:{frame.lineno} in {frame.name}"


def _short_path(filename: str) -> str:
    parts = Path(filename).parts
    if "site-packages" in parts:
        return "/".join(parts[parts.index("site-packages") + 1:])
    try:
        return Path(filename).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(filename).name


def _model_id(agent: Any) -> str | None:
    try:
        return (agent.model.get_config() or {}).get("model_id")
    except Exception:
        return None


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)
