"""C4 section grouping and ranking, with content and inference behind ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Dict, List, cast

from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable, InferenceFailure
from assistant_rh_api.core.models.context import AggregatedSection, SectionAggregationDiagnostics, SectionAggregationResult
from assistant_rh_api.core.models.inference import Reranking
from assistant_rh_api.core.models.rag_configuration import SectionAggregationConfig
from assistant_rh_api.core.models.retrieval import RetrievedChunk, freeze_metadata
from assistant_rh_api.core.pipeline.steps.context_formatting import serialize_section_chunks
from assistant_rh_api.core.ports.inference import RerankerPort
from assistant_rh_api.core.ports.retrieval import ContentStorePort


class SectionAggregator:
    """Aggregate chunks into sections, score them, and optionally rerank."""

    # Suffixe des lignes d'index ADDITIVES R2 de rag_chunks_dgafp (résumé
    # d'article : l'embedding encode le résumé, chunk_text reste le texte
    # authentique — cf. data-engineering legifrance/summary_rows.py).
    _SUMMARY_CHUNK_SUFFIX = "_r2s"

    def __init__(self, config: SectionAggregationConfig, content_store: ContentStorePort, reranker: RerankerPort):
        self.config = config
        self.content_store = content_store
        self.reranker = reranker

    @classmethod
    def _standalone_group_key(cls, chunk: RetrievedChunk) -> str:
        """Grouping key for chunks without a ``rag_sections`` parent.

        La ligne-résumé R2 d'un article (``{cid}_r2s``) porte le même
        chunk_text authentique que son chunk article (``{cid}_0``) : quand les
        deux sont retrouvés, ils doivent FUSIONNER en une section (double hit =
        signal ``chunk_count`` légitime) au lieu de consommer deux des
        ``_MAX_RERANK_INPUT`` places du reranker avec un texte identique.
        La fusion est bornée à cette paire précise (``_0``/``_r2s``), scopée
        par ``table_source`` — les chunks positionnels d'un article multi-chunk
        (``_1``, ``_2``…) gardent leur clé propre, comportement inchangé.
        """
        cid = str((chunk.metadata or {}).get("cid") or "").strip()
        chunk_id = str(chunk.chunk_id or "")
        if cid and chunk_id in (f"{cid}_0", f"{cid}{cls._SUMMARY_CHUNK_SUFFIX}"):
            return f"_standalone_cid_{chunk.table_source}:{cid}"
        return f"_standalone_{chunk_id}"

    async def aggregate(self, chunks: Sequence[RetrievedChunk], query: str | None = None) -> tuple[AggregatedSection, ...]:
        """Return aggregated sections.

        Use :meth:`aggregate_with_diagnostics` when the caller also needs
        request-scoped reranker diagnostics.
        """
        return (await self.aggregate_with_diagnostics(chunks, query=query)).sections

    async def aggregate_with_diagnostics(
        self,
        chunks: Sequence[RetrievedChunk],
        query: str | None = None,
    ) -> SectionAggregationResult:
        chunks = tuple(chunks)
        if not chunks:
            return SectionAggregationResult(
                sections=(),
                diagnostics=SectionAggregationDiagnostics(reranker_status="skipped_no_chunks"),
            )

        section_ids = [str(c.section_id) for c in chunks if c.section_id]
        store_errors: list[str] = []
        section_meta = await self._fetch_sections(section_ids, store_errors) if section_ids else {}

        groups: Dict[str, list[RetrievedChunk]] = {}
        for c in chunks:
            key = str(c.section_id) if c.section_id else self._standalone_group_key(c)
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

            first_meta = first.metadata or {}

            doc_id = meta.get("doc_id") or first_meta.get("source_document_id")

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
                    heading=cast(str, meta.get("heading") or first_meta.get("doc_title") or first_meta.get("source_name", "")),
                    markdown=meta.get("section_markdown", first.text),
                    chunks=tuple(group),
                    score=agg_score,
                    document_id=str(doc_id) if doc_id and not is_standalone else None,
                    publisher=meta.get("doc_publisher") or first.table_source,
                    references_juridiques=meta.get("references_juridiques"),
                    heading_path=meta.get("heading_path"),
                    metadata=sec_metadata,
                )
            )

        sections.sort(key=lambda s: s.score, reverse=True)

        sections_before_rerank = len(sections)
        chunks_before_rerank = serialize_section_chunks(sections)
        reranker_status = "not_run"
        reranker_error = ""
        outcome = Reranking((), ())
        reranked = False
        if not self.config.enable_section_reranker:
            reranker_status = "disabled"
        elif not query:
            reranker_status = "skipped_no_query"
        else:
            sections, reranker_status, reranker_error, outcome = await self._rerank(query, sections)
            reranked = reranker_status == "completed"

        return SectionAggregationResult(
            sections=tuple(sections),
            diagnostics=SectionAggregationDiagnostics(
                sections_before_rerank=sections_before_rerank,
                sections_after_rerank=len(sections),
                reranker_status=reranker_status,
                reranker_error=reranker_error,
                chunks_before_rerank=tuple(freeze_metadata(row) for row in chunks_before_rerank),
                chunks_after_rerank=tuple(freeze_metadata(row) for row in serialize_section_chunks(sections, include_rerank_score=reranked)),
                store_errors=tuple(store_errors),
                reranker_attempts=outcome.attempts,
                reranker_fallback=outcome.fallback,
            ),
        )

    async def _fetch_sections(self, ids: list[str], errors: list[str]) -> dict[str, dict]:
        try:
            rows = await self.content_store.sections(tuple(dict.fromkeys(ids)))
        except (DatabaseUnavailable, DatabaseFailure, DatabaseConflict) as exc:
            errors.append(exc.code)
            return {}
        metadata = {}
        for row in rows:
            doc = row.document
            metadata[row.section_id] = {
                "heading": row.heading,
                "section_markdown": row.markdown,
                "heading_path": row.heading_path,
                "references_juridiques": row.legal_references,
                "doc_id": row.doc_id,
                "doc_short_id": doc.short_id if doc else None,
                "doc_title": doc.title if doc else None,
                "doc_url": doc.url if doc else None,
                "doc_token_count": doc.token_count if doc else None,
                "doc_publisher": doc.publisher if doc else None,
                "doc_date": doc.updated_date if doc else None,
            }
        return metadata

    async def _rerank(self, query: str, sections: List[AggregatedSection]) -> tuple[List[AggregatedSection], str, str, Reranking]:
        input_k = max(self.config.rerank_input_k, self.config.section_rerank_top_k)
        candidates = sections[:input_k]
        texts = tuple(f"# {s.heading}\n\n{s.markdown[:1500]}" for s in candidates)
        try:
            outcome = await self.reranker.rerank(query, texts, top_k=self.config.section_rerank_top_k)
        except InferenceFailure as exc:
            return sections[: self.config.section_rerank_top_k], "failed", exc.code, Reranking((), exc.attempts)
        # The port provides ranking order, including input-index tie breaking.
        return [replace(candidates[row.index], score=row.score) for row in outcome.documents], "completed", "", outcome
