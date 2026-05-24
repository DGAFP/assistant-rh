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
