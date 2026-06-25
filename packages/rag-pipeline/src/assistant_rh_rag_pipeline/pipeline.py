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
from types import SimpleNamespace
from typing import Any, Callable, Dict, Generator, List, Optional

from .config import RAGConfig, SearchMode
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


@dataclass
class _RetrievalAttempt:
    """Request-scoped diagnostics for one retrieval/selection attempt."""

    name: str
    search_mode: SearchMode
    top_k: int
    tables_searched: list[str]
    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    aggregated_sections: list[dict[str, Any]] = field(default_factory=list)
    context_items_ref: list[dict[str, Any]] = field(default_factory=list)
    selector_decisions: dict[str, Any] = field(default_factory=dict)
    selector_reasoning: str = ""
    selector_raw_response: str = ""
    selector_items_before: int = 0
    selector_items_after: int = 0
    selector_all_rejected: bool = False
    selector_rejection_reason: str = ""
    sections_before_rerank: int = 0
    sections_after_rerank: int = 0
    reranker_status: str = ""
    reranker_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "search_mode": self.search_mode.value,
            "top_k": self.top_k,
            "tables_searched": self.tables_searched,
            "retrieved_chunks": self.retrieved_chunks,
            "aggregated_sections": self.aggregated_sections,
            "context_items_ref": self.context_items_ref,
            "selector": {
                "decisions": self.selector_decisions,
                "reason": self.selector_reasoning,
                "raw_response": self.selector_raw_response,
                "items_before": self.selector_items_before,
                "items_after": self.selector_items_after,
                "all_rejected": self.selector_all_rejected,
                "rejection_reason": self.selector_rejection_reason,
            },
            "aggregation": {
                "sections_before_rerank": self.sections_before_rerank,
                "sections_after_rerank": self.sections_after_rerank,
                "reranker_status": self.reranker_status,
                "reranker_error": self.reranker_error,
            },
        }


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
        # Full pipeline runs create request-scoped selector instances to avoid
        # mixing selector diagnostics. This snapshot is updated after selector runs
        # for legacy/manual callers that inspect ``pipe._selector``.
        # Initialized lazily via the property to avoid consuming mock side_effects
        # in tests and to defer construction until first access.
        self._selector: ContextSelector | None = None
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

        active_tables = list(self._retriever.config.tables)
        force_hybrid_tables: set[str] = set()
        if "dgafp" in active_tables:
            force_hybrid_tables.add("dgafp")

        initial_attempt = self._run_retrieval_attempt(
            name="initial",
            retrieval_query=retrieval_query,
            active_tables=active_tables,
            force_hybrid_tables=force_hybrid_tables,
            state=state,
            search_mode=self.config.retrieval.search_mode,
            top_k=self.config.retrieval.initial_top_k,
        )
        attempts = [initial_attempt]
        items = initial_attempt.context_items_ref

        if items or not initial_attempt.selector_all_rejected:
            self._set_latest_attempt_state(state, initial_attempt, attempts)
            return state.stage_refs.get("_latest_context_items", [])

        retry_enabled = bool(self.config.retrieval.enable_selector_retry)
        state.stage_refs["selector_retry_triggered"] = retry_enabled
        if not retry_enabled:
            self._set_latest_attempt_state(state, initial_attempt, attempts)
            return []

        logger.info(
            "Selector rejected all sections – retrying retrieval with %s/top_k=%d",
            self.config.retrieval.selector_retry_search_mode.value,
            self.config.retrieval.selector_retry_top_k,
        )
        retry_attempt = self._run_retrieval_attempt(
            name="selector_retry",
            retrieval_query=retrieval_query,
            active_tables=active_tables,
            force_hybrid_tables=force_hybrid_tables,
            state=state,
            search_mode=self.config.retrieval.selector_retry_search_mode,
            top_k=self.config.retrieval.selector_retry_top_k,
        )
        attempts.append(retry_attempt)
        retry_has_context = bool(retry_attempt.context_items_ref)
        self._set_latest_attempt_state(state, retry_attempt, attempts)
        state.stage_refs["selector_retry_succeeded"] = retry_has_context and not retry_attempt.selector_all_rejected
        # If the retry also failed to produce context, preserve the no-answer
        # signal so run()/run_stream() takes the no-answer path regardless
        # of whether the retry selector explicitly rejected or just returned
        # empty sections.
        if not retry_has_context:
            state.stage_refs["selector_all_rejected"] = True
            state.stage_refs["selector_rejection_reason"] = retry_attempt.selector_rejection_reason or initial_attempt.selector_rejection_reason
        return state.stage_refs.get("_latest_context_items", [])

    def _run_retrieval_attempt(
        self,
        *,
        name: str,
        retrieval_query: str,
        active_tables: list[str],
        force_hybrid_tables: set[str],
        state: _RunState,
        search_mode: SearchMode,
        top_k: int,
    ) -> _RetrievalAttempt:
        tables_searched = list(active_tables)
        if getattr(self._retriever.config, "enable_chunks_test", False) is True:
            tables_searched.append("rag_chunks_test")

        attempt = _RetrievalAttempt(
            name=name,
            search_mode=search_mode,
            top_k=top_k,
            tables_searched=tables_searched,
        )

        t0 = time.time()
        chunks = self._retriever.retrieve(
            retrieval_query,
            force_hybrid_tables=force_hybrid_tables,
            tables=active_tables,
            search_mode=search_mode,
            top_k=top_k,
        )
        state.timing[f"retrieval_{name}_ms"] = (time.time() - t0) * 1000

        attempt.retrieved_chunks = [
            {
                "chunk_id": str(c.chunk_id),
                "table": c.table_source,
                "score": round(c.score, 4),
                "section_id": str(c.section_id) if c.section_id else "",
                "preview": _preview(c.text),
                "retrieval_path": c.metadata.get("retrieval_path", "chunk"),
                "heading_search": bool(c.metadata.get("heading_search", False)),
                "heading_match_score": c.metadata.get("heading_match_score"),
            }
            for c in chunks
        ]

        t0 = time.time()
        aggregation_result = self._aggregator.aggregate_with_diagnostics(chunks, query=retrieval_query)
        sections = aggregation_result.sections
        diagnostics = aggregation_result.diagnostics
        state.timing[f"aggregation_{name}_ms"] = (time.time() - t0) * 1000

        attempt.sections_before_rerank = int(getattr(diagnostics, "sections_before_rerank", 0) or 0)
        attempt.sections_after_rerank = int(getattr(diagnostics, "sections_after_rerank", 0) or 0)
        attempt.reranker_status = str(getattr(diagnostics, "reranker_status", "") or "")
        attempt.reranker_error = str(getattr(diagnostics, "reranker_error", "") or "")
        attempt.aggregated_sections = [
            {
                "section_id": str(s.section_id) if s.section_id else "",
                "heading": (s.heading or "")[:80],
                "score": round(s.score, 4),
                "publisher": s.publisher or "",
                "chunk_count": len(s.chunks),
                "token_estimate": s.token_estimate,
                "document_id": str(s.document_id or s.metadata.get("doc_id", "") or ""),
            }
            for s in sections
        ]

        if self.config.selector.enabled:
            sections_before = len(sections)
            t0 = time.time()
            selector = ContextSelector(self.config.selector)
            sections = selector.select(retrieval_query, sections)
            state.timing[f"selector_{name}_ms"] = (time.time() - t0) * 1000
            attempt.selector_decisions = selector.last_decisions
            attempt.selector_reasoning = selector.last_reasoning
            attempt.selector_raw_response = selector.last_raw_response
            attempt.selector_items_before = sections_before
            attempt.selector_items_after = len(sections)
            attempt.selector_all_rejected = selector.all_rejected
            attempt.selector_rejection_reason = selector.last_reasoning if selector.all_rejected else ""
            self._selector = selector

            if not sections and selector.all_rejected:
                logger.info("Selector rejected all sections on %s attempt", name)
                state.timing[f"context_build_{name}_ms"] = 0
                return attempt

        t0 = time.time()
        items = self._context_builder.build(sections)
        state.timing[f"context_build_{name}_ms"] = (time.time() - t0) * 1000
        attempt.context_items_ref = [
            {
                "section_id": str(it.section_id) if it.section_id else "",
                "doc_id": str(it.metadata.get("doc_id", "") or ""),
                "heading": (it.heading or "")[:80],
                "publisher": it.publisher or "",
                "tokens": it.token_estimate,
                "score": round(it.score, 4),
                "is_doc_entire": it.metadata.get("is_doc_entire", False),
            }
            for it in items
        ]
        state.stage_refs["_latest_context_items"] = items
        return attempt

    def _set_latest_attempt_state(
        self,
        state: _RunState,
        latest: _RetrievalAttempt,
        attempts: list[_RetrievalAttempt],
    ) -> None:
        state.stage_refs["retrieval_attempts"] = [attempt.to_dict() for attempt in attempts]
        state.stage_refs["tables_searched"] = latest.tables_searched
        state.stage_refs["retrieved_chunks"] = latest.retrieved_chunks
        state.stage_refs["aggregated_sections"] = latest.aggregated_sections
        state.stage_refs["context_items_ref"] = latest.context_items_ref
        state.stage_refs["selector_decisions"] = latest.selector_decisions
        state.stage_refs["selector_reasoning"] = latest.selector_reasoning
        state.stage_refs["selector_raw_response"] = latest.selector_raw_response
        state.stage_refs["selector_items_before"] = latest.selector_items_before
        state.stage_refs["selector_items_after"] = latest.selector_items_after
        state.stage_refs["selector_all_rejected"] = latest.selector_all_rejected
        state.stage_refs["selector_rejection_reason"] = latest.selector_rejection_reason
        state.stage_refs.setdefault("selector_retry_triggered", len(attempts) > 1)
        state.stage_refs.setdefault("selector_retry_succeeded", False)

        diagnostics = SimpleNamespace(
            sections_before_rerank=latest.sections_before_rerank,
            sections_after_rerank=latest.sections_after_rerank,
            reranker_status=latest.reranker_status,
            reranker_error=latest.reranker_error,
        )
        state.aggregation_diagnostics = diagnostics

        state.timing["retrieval_ms"] = sum(state.timing.get(f"retrieval_{attempt.name}_ms", 0.0) for attempt in attempts)
        state.timing["aggregation_ms"] = sum(state.timing.get(f"aggregation_{attempt.name}_ms", 0.0) for attempt in attempts)
        state.timing["selector_ms"] = sum(state.timing.get(f"selector_{attempt.name}_ms", 0.0) for attempt in attempts)
        state.timing["context_build_ms"] = sum(state.timing.get(f"context_build_{attempt.name}_ms", 0.0) for attempt in attempts)

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
            sources.append(
                {
                    "heading": item.heading,
                    "publisher": item.publisher,
                    "document_title": item.document_title,
                    "document_url": item.document_url,
                    "score": item.score,
                }
            )

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
            "needs_legal_search_llm": qr.needs_legal_search_llm,
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
            "selector_retry_triggered": state.stage_refs.get("selector_retry_triggered", False),
            "selector_retry_succeeded": state.stage_refs.get("selector_retry_succeeded", False),
            "retrieval_attempts": state.stage_refs.get("retrieval_attempts", []),
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

        retrieval_attempts = metadata.get("retrieval_attempts")
        if not isinstance(retrieval_attempts, list):
            retrieval_attempts = []

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
                        "needs_legal_search_llm": qr.needs_legal_search_llm,
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
                        "attempts": retrieval_attempts,
                    },
                },
                "section-aggregator": {
                    "input": {
                        "retrieved_chunk_ids": [item.get("chunk_id") for item in retrieved_chunks if isinstance(item, dict)],
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
                        "attempts": retrieval_attempts,
                    },
                },
                "context-selector": {
                    "input": {
                        "query": qr.query_for_retrieval,
                        "aggregated_section_ids": [item.get("section_id") for item in aggregated_sections if isinstance(item, dict)],
                    },
                    "output": {
                        "selector_enabled": bool(metadata.get("selector_enabled", False)),
                        "selector_all_rejected": bool(metadata.get("selector_all_rejected", False)),
                        "selector_items_before": metadata.get("selector_items_before", 0),
                        "selector_items_after": metadata.get("selector_items_after", 0),
                        "selector_decisions": selector_decisions,
                        "selector_decision": metadata.get("selector_decision", "not_run"),
                        "selector_retry_triggered": bool(metadata.get("selector_retry_triggered", False)),
                        "selector_retry_succeeded": bool(metadata.get("selector_retry_succeeded", False)),
                        "rejection_reason": metadata.get("selector_rejection_reason", ""),
                        "reason": metadata.get("selector_reasoning", ""),
                        "selected_section_ids": selected_section_ids,
                        "attempts": retrieval_attempts,
                    },
                },
                "context-builder": {
                    "input": {
                        "selected_section_ids": selected_section_ids,
                        "selector_retry_triggered": bool(metadata.get("selector_retry_triggered", False)),
                        "attempts": retrieval_attempts,
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
                "needs_legal_search_llm": qr.needs_legal_search_llm,
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
            "selector_retry": {
                "enabled": metadata.get("selector_enabled", False) and self.config.retrieval.enable_selector_retry,
                "triggered": metadata.get("selector_retry_triggered", False),
                "succeeded": metadata.get("selector_retry_succeeded", False),
                "search_mode": self.config.retrieval.selector_retry_search_mode.value,
                "top_k": self.config.retrieval.selector_retry_top_k,
            },
            "attempts": metadata.get("retrieval_attempts", []),
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
