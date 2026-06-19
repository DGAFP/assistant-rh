"""
Section aggregator for the RAG V3 Clean pipeline.

Groups retrieved chunks by their ``rag_sections`` parent, computes a
weighted aggregate score, and optionally reranks sections via the Albert
reranker.  Chunks without a section link are treated as standalone items.

Scoring formula:
  score = w_max * max(chunk_scores) + w_mean * mean(chunk_scores) + w_count * norm_count

Dependencies (internal only):
  - config (SectionAggregationConfig, get_dsn)
  - models (RetrievedChunk, AggregatedSection)
  - reranker (AlbertReranker)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List

import psycopg
from psycopg.rows import dict_row

from .config import SectionAggregationConfig
from .db_helpers import get_dsn
from .models import AggregatedSection, RetrievedChunk
from .reranker import AlbertReranker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SectionAggregationDiagnostics:
    """Request-scoped diagnostics produced while aggregating sections."""

    sections_before_rerank: int = 0
    sections_after_rerank: int = 0
    reranker_status: str = "not_run"
    reranker_error: str = ""


@dataclass
class SectionAggregationResult:
    """Sections plus request-scoped aggregation diagnostics."""

    sections: List[AggregatedSection]
    diagnostics: SectionAggregationDiagnostics


class SectionAggregator:
    """Aggregate chunks into sections, score them, and optionally rerank."""

    def __init__(self, config: SectionAggregationConfig, dsn: str | None = None):
        self.config = config
        self.dsn = dsn or get_dsn()
        self._reranker: AlbertReranker | None = None

    def aggregate(self, chunks: List[RetrievedChunk], query: str | None = None) -> List[AggregatedSection]:
        """Return aggregated sections.

        Use :meth:`aggregate_with_diagnostics` when the caller also needs
        request-scoped reranker diagnostics.
        """
        return self.aggregate_with_diagnostics(chunks, query=query).sections

    def aggregate_with_diagnostics(
        self,
        chunks: List[RetrievedChunk],
        query: str | None = None,
    ) -> SectionAggregationResult:
        if not chunks:
            return SectionAggregationResult(
                sections=[],
                diagnostics=SectionAggregationDiagnostics(reranker_status="skipped_no_chunks"),
            )

        section_ids = [str(c.section_id) for c in chunks if c.section_id]
        section_meta = self._fetch_sections(section_ids) if section_ids else {}

        groups: Dict[str, List[RetrievedChunk]] = {}
        for c in chunks:
            key = str(c.section_id) if c.section_id else f"_standalone_{c.chunk_id}"
            groups.setdefault(key, []).append(c)

        max_count = max(len(g) for g in groups.values())

        sections: List[AggregatedSection] = []
        for key, group in groups.items():
            scores = [c.score for c in group]
            max_s = max(scores)
            mean_s = sum(scores) / len(scores)
            norm_count = len(group) / max_count

            agg_score = self.config.weight_max_score * max_s + self.config.weight_mean_score * mean_s + self.config.weight_chunk_count * norm_count

            meta = section_meta.get(key, {})
            first = group[0]
            is_standalone = key.startswith("_standalone_")

            doc_id = meta.get("doc_id") or first.metadata.get("source_document_id")
            first_meta = first.metadata

            doc_short_id = (
                meta.get("doc_short_id") or first_meta.get("doc_short_id") or first_meta.get("short_id") or first_meta.get("source_document_id") or ""
            )
            doc_title = meta.get("doc_title") or first_meta.get("doc_title") or first_meta.get("source_name", "")
            doc_url = meta.get("doc_url") or first_meta.get("doc_url") or first_meta.get("url")

            sec_metadata = {
                "doc_id": str(doc_id) if doc_id else "",
                "doc_short_id": str(doc_short_id) if doc_short_id else "",
                "doc_title": doc_title,
                "doc_url": doc_url,
                "doc_publisher": meta.get("doc_publisher", first.table_source),
                "doc_date": str(meta["doc_date"]) if meta.get("doc_date") else "",
                "doc_token_count": meta.get("doc_token_count", 0),
                "chunk_count": len(group),
                "max_chunk_score": max_s,
                "mean_chunk_score": mean_s,
            }

            # For standalone chunks (no rag_sections row), carry forward
            # chunk-level metadata needed for pill display (DGAFP, legifrance)
            if is_standalone:
                for k in ("number", "full_title", "title", "category", "cid"):
                    v = first_meta.get(k)
                    if v:
                        sec_metadata[k] = v

            sections.append(
                AggregatedSection(
                    section_id=None if is_standalone else key,
                    heading=meta.get("heading") or first_meta.get("doc_title") or first_meta.get("source_name", ""),
                    markdown=meta.get("section_markdown", first.text),
                    chunks=group,
                    score=agg_score,
                    document_id=str(doc_id) if doc_id else None,
                    publisher=meta.get("doc_publisher") or first.table_source,
                    references_juridiques=meta.get("references_juridiques"),
                    heading_path=meta.get("heading_path"),
                    metadata=sec_metadata,
                )
            )

        sections.sort(key=lambda s: s.score, reverse=True)

        sections_before_rerank = len(sections)
        reranker_status = "not_run"
        reranker_error = ""
        if not self.config.enable_section_reranker:
            reranker_status = "disabled"
        elif not query:
            reranker_status = "skipped_no_query"
        else:
            sections, reranker_status, reranker_error = self._rerank(query, sections)

        return SectionAggregationResult(
            sections=sections,
            diagnostics=SectionAggregationDiagnostics(
                sections_before_rerank=sections_before_rerank,
                sections_after_rerank=len(sections),
                reranker_status=reranker_status,
                reranker_error=reranker_error,
            ),
        )

    # ------------------------------------------------------------------
    # DB lookup for rag_sections + rag_documents
    # ------------------------------------------------------------------

    def _fetch_sections(self, section_ids: List[str]) -> Dict[str, dict]:
        if not section_ids:
            return {}
        unique = list(set(section_ids))
        sql = """
            SELECT
                s.section_id    AS section_id,
                s.heading,
                s.section_markdown,
                s.heading_path,
                s.references_juridiques,
                s.doc_id,
                d.short_id      AS doc_short_id,
                d.title         AS doc_title,
                d.source_url    AS doc_url,
                d.token_count   AS doc_token_count,
                d.publisher     AS doc_publisher,
                d.last_updated_date AS doc_date
            FROM rag_sections s
            LEFT JOIN rag_documents d ON d.doc_id = s.doc_id
            WHERE s.section_id = ANY(%s::uuid[])
        """
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                rows = conn.execute(sql, (unique,)).fetchall()
            return {str(r["section_id"]): dict(r) for r in rows}
        except psycopg.Error as exc:
            logger.warning("Section metadata lookup failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Reranking
    # ------------------------------------------------------------------

    # Max sections to send to the reranker API (avoids 413 payload errors)
    _MAX_RERANK_INPUT = 20

    def _rerank(self, query: str, sections: List[AggregatedSection]) -> tuple[List[AggregatedSection], str, str]:
        if not sections:
            return sections, "skipped_no_sections", ""
        try:
            if self._reranker is None:
                self._reranker = AlbertReranker()

            # Pre-filter to avoid oversized payloads to the reranker API
            candidates = sections[: self._MAX_RERANK_INPUT]

            texts = [f"# {s.heading}\n\n{s.markdown[:1500]}" for s in candidates]
            t0 = time.time()
            ranked = self._reranker.rerank(query, texts, top_k=self.config.section_rerank_top_k)
            logger.info("Section reranking done in %.0fms (%d candidates → %d selected)", (time.time() - t0) * 1000, len(candidates), len(ranked))

            out: List[AggregatedSection] = []
            for idx, score in ranked:
                sec = candidates[idx]
                sec.score = score
                out.append(sec)
            return out, "completed", ""

        except Exception as exc:
            logger.error("Section reranking failed, keeping aggregated order: %s", exc)
            return sections[: self.config.section_rerank_top_k], "failed", str(exc)
