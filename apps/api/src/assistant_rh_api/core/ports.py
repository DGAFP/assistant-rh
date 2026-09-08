"""Narrow async store boundaries; no SQL, provider SDKs or transport types.

Read methods distinguish absence (None) from failure (ApplicationError).
Adapters return immutable snapshots and keep cache/lifecycle state outside core.
Business-specific search, auth, persistence and provider contracts grow with
their B2/B3 slices, once their input and output types are needed.
"""

from datetime import datetime
from typing import Protocol

from assistant_rh_api.core.health import HealthProbe as HealthProbe
from assistant_rh_api.core.models import Acronym, ConfigValues, Prompt, Snapshot


class ConfigStorePort(Protocol):
    async def load(self) -> Snapshot[ConfigValues] | None: ...


class PromptStorePort(Protocol):
    async def get(self, name: str) -> Snapshot[Prompt] | None: ...


class AcronymStorePort(Protocol):
    async def load(self) -> Snapshot[tuple[Acronym, ...]]: ...


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
