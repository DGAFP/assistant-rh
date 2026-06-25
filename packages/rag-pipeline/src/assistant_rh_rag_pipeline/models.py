"""
Data models for the RAG V3 Clean pipeline.

Defines the core data structures passed between pipeline stages:
  Query → RetrievedChunk → AggregatedSection → ContextItem → PipelineResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def estimate_tokens(text: str) -> int:
    """Rough token count for French text (~4 chars/token on average)."""
    return len(text) // 4


@dataclass
class RetrievedChunk:
    """A text chunk returned by the retriever from one of the DE tables."""

    chunk_id: str
    text: str
    score: float
    table_source: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    section_id: Optional[str] = None
    embedding_model_used: Optional[str] = None


@dataclass
class AggregatedSection:
    """Chunks grouped under a common *rag_sections* row (or standalone)."""

    section_id: Optional[str]
    heading: str
    markdown: str
    chunks: List[RetrievedChunk]
    score: float
    document_id: Optional[str] = None
    publisher: Optional[str] = None
    references_juridiques: Optional[str] = None
    heading_path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.markdown)


@dataclass
class ContextItem:
    """A section (or standalone chunk) selected for inclusion in the LLM prompt."""

    section_id: Optional[str]
    heading: str
    content: str
    score: float
    publisher: Optional[str] = None
    document_title: Optional[str] = None
    document_url: Optional[str] = None
    references_juridiques: Optional[str] = None
    token_estimate: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Full output of a RAG pipeline run."""

    query: str
    answer: str
    context_items: List[ContextItem]
    sources: List[Dict[str, Any]]
    timing: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Chunk-level logging serialization
# ---------------------------------------------------------------------------

# Preview length for ``chunk_markdown`` persisted to chat_runs logging columns.
# Kept at 300 chars to match the historical chunk-trace format and to bound
# JSONB row growth.
CHUNK_LOG_MARKDOWN_PREVIEW = 300

CHUNK_LOG_KEYS = (
    "doc_id",
    "chunk_id",
    "doc_title",
    "section_id",
    "final_score",
    "rerank_score",
    "doc_publisher",
    "chunk_markdown",
    "section_heading",
)


def _first_metadata_value(*sources: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    for source in sources:
        if not source:
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return ""


def _round_score(score: Any) -> float | None:
    if score is None:
        return None
    return round(float(score), 6)


def _chunk_log_dict(
    chunk: "RetrievedChunk",
    *,
    section: "Optional[AggregatedSection]" = None,
    rerank_score: Optional[float] = None,
) -> Dict[str, Any]:
    """Serialize one chunk to the 9-key shape used by chat_runs trace columns."""
    meta = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    section_meta = section.metadata if section is not None and isinstance(section.metadata, dict) else {}

    if section is not None:
        doc_id = section.document_id or _first_metadata_value(
            section_meta,
            meta,
            keys=("doc_id", "doc_short_id", "document_id", "source_document_id", "short_id", "cid"),
        )
        doc_title = _first_metadata_value(
            section_meta,
            meta,
            keys=("doc_title", "source_name", "full_title", "title", "number"),
        )
        doc_publisher = section.publisher or _first_metadata_value(section_meta, meta, keys=("doc_publisher", "publisher", "source"))
        section_heading = (
            section.heading
            or section.heading_path
            or _first_metadata_value(
                section_meta,
                meta,
                keys=(
                    "heading",
                    "section_heading",
                    "matched_heading",
                    "matched_heading_path",
                    "heading_path",
                    "section_path",
                    "doc_title",
                    "full_title",
                    "title",
                    "number",
                ),
            )
        )
    else:
        doc_id = _first_metadata_value(meta, keys=("doc_id", "doc_short_id", "document_id", "source_document_id", "short_id", "cid"))
        doc_title = _first_metadata_value(meta, keys=("doc_title", "source_name", "full_title", "title", "number"))
        doc_publisher = _first_metadata_value(meta, keys=("doc_publisher", "publisher", "source")) or chunk.table_source
        section_heading = _first_metadata_value(
            meta,
            keys=(
                "heading",
                "section_heading",
                "matched_heading",
                "matched_heading_path",
                "heading_path",
                "section_path",
                "doc_title",
                "source_name",
                "full_title",
                "title",
                "number",
            ),
        )

    return {
        "doc_id": str(doc_id) if doc_id else "",
        "chunk_id": str(chunk.chunk_id),
        "doc_title": str(doc_title) if doc_title else "",
        "section_id": str(chunk.section_id) if chunk.section_id else "",
        "final_score": _round_score(chunk.score),
        "rerank_score": _round_score(rerank_score),
        "doc_publisher": str(doc_publisher or chunk.table_source or ""),
        "chunk_markdown": (chunk.text or "")[:CHUNK_LOG_MARKDOWN_PREVIEW],
        "section_heading": str(section_heading) if section_heading else "",
    }


def serialize_raw_chunks(chunks: List["RetrievedChunk"]) -> List[Dict[str, Any]]:
    """Flatten raw retrieved chunks for the ``v3_chunks_raw`` trace."""
    return [_chunk_log_dict(c) for c in chunks]


def serialize_section_chunks(
    sections: List["AggregatedSection"],
    *,
    include_rerank_score: bool = False,
) -> List[Dict[str, Any]]:
    """Flatten chunks grouped under aggregated sections, preserving section order."""
    out: List[Dict[str, Any]] = []
    for section in sections:
        rerank_score = section.score if include_rerank_score else None
        for chunk in section.chunks:
            out.append(_chunk_log_dict(chunk, section=section, rerank_score=rerank_score))
    return out


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """Legacy chunk model used by src/ui/ components for display and logging."""

    id: str
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def preview(self, n: int = 320) -> str:
        t = " ".join(self.text.split())
        return (t[: n - 1] + "\u2026") if len(t) > n else t
