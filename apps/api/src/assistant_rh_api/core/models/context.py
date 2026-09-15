"""Immutable values exchanged by aggregation, selection and context building."""

from dataclasses import dataclass, field

from assistant_rh_api.core.models.configuration import ConfigValues, JsonValue
from assistant_rh_api.core.models.inference import Attempt
from assistant_rh_api.core.models.retrieval import RetrievedChunk, freeze_metadata


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def freeze_value(value: JsonValue) -> JsonValue:
    return freeze_metadata({"value": value})["value"]


@dataclass(frozen=True, slots=True)
class AggregatedSection:
    section_id: str | None
    heading: str
    markdown: str
    chunks: tuple[RetrievedChunk, ...]
    score: float
    document_id: str | None = None
    publisher: str | None = None
    references_juridiques: JsonValue = None
    heading_path: str | None = None
    metadata: ConfigValues = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunks", tuple(self.chunks))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
        object.__setattr__(self, "references_juridiques", freeze_value(self.references_juridiques))

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.markdown)


@dataclass(frozen=True, slots=True)
class ContextItem:
    section_id: str | None
    heading: str | None
    content: str
    score: float
    publisher: str | None = None
    document_title: str | None = None
    document_url: str | None = None
    references_juridiques: JsonValue = None
    token_estimate: int = 0
    metadata: ConfigValues = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
        object.__setattr__(self, "references_juridiques", freeze_value(self.references_juridiques))


@dataclass(frozen=True, slots=True)
class SectionAggregationDiagnostics:
    sections_before_rerank: int = 0
    sections_after_rerank: int = 0
    reranker_status: str = "not_run"
    reranker_error: str = ""
    chunks_before_rerank: tuple[ConfigValues, ...] = ()
    chunks_after_rerank: tuple[ConfigValues, ...] = ()
    store_errors: tuple[str, ...] = ()
    reranker_attempts: tuple[Attempt, ...] = ()
    reranker_fallback: bool = False


@dataclass(frozen=True, slots=True)
class SectionAggregationResult:
    sections: tuple[AggregatedSection, ...]
    diagnostics: SectionAggregationDiagnostics


@dataclass(frozen=True, slots=True)
class ContextBuildDiagnostics:
    tokens_used: int = 0
    token_budget: int = 0
    full_doc_count: int = 0
    triangulation_count: int = 0
    reference_tokens: int = 0
    store_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    items: tuple[ContextItem, ...]
    resolved_refs: ConfigValues
    diagnostics: ContextBuildDiagnostics

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved_refs", freeze_metadata(self.resolved_refs))
