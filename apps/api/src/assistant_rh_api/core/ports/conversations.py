"""Conversations boundaries; adapters own I/O and lifecycle state."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from assistant_rh_api.core.models.conversations import ChatRun, Feedback, FeedbackAnalysisData, FeedbackInput, RunSource


class ChatRunStorePort(Protocol):
    async def require_individual_schema(self) -> None: ...
    async def finalize(self, run: ChatRun) -> None: ...
    async def get(self, turn_id: str) -> ChatRun | None: ...
    async def sources(self, turn_id: str, group_slug: str, *, ministries: tuple[str, ...], user_id: UUID | None = None) -> tuple[RunSource, ...]: ...


class FeedbackStorePort(Protocol):
    async def get(self, turn_id: str) -> Feedback | None: ...
    async def get_owned(self, turn_id: str, user_id: UUID, group_slug: str, ministries: tuple[str, ...]) -> Feedback | None: ...
    async def save(
        self,
        value: FeedbackInput,
        group_slug: str,
        session_hash: str,
        now: datetime,
        *,
        user_id: UUID | None = None,
        ministries: tuple[str, ...],
    ) -> Feedback | None: ...
    async def for_analysis(self, max_stars: int, limit: int) -> tuple[FeedbackAnalysisData, ...]: ...
    async def save_analysis(self, feedback_id: int, revision: str, category: str, reason: str, now: datetime) -> bool: ...
