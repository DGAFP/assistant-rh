"""Immutable conversations values, independent of storage and transport."""

from dataclasses import dataclass, field
from datetime import datetime

from assistant_rh_api.core.models.configuration import ConfigValues, JsonValue


@dataclass(frozen=True, slots=True)
class RunSource:
    doc_ref: str
    title: str
    url: str
    document_id: str | None = None


@dataclass(frozen=True, slots=True)
class TraceEvent:
    stage: str
    duration_ms: int = 0
    status: str = "ok"
    attempt_name: str = ""
    input_ref: JsonValue = None
    output_ref: JsonValue = None
    metrics: JsonValue = None
    error_type: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class ChatRun:
    turn_id: str
    trace_id: str
    timestamp: datetime | None
    group_slug: str
    session_hash: str = field(repr=False)
    conversation_id: str
    question: str
    answer: str
    selected_ministry: str
    model: str
    sources: tuple[RunSource, ...] = ()
    events: tuple[TraceEvent, ...] = ()
    diagnostics: JsonValue = None


@dataclass(frozen=True, slots=True)
class FeedbackInput:
    turn_id: str
    stars: int | None
    comment: str = ""
    reasons_positive: tuple[str, ...] = ()
    reasons_negative: tuple[str, ...] = ()
    helpful: bool | None = None

    def __post_init__(self) -> None:
        if self.stars is not None and (type(self.stars) is not int or not 1 <= self.stars <= 5):
            raise ValueError("stars must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class Feedback:
    id: int
    value: FeedbackInput
    timestamp: datetime | None
    revision: str
    analysis_category: str | None
    analysis_reason: str | None
    annotations: ConfigValues


@dataclass(frozen=True, slots=True)
class FeedbackAnalysisData:
    feedback: Feedback
    question: str
    answer: str
    run_context: ConfigValues | None
