"""
RAG V3 Clean pipeline orchestrator.

Wires together all pipeline stages in a single ``run`` / ``run_stream`` call:

  Query
    → QueryProcessor    (intent + acronyms + reformulation)
    → Retriever          (parallel search on 4 DE tables)
    → SectionAggregator  (chunk → section + rerank)
    → ContextSelector    (optional LLM filter)
    → ContextBuilder     (token budget + triangulation + legal refs)
    → Generator          (streaming LLM answer)
  → PipelineResult

Dependencies: all internal to ``rag_v3_clean``.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable, Dict, Generator, List, Optional

from .config import RAGConfig
from .context_builder import ContextBuilder
from .context_selector import ContextSelector
from .db_helpers import get_dsn
from .generator import StreamingGenerator
from .models import ContextItem, PipelineResult, estimate_tokens
from .query_processor import QueryProcessor, QueryProcessResult
from .retriever import Retriever
from .section_aggregator import SectionAggregator

logger = logging.getLogger(__name__)


class Pipeline:
    """
    End-to-end RAG pipeline.

    Typical usage (streaming)::

        from assistant_rh_rag_pipeline import create_pipeline
        pipe = create_pipeline()
        qr = pipe.process_query("Qu'est-ce que le RIFSEEP ?")
        if qr.should_proceed:
            for token in pipe.run_stream(qr):
                print(token, end="")
            result = pipe.last_result

    Typical usage (batch / eval)::

        result = pipe.run("Qu'est-ce que le RIFSEEP ?")
        print(result.answer)
    """

    def __init__(self, config: RAGConfig, dsn: str | None = None):
        self.config = config
        dsn = dsn or get_dsn()

        self._query_processor = QueryProcessor(config.query_processor, verbose=config.verbose)
        self._retriever = Retriever(config.retrieval, dsn=dsn)
        self._aggregator = SectionAggregator(config.aggregation, dsn=dsn)
        self._context_builder = ContextBuilder(config.context, dsn=dsn)
        self._selector = ContextSelector(config.selector)
        self._generator = StreamingGenerator(config.generation)

        self.last_result: Optional[PipelineResult] = None
        self.last_query_result: Optional[QueryProcessResult] = None
        self._timing: Dict[str, float] = {}
        self._stage_refs: Dict[str, list] = {}
        self._stage_trace: Dict[str, Any] = {}

    @property
    def last_full_prompt(self) -> str:
        """The user prompt sent to the generator in the last run."""
        return self._generator.last_full_prompt

    @property
    def last_system_prompt(self) -> str:
        """The system prompt used by the generator in the last run."""
        return self._generator.last_system_prompt

    # ------------------------------------------------------------------
    # Query processing (step 1 – can be called separately for intent check)
    # ------------------------------------------------------------------

    def process_query(
        self,
        query: str,
        conversation_history: list[Dict[str, str]] | None = None,
    ) -> QueryProcessResult:
        t0 = time.time()
        qr = self._query_processor.process(query, conversation_history)
        self._timing["query_processing_ms"] = (time.time() - t0) * 1000
        self.last_query_result = qr
        return qr

    # ------------------------------------------------------------------
    # Full pipeline (non-streaming – for eval)
    # ------------------------------------------------------------------

    def run(
        self,
        query: str,
        conversation_history: list[Dict[str, str]] | None = None,
        include_stage_trace: bool = False,
    ) -> PipelineResult:
        self._timing = {}
        self._stage_refs = {}
        self._stage_trace = {}

        qr = self.process_query(query, conversation_history)

        if not qr.should_proceed:
            metadata: Dict[str, Any] = {
                "intent": qr.intent.value,
                "intent_reason": qr.intent_reason,
            }
            if include_stage_trace:
                metadata["stage_trace"] = self._build_stage_trace(
                    query=query,
                    conversation_history=conversation_history,
                    qr=qr,
                    answer=qr.direct_response or "",
                    context_items=[],
                    metadata=metadata,
                )
            return PipelineResult(
                query=query,
                answer=qr.direct_response or "",
                context_items=[],
                sources=[],
                timing=self._timing,
                metadata=metadata,
            )

        context_items = self._retrieve_and_build(qr)

        if not context_items and self._stage_refs.get("selector_all_rejected"):
            no_answer = (
                "Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances "
                "pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter "
                "votre service RH pour obtenir une réponse précise."
            )
            return self._build_result(query, no_answer, [], qr)

        t0 = time.time()
        answer = self._generator.generate(qr.query_for_retrieval, context_items)
        self._timing["generation_ms"] = (time.time() - t0) * 1000
        self._timing["response_length_tokens"] = estimate_tokens(answer)

        return self._build_result(
            query,
            answer,
            context_items,
            qr,
            conversation_history=conversation_history,
            include_stage_trace=include_stage_trace,
        )

    def run_with_trace(
        self,
        query: str,
        conversation_history: list[Dict[str, str]] | None = None,
    ) -> PipelineResult:
        """Run pipeline and include stage-level input/output trace in metadata."""
        return self.run(
            query=query,
            conversation_history=conversation_history,
            include_stage_trace=True,
        )

    # ------------------------------------------------------------------
    # Streaming pipeline (for Streamlit chatbot)
    # ------------------------------------------------------------------

    def run_stream(
        self,
        qr: QueryProcessResult,
        conversation_history: list[Dict[str, str]] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> Generator[str, None, None]:
        """
        Yield tokens for the streaming answer.

        Call ``process_query`` first to get a ``QueryProcessResult``, check
        ``qr.should_proceed``, then pass it here.  After the generator is
        exhausted, ``self.last_result`` holds the full ``PipelineResult``.

        *on_status* is an optional callback invoked at each pipeline stage
        (useful for updating a Streamlit loader).
        """
        self._timing = {}
        self._stage_refs = {}
        _notify = on_status or (lambda _: None)

        _notify("📚 Recherche dans les sources...")
        context_items = self._retrieve_and_build(qr)

        if not context_items and self._stage_refs.get("selector_all_rejected"):
            _notify("🚫 Aucune source pertinente trouvée")
            no_answer = (
                "Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances "
                "pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter "
                "votre service RH pour obtenir une réponse précise."
            )
            self._timing["generation_ms"] = 0
            self._timing["ttft_ms"] = 0
            self._timing["chars_per_second"] = 0.0
            self._timing["response_length_tokens"] = estimate_tokens(no_answer)
            yield no_answer
            self.last_result = self._build_result(qr.original_query, no_answer, [], qr)
            return

        _notify("✍️ Génération de la réponse...")
        collected: list[str] = []
        t0 = time.time()
        ttft: float = 0.0
        for token in self._generator.stream(qr.query_for_retrieval, context_items, conversation_history):
            if not collected:
                ttft = (time.time() - t0) * 1000
            collected.append(token)
            yield token
        gen_ms = (time.time() - t0) * 1000
        self._timing["generation_ms"] = gen_ms

        answer = "".join(collected)
        self._timing["ttft_ms"] = ttft
        chars = len(answer)
        self._timing["chars_per_second"] = (chars / (gen_ms / 1000)) if gen_ms > 0 else 0.0
        self._timing["response_length_tokens"] = estimate_tokens(answer)

        self.last_result = self._build_result(qr.original_query, answer, context_items, qr)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _retrieve_and_build(self, qr: QueryProcessResult) -> List[ContextItem]:
        retrieval_query = qr.query_for_retrieval

        # ── Stage 1: Retrieve chunks ──────────────────────────────────────
        original_tables = self._retriever.config.tables
        force_hybrid_tables: set[str] = set()
        if not qr.needs_legal_search and "dgafp" in self._retriever.config.tables:
            self._retriever.config.tables = [t for t in original_tables if t != "dgafp"]
        elif qr.needs_legal_search:
            force_hybrid_tables.add("dgafp")

        t0 = time.time()
        chunks = self._retriever.retrieve(retrieval_query, force_hybrid_tables=force_hybrid_tables)
        self._timing["retrieval_ms"] = (time.time() - t0) * 1000
        self._retriever.config.tables = original_tables

        self._stage_refs["retrieved_chunks"] = [
            {
                "chunk_id": str(c.chunk_id),
                "table": c.table_source,
                "score": round(c.score, 4),
                "section_id": str(c.section_id) if c.section_id else "",
            }
            for c in chunks
        ]

        # ── Stage 2: Aggregate chunks into sections + rerank ──────────────
        t0 = time.time()
        sections = self._aggregator.aggregate(chunks, query=retrieval_query)
        self._timing["aggregation_ms"] = (time.time() - t0) * 1000

        self._stage_refs["aggregated_sections"] = [
            {"section_id": str(s.section_id) if s.section_id else "", "heading": (s.heading or "")[:80], "score": round(s.score, 4),
             "publisher": s.publisher or "", "chunk_count": len(s.chunks)}
            for s in sections
        ]

        # ── Stage 3: LLM Selector (filter sections by relevance) ─────────
        if self._selector.enabled:
            sections_before = len(sections)
            t0 = time.time()
            sections = self._selector.select(retrieval_query, sections)
            self._timing["selector_ms"] = (time.time() - t0) * 1000
            self._stage_refs["selector_decisions"] = self._selector.last_decisions
            self._stage_refs["selector_reasoning"] = self._selector.last_reasoning
            self._stage_refs["selector_raw_response"] = self._selector.last_raw_response
            self._stage_refs["selector_items_before"] = sections_before
            self._stage_refs["selector_items_after"] = len(sections)
            self._stage_refs["selector_all_rejected"] = self._selector.all_rejected

            if not sections and self._selector.all_rejected:
                logger.info("Selector rejected all sections – returning empty context")
                self._timing["context_build_ms"] = 0
                self._stage_refs["context_items_ref"] = []
                return []

        # ── Stage 4: ContextBuilder (budget + doc-entire + triangulation + legal refs) ──
        t0 = time.time()
        items = self._context_builder.build(sections)
        self._timing["context_build_ms"] = (time.time() - t0) * 1000

        self._stage_refs["context_items_ref"] = [
            {"section_id": str(it.section_id) if it.section_id else "", "doc_id": str(it.metadata.get("doc_id", "") or ""),
             "heading": (it.heading or "")[:80], "publisher": it.publisher or "",
             "tokens": it.token_estimate, "score": round(it.score, 4),
             "is_doc_entire": it.metadata.get("is_doc_entire", False)}
            for it in items
        ]

        return items

    def _build_result(
        self,
        query: str,
        answer: str,
        context_items: List[ContextItem],
        qr: QueryProcessResult,
        *,
        conversation_history: list[Dict[str, str]] | None = None,
        include_stage_trace: bool = False,
    ) -> PipelineResult:
        sources = []
        seen = set()
        for item in context_items:
            key = (item.section_id or "", item.heading)
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "heading": item.heading,
                "publisher": item.publisher,
                "document_title": item.document_title,
                "document_url": item.document_url,
                "score": item.score,
            })

        metadata: Dict[str, Any] = {
            "intent": qr.intent.value,
            "intent_confidence": qr.intent_confidence,
            "theme": qr.theme,
            "was_expanded": qr.was_expanded,
            "expanded_acronyms": qr.expanded_acronyms,
            "enriched_query": qr.enriched_query,
            "query_for_retrieval": qr.query_for_retrieval,
            "needs_legal_search": qr.needs_legal_search,
            "tables_searched": [t for t in self.config.retrieval.tables if qr.needs_legal_search or t != "dgafp"],
            "selector_enabled": self.config.selector.enabled,
            "generator_model": self.config.generation.model,
            "generator_provider": self.config.generation.provider.value,
            "embedding_model": self.config.retrieval.embedding_model.value,
            "retrieved_chunks": self._stage_refs.get("retrieved_chunks", []),
            "aggregated_sections": self._stage_refs.get("aggregated_sections", []),
            "context_items_ref": self._stage_refs.get("context_items_ref", []),
            "selector_decisions": self._stage_refs.get("selector_decisions", {}),
            "selector_reasoning": self._stage_refs.get("selector_reasoning", ""),
            "selector_raw_response": self._stage_refs.get("selector_raw_response", ""),
            "selector_items_before": self._stage_refs.get("selector_items_before", 0),
            "selector_items_after": self._stage_refs.get("selector_items_after", 0),
            "sections_before_rerank": self._aggregator.last_sections_before_rerank,
            "sections_after_rerank": self._aggregator.last_sections_after_rerank,
            "selector_all_rejected": self._stage_refs.get("selector_all_rejected", False),
        }

        if include_stage_trace:
            metadata["stage_trace"] = self._build_stage_trace(
                query=query,
                conversation_history=conversation_history,
                qr=qr,
                answer=answer,
                context_items=context_items,
                metadata=metadata,
            )

        return PipelineResult(
            query=query,
            answer=answer,
            context_items=context_items,
            sources=sources,
            timing=dict(self._timing),
            metadata=metadata,
        )

    def _build_stage_trace(
        self,
        *,
        query: str,
        conversation_history: list[Dict[str, str]] | None,
        qr: QueryProcessResult,
        answer: str,
        context_items: List[ContextItem],
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        history = conversation_history or []

        retrieved_chunks = metadata.get("retrieved_chunks")
        if not isinstance(retrieved_chunks, list):
            retrieved_chunks = []

        aggregated_sections = metadata.get("aggregated_sections")
        if not isinstance(aggregated_sections, list):
            aggregated_sections = []

        context_items_ref = metadata.get("context_items_ref")
        if not isinstance(context_items_ref, list):
            context_items_ref = []

        tables_searched = metadata.get("tables_searched")
        if not isinstance(tables_searched, list):
            tables_searched = []

        selector_decisions = metadata.get("selector_decisions")
        if not isinstance(selector_decisions, dict):
            selector_decisions = {}

        context_text = "\n\n".join(item.content for item in context_items)
        context_hash = ""
        if context_text:
            context_hash = hashlib.sha256(context_text.encode("utf-8")).hexdigest()

        selected_section_ids = []
        for item in context_items_ref:
            if not isinstance(item, dict):
                continue
            section_id = str(item.get("section_id", "") or "").strip()
            if section_id and section_id not in selected_section_ids:
                selected_section_ids.append(section_id)

        stage_trace = {
            "schema_version": "2026-05-05",
            "stage_order": [
                "query-processor",
                "retriever",
                "section-aggregator",
                "context-selector",
                "context-builder",
                "generator",
            ],
            "stages": {
                "query-processor": {
                    "input": {
                        "query": query,
                        "conversation_history": history,
                    },
                    "output": {
                        "intent": qr.intent.value,
                        "theme": qr.theme,
                        "needs_legal_search": qr.needs_legal_search,
                        "should_proceed": qr.should_proceed,
                        "processed_query": qr.processed_query,
                        "enriched_query": qr.enriched_query,
                        "query_for_retrieval": qr.query_for_retrieval,
                        "search_terms": [],
                    },
                },
                "retriever": {
                    "input": {
                        "query": qr.query_for_retrieval,
                        "needs_legal_search": qr.needs_legal_search,
                        "tables_searched": tables_searched,
                    },
                    "output": {
                        "retrieved_chunks": [
                            {
                                "chunk_id": item.get("chunk_id"),
                                "section_id": item.get("section_id"),
                                "score": item.get("score"),
                                "source_table": item.get("table"),
                            }
                            for item in retrieved_chunks
                            if isinstance(item, dict)
                        ],
                    },
                },
                "section-aggregator": {
                    "input": {
                        "retrieved_chunk_ids": [
                            item.get("chunk_id")
                            for item in retrieved_chunks
                            if isinstance(item, dict)
                        ],
                    },
                    "output": {
                        "aggregated_sections": [
                            {
                                "section_id": item.get("section_id"),
                                "score": item.get("score"),
                                "publisher": item.get("publisher"),
                                "heading": item.get("heading"),
                                "chunk_count": item.get("chunk_count"),
                            }
                            for item in aggregated_sections
                            if isinstance(item, dict)
                        ],
                    },
                },
                "context-selector": {
                    "input": {
                        "query": qr.query_for_retrieval,
                        "aggregated_section_ids": [
                            item.get("section_id")
                            for item in aggregated_sections
                            if isinstance(item, dict)
                        ],
                    },
                    "output": {
                        "selector_enabled": bool(metadata.get("selector_enabled", False)),
                        "selector_all_rejected": bool(metadata.get("selector_all_rejected", False)),
                        "selector_items_before": metadata.get("selector_items_before", 0),
                        "selector_items_after": metadata.get("selector_items_after", 0),
                        "selector_decisions": selector_decisions,
                        "selected_section_ids": selected_section_ids,
                    },
                },
                "context-builder": {
                    "input": {
                        "selected_section_ids": selected_section_ids,
                    },
                    "output": {
                        "context_items_ref": context_items_ref,
                        "context_text_hash": context_hash,
                    },
                },
                "generator": {
                    "input": {
                        "query": qr.query_for_retrieval,
                        "context_item_count": len(context_items),
                        "conversation_history": history,
                    },
                    "output": {
                        "answer": answer,
                        "answer_length": len(answer),
                        "citations": [],
                    },
                },
            },
        }

        self._stage_trace = stage_trace
        return stage_trace
