"""Pure health-probe boundary shared by the HTTP and database adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Operational state returned by a concrete health probe."""

    db: Literal["ok", "error"]
    config_loaded: bool

    @property
    def is_healthy(self) -> bool:
        return self.db == "ok" and self.config_loaded


class HealthProbe(Protocol):
    """Port used by the transport handler to inspect runtime dependencies."""

    async def check(self) -> HealthReport:
        """Return current database and runtime-config availability."""
        ...
