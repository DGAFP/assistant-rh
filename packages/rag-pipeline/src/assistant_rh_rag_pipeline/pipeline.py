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
from dataclasses import dataclass, field
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


@dataclass
class _RunState:
    """Request-scoped pipeline state.

    Keep diagnostic and timing state local to a single run so concurrent
    requests do not mix intermediate traces on the shared Pipeline instance.
    """

    timing: Dict[str, float] = field(default_factory=dict)
    stage_refs: Dict[str, Any] = field(default_factory=dict)
    aggregation_diagnostics: Any = None


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
        # Retained for legacy/manual step callers; full pipeline runs create a
        # request-scoped selector instance to avoid mixing selector diagnostics.
        self._selector = ContextSelector(config.selector)
        self._generator = StreamingGenerator(config.generation)

        self.last_result: Optional[PipelineResult] = None
        self.last_query_result: Optional[QueryProcessResult] = None
        # Backward-compatible snapshot of the most recent standalone query/run.
        # Internal run state is kept in _RunState instances.
        self._timing: Dict[str, float] = {}

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
        state = _RunState()
        qr = self._process_query(query, conversation_history, state)
        self._timing = dict(state.timing)
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
        state = _RunState()
        qr = self._process_query(query, conversation_history, state)

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
            result = PipelineResult(
                query=query,
                answer=qr.direct_response or "",
                context_items=[],
                sources=[],
                timing=dict(state.timing),
                metadata=metadata,
            )
            self.last_result = result
            self._timing = dict(result.timing)
            return result

        context_items = self._retrieve_and_build(qr, state)

        if not context_items and state.stage_refs.get("selector_all_rejected"):
            no_answer = (
                "Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances "
                "pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter "
                "votre service RH pour obtenir une réponse précise."
            )
            result = self._build_result(
                query,
                no_answer,
                [],
                qr,
                state=state,
                conversation_history=conversation_history,
                include_stage_trace=include_stage_trace,
            )
            self.last_result = result
            self._timing = dict(result.timing)
            return result

        t0 = time.time()
        answer = self._generator.generate(qr.query_for_retrieval, context_items)
        state.timing["generation_ms"] = (time.time() - t0) * 1000
        state.timing["response_length_tokens"] = estimate_tokens(answer)

        result = self._build_result(
            query,
            answer,
            context_items,
            qr,
            state=state,
            conversation_history=conversation_history,
            include_stage_trace=include_stage_trace,
        )
        self.last_result = result
        self._timing = dict(result.timing)
        return result

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
        state = _RunState()
        _notify = on_status or (lambda _: None)

        _notify("📚 Recherche dans les sources...")
        context_items = self._retrieve_and_build(qr, state)

        if not context_items and state.stage_refs.get("selector_all_rejected"):
            _notify("🚫 Aucune source pertinente trouvée")
            no_answer = (
                "Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances "
                "pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter "
                "votre service RH pour obtenir une réponse précise."
            )
            state.timing["generation_ms"] = 0
            state.timing["ttft_ms"] = 0
            state.timing["chars_per_second"] = 0.0
            state.timing["response_length_tokens"] = estimate_tokens(no_answer)
            yield no_answer
            self.last_result = self._build_result(qr.original_query, no_answer, [], qr, state=state)
            self._timing = dict(self.last_result.timing)
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
        state.timing["generation_ms"] = gen_ms

        answer = "".join(collected)
        state.timing["ttft_ms"] = ttft
        chars = len(answer)
        state.timing["chars_per_second"] = (chars / (gen_ms / 1000)) if gen_ms > 0 else 0.0
        state.timing["response_length_tokens"] = estimate_tokens(answer)

        self.last_result = self._build_result(qr.original_query, answer, context_items, qr, state=state)
        self._timing = dict(self.last_result.timing)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_query(
        self,
        query: str,
        conversation_history: list[Dict[str, str]] | None,
        state: _RunState,
    ) -> QueryProcessResult:
        t0 = time.time()
        qr = self._query_processor.process(query, conversation_history)
        state.timing["query_processing_ms"] = (time.time() - t0) * 1000
        self.last_query_result = qr
        return qr

    def _retrieve_and_build(self, qr: QueryProcessResult, state: _RunState) -> List[ContextItem]:
        retrieval_query = qr.query_for_retrieval

        # ── Stage 1: Retrieve chunks ──────────────────────────────────────
        configured_tables = list(self._retriever.config.tables)
        force_hybrid_tables: set[str] = set()
        if qr.needs_legal_search:
            active_tables = configured_tables
            force_hybrid_tables.add("dgafp")
        else:
            active_tables = [t for t in configured_tables if t != "dgafp"]

        tables_searched = list(active_tables)
        # rag_chunks_test is controlled by a dedicated config flag inside
        # Retriever, not by the request-scoped table-key list. Include it in
        # diagnostics when enabled because it is still searched.
        if getattr(self._retriever.config, "enable_chunks_test", False) is True:
            tables_searched.append("rag_chunks_test")
        state.stage_refs["tables_searched"] = tables_searched

        t0 = time.time()
        chunks = self._retriever.retrieve(
            retrieval_query,
            force_hybrid_tables=force_hybrid_tables,
            tables=active_tables,
        )
        state.timing["retrieval_ms"] = (time.time() - t0) * 1000

        state.stage_refs["retrieved_chunks"] = [
            {
                "chunk_id": str(c.chunk_id),
                "table": c.table_source,
                "score": round(c.score, 4),
                "section_id": str(c.section_id) if c.section_id else "",
                "preview": _preview(c.text),
            }
            for c in chunks
        ]

        # ── Stage 2: Aggregate chunks into sections + rerank ──────────────
        t0 = time.time()
        aggregation_result = self._aggregator.aggregate_with_diagnostics(chunks, query=retrieval_query)
        sections = aggregation_result.sections
        state.aggregation_diagnostics = aggregation_result.diagnostics
        state.timing["aggregation_ms"] = (time.time() - t0) * 1000

        state.stage_refs["aggregated_sections"] = [
            {"section_id": str(s.section_id) if s.section_id else "", "heading": (s.heading or "")[:80], "score": round(s.score, 4),
             "publisher": s.publisher or "", "chunk_count": len(s.chunks), "token_estimate": s.token_estimate,
             "document_id": str(s.document_id or s.metadata.get("doc_id", "") or "")}
            for s in sections
        ]

        # ── Stage 3: LLM Selector (filter sections by relevance) ─────────
        if self.config.selector.enabled:
            sections_before = len(sections)
            t0 = time.time()
            selector = ContextSelector(self.config.selector)
            sections = selector.select(retrieval_query, sections)
            state.timing["selector_ms"] = (time.time() - t0) * 1000
            state.stage_refs["selector_decisions"] = selector.last_decisions
            state.stage_refs["selector_reasoning"] = selector.last_reasoning
            state.stage_refs["selector_raw_response"] = selector.last_raw_response
            state.stage_refs["selector_items_before"] = sections_before
            state.stage_refs["selector_items_after"] = len(sections)
            state.stage_refs["selector_all_rejected"] = selector.all_rejected
            state.stage_refs["selector_rejection_reason"] = (
                selector.last_reasoning if selector.all_rejected else ""
            )
            # Backward-compatible snapshot for manual step callers that inspect
            # pipe._selector after a run. The pipeline's own diagnostics use the
            # request-local selector above and do not read this shared snapshot.
            self._selector = selector

            if not sections and selector.all_rejected:
                logger.info("Selector rejected all sections – returning empty context")
                state.timing["context_build_ms"] = 0
                state.stage_refs["context_items_ref"] = []
                return []

        # ── Stage 4: ContextBuilder (budget + doc-entire + triangulation + legal refs) ──
        t0 = time.time()
        items = self._context_builder.build(sections)
        state.timing["context_build_ms"] = (time.time() - t0) * 1000

        state.stage_refs["context_items_ref"] = [
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
        state: _RunState,
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
            "original_query": query,
            "intent": qr.intent.value,
            "intent_confidence": qr.intent_confidence,
            "theme": qr.theme,
            "was_expanded": qr.was_expanded,
            "expanded_acronyms": qr.expanded_acronyms,
            "enriched_query": qr.enriched_query,
            "query_for_retrieval": qr.query_for_retrieval,
            "needs_legal_search": qr.needs_legal_search,
            "tables_searched": state.stage_refs.get("tables_searched", []),
            "selector_enabled": self.config.selector.enabled,
            "generator_model": self.config.generation.model,
            "generator_provider": self.config.generation.provider.value,
            "embedding_model": self.config.retrieval.embedding_model.value,
            "retrieved_chunks": state.stage_refs.get("retrieved_chunks", []),
            "aggregated_sections": state.stage_refs.get("aggregated_sections", []),
            "context_items_ref": state.stage_refs.get("context_items_ref", []),
            "selector_decisions": state.stage_refs.get("selector_decisions", {}),
            "selector_reasoning": state.stage_refs.get("selector_reasoning", ""),
            "selector_rejection_reason": state.stage_refs.get("selector_rejection_reason", ""),
            "selector_raw_response": state.stage_refs.get("selector_raw_response", ""),
            "selector_items_before": state.stage_refs.get("selector_items_before", 0),
            "selector_items_after": state.stage_refs.get("selector_items_after", 0),
            "sections_before_rerank": _sections_before_rerank(state),
            "sections_after_rerank": _sections_after_rerank(state),
            "selector_all_rejected": state.stage_refs.get("selector_all_rejected", False),
        }
        metadata["selector_decision"] = self._selector_decision(metadata)
        metadata["reranker_status"] = self._reranker_status(state)
        metadata["rag_diagnostics"] = self._build_rag_diagnostics(
            query=query,
            qr=qr,
            metadata=metadata,
        )

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
            timing=dict(state.timing),
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
                        "selector_decision": metadata.get("selector_decision", "not_run"),
                        "rejection_reason": metadata.get("selector_rejection_reason", ""),
                        "reason": metadata.get("selector_reasoning", ""),
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

        return stage_trace

    def _selector_decision(self, metadata: Dict[str, Any]) -> str:
        if not metadata.get("selector_enabled", False):
            return "disabled"
        if metadata.get("selector_all_rejected", False):
            return "all_rejected"
        if int(metadata.get("selector_items_before", 0) or 0) <= 0:
            return "skipped_no_sections"
        return "selected"

    def _reranker_status(self, state: _RunState) -> Dict[str, Any]:
        section_before = _sections_before_rerank(state)
        section_after = _sections_after_rerank(state)
        diagnostics = state.aggregation_diagnostics
        section_status = getattr(diagnostics, "reranker_status", None)
        if not section_status:
            if not self.config.aggregation.enable_section_reranker:
                section_status = "disabled"
            elif section_before <= 0:
                section_status = "skipped_no_sections"
            else:
                section_status = "completed"

        return {
            "chunk": {
                "enabled": bool(self.config.retrieval.enable_chunk_reranker),
                "status": "not_implemented" if self.config.retrieval.enable_chunk_reranker else "disabled",
                "top_k": self.config.retrieval.chunk_rerank_top_k,
            },
            "section": {
                "enabled": bool(self.config.aggregation.enable_section_reranker),
                "status": section_status,
                "top_k": self.config.aggregation.section_rerank_top_k,
                "items_before": section_before,
                "items_after": section_after,
                "error": getattr(diagnostics, "reranker_error", ""),
            },
        }

    def _build_rag_diagnostics(
        self,
        *,
        query: str,
        qr: QueryProcessResult,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "query": {
                "original": query,
                "processed": qr.processed_query,
                "enriched": qr.enriched_query,
                "retrieval": qr.query_for_retrieval,
                "needs_legal_search": qr.needs_legal_search,
            },
            "retrieval": {
                "tables_searched": metadata.get("tables_searched", []),
                "retrieved_chunks": metadata.get("retrieved_chunks", []),
            },
            "aggregation": {
                "sections_before_rerank": metadata.get("sections_before_rerank", 0),
                "sections_after_rerank": metadata.get("sections_after_rerank", 0),
                "aggregated_sections": metadata.get("aggregated_sections", []),
            },
            "reranker": metadata.get("reranker_status", {}),
            "selector": {
                "enabled": metadata.get("selector_enabled", False),
                "decision": metadata.get("selector_decision", "not_run"),
                "all_rejected": metadata.get("selector_all_rejected", False),
                "items_before": metadata.get("selector_items_before", 0),
                "items_after": metadata.get("selector_items_after", 0),
                "decisions": metadata.get("selector_decisions", {}),
                "reason": metadata.get("selector_reasoning", ""),
                "rejection_reason": metadata.get("selector_rejection_reason", ""),
                "raw_response": metadata.get("selector_raw_response", ""),
            },
        }


def _sections_before_rerank(state: _RunState) -> int:
    return int(getattr(state.aggregation_diagnostics, "sections_before_rerank", 0) or 0)


def _sections_after_rerank(state: _RunState) -> int:
    return int(getattr(state.aggregation_diagnostics, "sections_after_rerank", 0) or 0)


def _preview(text: str, max_chars: int = 240) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max(max_chars, 0)
    return f"{normalized[: max_chars - 3]}..."
