"""
Context builder for the RAG V3 Clean pipeline.

Selects sections to include in the LLM prompt under a fixed **token budget**,
applying:
  - Document-entire inclusion for small documents (< threshold)
  - Source triangulation (always include top sections from secondary publishers)
  - Legal-reference injection from ``rag_sections.references_juridiques``

The output is a list of ``ContextItem`` ready for prompt formatting.

Dependencies (internal only):
  - config (ContextBuildConfig, get_dsn)
  - models (AggregatedSection, ContextItem)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set

import psycopg
from psycopg.rows import dict_row

from .config import ContextBuildConfig
from .db_helpers import get_dsn
from .models import AggregatedSection, ContextItem, estimate_tokens

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    Build the final context for the LLM generator from ranked sections.

    Strategy:
      1. If the top document is small (< ``doc_entire_threshold`` tokens),
         include **all its sections** up to ``max_full_docs``.
      2. Fill remaining budget with the highest-scored individual sections.
      3. **Triangulation**: always inject at least ``triangulation_sections``
         sections from publishers *other* than the top one.
      4. Append legal references cited inside the selected sections.
    """

    def __init__(self, config: ContextBuildConfig, dsn: str | None = None):
        self.config = config
        self.dsn = dsn or get_dsn()
        self.last_resolved_refs: Dict[str, Dict[str, str]] = {}

    def build(self, sections: List[AggregatedSection]) -> List[ContextItem]:
        if not sections:
            return []

        budget = self.config.get_token_budget()
        max_full_docs = self.config.get_max_full_docs()
        doc_threshold = self.config.get_doc_entire_threshold()
        max_sections = self.config.get_max_sections()
        refs_budget = self.config.get_legal_refs_budget()

        selected: List[ContextItem] = []
        used_ids: Set[str] = set()
        tokens_used = 0
        full_doc_count = 0

        # Group by document (only sections that have a document_id). Sections
        # without a document_id are picked up by the Step-2 loop directly.
        by_doc: Dict[str, List[AggregatedSection]] = defaultdict(list)
        for s in sections:
            if s.document_id:
                by_doc[str(s.document_id)].append(s)

        # Sort doc groups by best section score (descending); compute the max
        # once and reuse it (used both for sort and for the inner gate).
        doc_max_score: Dict[str, float] = {doc_id: max((s.score or 0.0) for s in secs) for doc_id, secs in by_doc.items()}
        sorted_docs = sorted(by_doc.items(), key=lambda kv: doc_max_score[kv[0]], reverse=True)

        def _section_key(s: AggregatedSection) -> str:
            # Unique-and-stable across both loops. `id(s)` is fine because the
            # input list is stable for the lifetime of this build().
            return s.section_id or s.heading or f"_obj_{id(s)}"

        # Step 1 – doc-entire for small documents. We always include qualifying
        # small docs (within budget and the max_full_docs cap); the original
        # legifrance-q2 ordering bug is fixed by the score-sort at the end of
        # Step 2 below, not by suppressing doc-entires here.
        for doc_id, doc_sections in sorted_docs:
            if full_doc_count >= max_full_docs:
                break
            doc_token_count = doc_sections[0].metadata.get("doc_token_count", 0) or 0
            if doc_token_count <= 0 or doc_token_count > doc_threshold:
                continue
            if tokens_used + doc_token_count > budget:
                continue

            doc_row = self._load_full_document(doc_id)
            if not doc_row or not doc_row.get("doc_markdown"):
                continue

            item = self._full_doc_to_item(doc_row, doc_sections)
            item.metadata["is_doc_entire"] = True
            # ContextItem.score on a doc-entire reflects the best section score
            # of its constituents, so the post-Step-2 sort places it correctly.
            item.score = doc_max_score[doc_id]
            selected.append(item)
            for s in doc_sections:
                used_ids.add(_section_key(s))
            tokens_used += item.token_estimate
            full_doc_count += 1
            logger.info("Doc-entire included doc %s (%s): ~%d tokens", doc_id, item.document_title[:40], item.token_estimate)

        # Step 2 – fill with top individual sections (from selector/reranker order)
        for s in sections:
            key = _section_key(s)
            if key in used_ids:
                continue
            item = self._section_to_item(s)
            if tokens_used + item.token_estimate > budget:
                continue
            if len(selected) >= max_sections:
                break
            selected.append(item)
            used_ids.add(key)
            tokens_used += item.token_estimate

        # The sort key puts highest-scoring items first; on exact score ties,
        # standalone chunks beat doc-entires (promotion intent from review
        # finding #7) and Python's stable sort preserves retrieval order for
        # the remaining ties.
        def _sort_key(item):
            return (-(item.score or 0.0), 1 if item.metadata.get("is_doc_entire") else 0)

        # Determine the "primary publisher" for triangulation BEFORE appending —
        # use the highest-scored section in the input rather than `selected[0]`
        # so triangulation depends on the data, not on insertion ordering.
        primary_publisher = None
        if sections:
            top_section = max(sections, key=lambda s: s.score or 0.0)
            primary_publisher = top_section.publisher

        # Step 3 – triangulation (ignores budget to guarantee publisher diversity)
        tri_added = 0
        for s in sections:
            if tri_added >= self.config.triangulation_sections:
                break
            if s.publisher == primary_publisher:
                continue
            key = _section_key(s)
            if key in used_ids:
                continue
            item = self._section_to_item(s)
            item.metadata["is_triangulation"] = True
            selected.append(item)
            used_ids.add(key)
            tokens_used += item.token_estimate
            tri_added += 1

        # Single sort at the very end so triangulation items participate in the
        # final ordering — the LLM always sees the strongest evidence first,
        # regardless of which step contributed each item.
        selected.sort(key=_sort_key)

        # Step 4 – resolve legal references from rag_chunks_dgafp and inject
        refs_tokens = 0
        all_ref_numbers = self._collect_ref_numbers(selected)
        cid_map = self._resolve_cids(all_ref_numbers) if all_ref_numbers else {}
        self.last_resolved_refs = cid_map

        # Allocate the legal-refs budget primary-content-first: triangulation
        # items are publisher-diversity fillers and must not consume refs_budget
        # ahead of the answer-bearing primary sources, even when they outscore
        # them. Stable sort preserves the score order within each group, so the
        # prompt order (``selected``) is untouched — only the refs pass reorders.
        refs_order = sorted(selected, key=lambda it: bool(it.metadata.get("is_triangulation")))
        for item in refs_order:
            if item.references_juridiques and refs_tokens < refs_budget:
                self._enrich_refs_with_cid(item, cid_map)
                ref_text = self._format_references(item.references_juridiques)
                ref_tokens = estimate_tokens(ref_text)
                if refs_tokens + ref_tokens <= refs_budget:
                    item.content += f"\n\n---\nReferences juridiques :\n{ref_text}"
                    item.token_estimate += ref_tokens
                    tokens_used += ref_tokens
                    refs_tokens += ref_tokens

        logger.info(
            "Context built: %d items, ~%d tokens (budget %d, mode %s), %d full docs, %d legal refs",
            len(selected),
            tokens_used,
            budget,
            self.config.context_mode.value,
            full_doc_count,
            len(cid_map),
        )
        return selected

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def format_for_prompt(items: List[ContextItem]) -> str:
        """Render context items as a string ready for the LLM user-message."""
        parts: List[str] = []
        for i, item in enumerate(items, 1):
            header = f"[Source {i}] {item.document_title or item.heading}"
            if item.publisher:
                header += f" ({item.publisher})"
            parts.append(f"### {header}\n\n```markdown\n{item.content}\n```\n\n---\n")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_full_document(self, doc_id: str) -> Optional[dict]:
        """Load the full document markdown and metadata from rag_documents."""
        sql = """
            SELECT doc_id, title, source_url, publisher, doc_markdown, token_count
            FROM rag_documents
            WHERE doc_id = %s AND doc_markdown IS NOT NULL
        """
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                return conn.execute(sql, (doc_id,)).fetchone()
        except psycopg.Error as exc:
            logger.warning("Failed to load full document %s: %s", doc_id, exc)
            return None

    def _full_doc_to_item(self, doc_row: dict, matched_sections: List[AggregatedSection]) -> ContextItem:
        """Convert a rag_documents row into a single ContextItem with the full document content."""
        best_score = max((s.score for s in matched_sections), default=0.0)
        all_refs = []
        for s in matched_sections:
            if s.references_juridiques:
                all_refs.append(s.references_juridiques)
        return ContextItem(
            section_id=None,
            heading=doc_row.get("title", ""),
            content=doc_row.get("doc_markdown", ""),
            score=best_score,
            publisher=doc_row.get("publisher", ""),
            document_title=doc_row.get("title", ""),
            document_url=doc_row.get("source_url"),
            references_juridiques=all_refs[0] if all_refs else None,
            token_estimate=doc_row.get("token_count") or estimate_tokens(doc_row.get("doc_markdown", "")),
            metadata={
                "doc_id": str(doc_row.get("doc_id", "")),
                "doc_title": doc_row.get("title", ""),
                "doc_url": doc_row.get("source_url"),
                "doc_publisher": doc_row.get("publisher", ""),
                "doc_token_count": doc_row.get("token_count", 0),
            },
        )

    def _section_to_item(self, s: AggregatedSection) -> ContextItem:
        return ContextItem(
            section_id=s.section_id,
            heading=s.heading,
            content=s.markdown,
            score=s.score,
            publisher=s.publisher,
            document_title=s.metadata.get("doc_title", ""),
            document_url=s.metadata.get("doc_url"),
            references_juridiques=s.references_juridiques,
            token_estimate=s.token_estimate,
            metadata=s.metadata,
        )

    @staticmethod
    def _collect_ref_numbers(items: List[ContextItem]) -> List[str]:
        """Extract all article numbers from references_juridiques across items."""
        numbers = []
        for item in items:
            refs = item.references_juridiques
            if not refs:
                continue
            if isinstance(refs, str):
                try:
                    refs = json.loads(refs)
                except (ValueError, json.JSONDecodeError):
                    continue
            if isinstance(refs, list):
                for r in refs:
                    if isinstance(r, dict) and r.get("number"):
                        numbers.append(r["number"])
        return list(set(numbers))

    def _resolve_cids(self, numbers: List[str]) -> Dict[str, Dict[str, str]]:
        """Lookup article numbers in rag_chunks_dgafp to get cid and url."""
        if not numbers:
            return {}
        sql = """
            SELECT number, cid, url, full_title
            FROM rag_chunks_dgafp
            WHERE number = ANY(%s)
            GROUP BY number, cid, url, full_title
        """
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                rows = conn.execute(sql, (numbers,)).fetchall()
            return {r["number"]: {"cid": r["cid"] or "", "url": r["url"] or "", "title": r["full_title"] or ""} for r in rows if r.get("cid")}
        except psycopg.Error as exc:
            logger.warning("CID resolution failed: %s", exc)
            return {}

    @staticmethod
    def _enrich_refs_with_cid(item: ContextItem, cid_map: Dict[str, Dict[str, str]]):
        """Add cid/url to references_juridiques entries if found in cid_map."""
        refs = item.references_juridiques
        if not refs:
            return
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except (ValueError, json.JSONDecodeError):
                return
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, dict) and r.get("number") in cid_map:
                    r.update(cid_map[r["number"]])
            item.references_juridiques = refs

    @staticmethod
    def _format_references(refs) -> str:
        if isinstance(refs, str):
            return refs
        if isinstance(refs, dict):
            lines = []
            for key, val in refs.items():
                lines.append(f"- {key}: {val}")
            return "\n".join(lines)
        if isinstance(refs, list):
            return "\n".join(f"- {r}" for r in refs)
        return str(refs)
