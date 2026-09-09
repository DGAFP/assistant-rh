"""System boundaries; adapters own I/O and lifecycle state."""

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime:
        """Return a timezone-aware UTC timestamp."""
        ...

    def monotonic(self) -> float:
        """Return elapsed seconds from an arbitrary, non-decreasing origin."""
        ...


class IdGeneratorPort(Protocol):
    def new_id(self) -> str:
        """Return a full, unique identifier for a request or event."""
        ...
