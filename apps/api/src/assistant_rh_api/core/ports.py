"""Narrow async store boundaries; no SQL, provider SDKs or transport types.

Read methods distinguish absence (None) from failure (ApplicationError).
Adapters return immutable snapshots and keep cache/lifecycle state outside core.
Runtime repositories consume domain values and return explicit records.
Provider contracts grow separately with B3.
"""

from datetime import datetime
from typing import Protocol

from assistant_rh_api.core.health import HealthProbe as HealthProbe
from assistant_rh_api.core.models import Acronym, ConfigValues, Prompt, Snapshot
from assistant_rh_api.core.runtime import (
    ChatRun,
    Document,
    Feedback,
    FeedbackAnalysisData,
    FeedbackInput,
    Group,
    LegalReference,
    RawChunk,
    RunSource,
    SearchRequest,
    Section,
    Session,
)


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


class GroupStorePort(Protocol):
    async def list_groups(self) -> tuple[Group, ...]: ...
    async def get(self, slug: str) -> Group | None: ...


class SessionStorePort(Protocol):
    async def create(self, session: Session) -> None: ...
    async def get_active(self, token_hash: str, now: datetime) -> Session | None: ...
    async def revoke(self, token_hash: str, now: datetime) -> None: ...


class SearchPort(Protocol):
    async def search(self, request: SearchRequest) -> tuple[RawChunk, ...]: ...


class ContentStorePort(Protocol):
    async def documents(self, ids: tuple[str, ...]) -> tuple[Document, ...]: ...
    async def sections(self, ids: tuple[str, ...]) -> tuple[Section, ...]: ...
    async def references(self, numbers: tuple[str, ...]) -> tuple[LegalReference, ...]: ...
    async def chunks(self, source: str, ids: tuple[str, ...]) -> tuple[RawChunk, ...]: ...


class ChatRunStorePort(Protocol):
    async def finalize(self, run: ChatRun) -> None: ...
    async def get(self, turn_id: str) -> ChatRun | None: ...
    async def sources(self, turn_id: str, group_slug: str) -> tuple[RunSource, ...]: ...


class FeedbackStorePort(Protocol):
    async def get(self, turn_id: str) -> Feedback | None: ...
    async def save(self, value: FeedbackInput, group_slug: str, session_hash: str, now: datetime) -> Feedback | None: ...
    async def for_analysis(self, max_stars: int, limit: int) -> tuple[FeedbackAnalysisData, ...]: ...
    async def save_analysis(self, feedback_id: int, revision: str, category: str, reason: str, now: datetime) -> bool: ...
