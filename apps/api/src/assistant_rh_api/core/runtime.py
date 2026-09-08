"""Immutable repository contracts for the runtime; no physical schema names."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from assistant_rh_api.core.models import ConfigValues, JsonValue

type Source = Literal["matte", "mso", "mi", "masa", "service_public", "dgafp", "rgrh"]


@dataclass(frozen=True, slots=True)
class Group:
    slug: str
    label: str
    priority: int
    visible: bool
    is_admin: bool
    password_hash: str | None = field(repr=False)
    allowed_ministries: tuple[str, ...]
    default_ministry: str

    @property
    def roles(self) -> tuple[str, ...]:
        return ("admin",) if self.is_admin else ("user",)


@dataclass(frozen=True, slots=True)
class Session:
    token_hash: str = field(repr=False)
    group_slug: str
    created_at: datetime
    expires_at: datetime
    credential_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SearchRequest:
    source: Source
    mode: Literal["vector", "lexical", "heading"]
    query: str = ""
    embedding: tuple[float, ...] = ()
    embedding_model: Literal["albert", "bge_scaleway"] = "albert"
    limit: int = 20
    probes: int = 10


@dataclass(frozen=True, slots=True)
class RawChunk:
    source: Source
    chunk_id: str
    text: str
    section_id: str | None
    score: float
    rank: int
    metadata: ConfigValues


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    short_id: str | None
    title: str
    url: str
    publisher: str
    markdown: str
    token_count: int
    updated_date: str | None = None


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    doc_id: str
    heading: str
    heading_path: str
    markdown: str
    legal_references: JsonValue
    document: Document | None


@dataclass(frozen=True, slots=True)
class LegalReference:
    number: str
    cid: str
    url: str
    title: str


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
