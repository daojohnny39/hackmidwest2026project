"""Current search stage, scoped to one investigation and inherited by its async tasks."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

SearchStage = Literal["preparing", "searching", "comparing", "reviewing", "checking", "finalizing"]
_reporter: ContextVar[Callable[[SearchStage], None] | None] = ContextVar("search_progress", default=None)


def report(stage: SearchStage) -> None:
    """Publish a public stage code; CLI runs without a reporter are unaffected."""
    reporter = _reporter.get()
    if reporter is not None:
        reporter(stage)


@contextmanager
def reporting(reporter: Callable[[SearchStage], None]) -> Iterator[None]:
    token = _reporter.set(reporter)
    try:
        yield
    finally:
        _reporter.reset(token)
