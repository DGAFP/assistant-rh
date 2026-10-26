"""Request-scoped diagnostic port; never carries user content or credentials."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Observation:
    kind: Literal["stage", "provider", "database"]
    name: str
    outcome: str
    seconds: float = 0
    provider: str = "none"


_observer: ContextVar[Callable[[Observation], None] | None] = ContextVar("api_observer", default=None)


@contextmanager
def observing(observer: Callable[[Observation], None]) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def observe(event: Observation) -> None:
    observer = _observer.get()
    if observer is not None:
        try:
            observer(event)
        except Exception:
            # Diagnostics cannot replace a provider/SQL error or break a stream.
            pass
