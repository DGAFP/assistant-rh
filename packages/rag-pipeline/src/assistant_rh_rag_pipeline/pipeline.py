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
from .ministry_scope import MinistrySource, RetrievalScope, resolve_ministry
from .models import ContextItem, PipelineResult, estimate_tokens, serialize_raw_chunks, serialize_section_chunks
from .query_processor import QueryProcessor, QueryProcessResult
from .retriever import Retriever
from .section_aggregator import SectionAggregator
from .tracing import bounded_preview, chunk_ref, context_item_ref, make_trace_event, normalize_trace_id, section_ref

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
    turn_id: str = ""
    trace_id: str = field(default_factory=normalize_trace_id)
    trace_events: list[dict[str, Any]] = field(default_factory=list)


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
    chunks_raw: list[dict[str, Any]] = field(default_factory=list)
    chunks_before_rerank: list[dict[str, Any]] = field(default_factory=list)
    chunks_after_rerank: list[dict[str, Any]] = field(default_factory=list)
    context_before_selector: list[dict[str, Any]] = field(default_factory=list)
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
            "chunks_raw": self.chunks_raw,
            "chunks_before_rerank": self.chunks_before_rerank,
            "chunks_after_rerank": self.chunks_after_rerank,
            "context_before_selector": self.context_before_selector,
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


def _intent_value(qr: QueryProcessResult) -> str:
    intent = getattr(qr, "intent", None)
    if intent is None:
        return "unknown"
    return str(getattr(intent, "value", intent) or "unknown")


def _record_scope(state: _RunState, retrieval_scope: RetrievalScope | None) -> None:
    if retrieval_scope is None:
        return
    scope_dict = retrieval_scope.to_dict()
    state.stage_refs["retrieval_scope"] = scope_dict
    state.stage_refs["selected_ministry"] = retrieval_scope.selected_ministry
    state.stage_refs["scoped_table_keys"] = list(retrieval_scope.table_keys)


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
        retrieval_scope: RetrievalScope | None = None,
    ) -> QueryProcessResult:
        state = _RunState()
        qr = self._process_query(query, conversation_history, state, ministry=resolve_ministry(retrieval_scope))
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
        turn_id: str | None = None,
        trace_id: str | None = None,
        retrieval_scope: RetrievalScope | None = None,
    ) -> PipelineResult:
        state = _RunState(turn_id=turn_id or "", trace_id=normalize_trace_id(trace_id))
        _record_scope(state, retrieval_scope)
        ministry = resolve_ministry(retrieval_scope)
        qr = self._process_query(query, conversation_history, state, ministry=ministry)

        if not qr.should_proceed:
            metadata: Dict[str, Any] = {
                "intent": _intent_value(qr),
                "intent_reason": qr.intent_reason,
                "turn_id": state.turn_id,
                "trace_id": state.trace_id,
                "rag_trace_events": list(state.trace_events),
                "retrieval_scope": state.stage_refs.get("retrieval_scope"),
                "selected_ministry": state.stage_refs.get("selected_ministry"),
                "scoped_table_keys": state.stage_refs.get("scoped_table_keys", []),
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

        context_items = self._retrieve_and_build(qr, state, retrieval_scope=retrieval_scope, ministry=ministry)

        if not context_items and state.stage_refs.get("selector_all_rejected"):
            no_answer = (
                "Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances "
                "pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter "
                "votre service RH pour obtenir une réponse précise."
            )
            state.timing["generation_ms"] = 0
            state.timing["response_length_tokens"] = estimate_tokens(no_answer)
            self._record_generator_event(qr, [], no_answer, state, duration_ms=0, status="skipped_no_context")
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
        answer = self._generator.generate(qr.query_for_retrieval, context_items, ministry=ministry)
        generation_ms = (time.time() - t0) * 1000
        state.timing["generation_ms"] = generation_ms
        state.timing["response_length_tokens"] = estimate_tokens(answer)
        self._record_generator_event(qr, context_items, answer, state, duration_ms=generation_ms)

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
        turn_id: str | None = None,
        trace_id: str | None = None,
        retrieval_scope: RetrievalScope | None = None,
    ) -> PipelineResult:
        """Run pipeline and include stage-level input/output trace in metadata."""
        return self.run(
            query=query,
            conversation_history=conversation_history,
            include_stage_trace=True,
            turn_id=turn_id,
            trace_id=trace_id,
            retrieval_scope=retrieval_scope,
        )

    # ------------------------------------------------------------------
    # Streaming pipeline (for Streamlit chatbot)
    # ------------------------------------------------------------------

    def run_stream(
        self,
        qr: QueryProcessResult,
        conversation_history: list[Dict[str, str]] | None = None,
        on_status: Callable[[str], None] | None = None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        retrieval_scope: RetrievalScope | None = None,
    ) -> Generator[str, None, None]:
        """
        Yield tokens for the streaming answer.

        Call ``process_query`` first to get a ``QueryProcessResult``, check
        ``qr.should_proceed``, then pass it here.  After the generator is
        exhausted, ``self.last_result`` holds the full ``PipelineResult``.

        *on_status* is an optional callback invoked at each pipeline stage
        (useful for updating a Streamlit loader).
        """
        state = _RunState(turn_id=turn_id or "", trace_id=normalize_trace_id(trace_id))
        _record_scope(state, retrieval_scope)
        ministry = resolve_ministry(retrieval_scope)
        self._record_query_processor_event(
            query=qr.original_query,
            conversation_history=conversation_history,
            qr=qr,
            state=state,
            duration_ms=self._timing.get("query_processing_ms", 0),
        )
        _notify = on_status or (lambda _: None)

        _notify("📚 Recherche dans les sources...")
        context_items = self._retrieve_and_build(qr, state, retrieval_scope=retrieval_scope, ministry=ministry)

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
            self._record_generator_event(qr, [], no_answer, state, duration_ms=0, status="skipped_no_context")
            yield no_answer
            self.last_result = self._build_result(qr.original_query, no_answer, [], qr, state=state)
            self._timing = dict(self.last_result.timing)
            return

        _notify("✍️ Génération de la réponse...")
        collected: list[str] = []
        t0 = time.time()
        ttft: float = 0.0
        for token in self._generator.stream(qr.query_for_retrieval, context_items, conversation_history, ministry=ministry):
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
        self._record_generator_event(qr, context_items, answer, state, duration_ms=gen_ms)

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
        ministry: MinistrySource | None = None,
    ) -> QueryProcessResult:
        t0 = time.time()
        qr = self._query_processor.process(query, conversation_history, ministry=ministry)
        duration_ms = (time.time() - t0) * 1000
        state.timing["query_processing_ms"] = duration_ms
        self._record_query_processor_event(query=query, conversation_history=conversation_history, qr=qr, state=state, duration_ms=duration_ms)
        self.last_query_result = qr
        return qr

    def _retrieve_and_build(
        self,
        qr: QueryProcessResult,
        state: _RunState,
        *,
        retrieval_scope: RetrievalScope | None = None,
        ministry: MinistrySource | None = None,
    ) -> List[ContextItem]:
        retrieval_query = qr.query_for_retrieval

        active_tables = list(retrieval_scope.table_keys) if retrieval_scope is not None else list(self._retriever.config.tables)
        strict_table_errors = retrieval_scope is not None
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
            strict_table_errors=strict_table_errors,
            ministry=ministry,
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
            strict_table_errors=strict_table_errors,
            ministry=ministry,
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
        strict_table_errors: bool,
        ministry: MinistrySource | None = None,
    ) -> _RetrievalAttempt:
        tables_searched = list(active_tables)

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
            strict_table_errors=strict_table_errors,
        )
        retrieval_ms = (time.time() - t0) * 1000
        state.timing[f"retrieval_{name}_ms"] = retrieval_ms

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
        attempt.chunks_raw = serialize_raw_chunks(chunks)
        state.trace_events.append(
            make_trace_event(
                stage="retriever",
                attempt_name=name,
                duration_ms=retrieval_ms,
                status="ok" if chunks else "empty",
                input_ref={
                    "query": bounded_preview(retrieval_query, 1_000),
                    "tables_searched": tables_searched,
                    "search_mode": search_mode.value,
                    "top_k": top_k,
                    "retrieval_scope": state.stage_refs.get("retrieval_scope"),
                },
                output_ref={"retrieved_chunks": [chunk_ref(chunk) for chunk in chunks]},
                metrics={
                    "chunk_count": len(chunks),
                    "top_score": round(max((chunk.score for chunk in chunks), default=0.0), 4),
                    "avg_score": round(sum(chunk.score for chunk in chunks) / len(chunks), 4) if chunks else 0.0,
                },
            )
        )

        t0 = time.time()
        aggregation_result = self._aggregator.aggregate_with_diagnostics(chunks, query=retrieval_query)
        sections = aggregation_result.sections
        diagnostics = aggregation_result.diagnostics
        aggregation_ms = (time.time() - t0) * 1000
        state.timing[f"aggregation_{name}_ms"] = aggregation_ms

        attempt.sections_before_rerank = int(getattr(diagnostics, "sections_before_rerank", 0) or 0)
        attempt.sections_after_rerank = int(getattr(diagnostics, "sections_after_rerank", 0) or 0)
        attempt.reranker_status = str(getattr(diagnostics, "reranker_status", "") or "")
        attempt.reranker_error = str(getattr(diagnostics, "reranker_error", "") or "")
        attempt.chunks_before_rerank = list(getattr(diagnostics, "chunks_before_rerank", []) or [])
        attempt.chunks_after_rerank = list(getattr(diagnostics, "chunks_after_rerank", []) or [])
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
        attempt.context_before_selector = serialize_section_chunks(
            sections,
            include_rerank_score=attempt.reranker_status == "completed",
        )
        aggregation_status = "failed" if attempt.reranker_status == "failed" else "ok" if sections else "empty"
        state.trace_events.append(
            make_trace_event(
                stage="section-aggregator",
                attempt_name=name,
                duration_ms=aggregation_ms,
                status=aggregation_status,
                input_ref={"retrieved_chunk_ids": [str(c.chunk_id) for c in chunks]},
                output_ref={"aggregated_sections": [section_ref(section) for section in sections]},
                metrics={
                    "sections_before_rerank": attempt.sections_before_rerank,
                    "sections_after_rerank": attempt.sections_after_rerank,
                    "reranker_status": attempt.reranker_status,
                },
                error_type="reranker_error" if attempt.reranker_status == "failed" else "",
                error_message=attempt.reranker_error,
            )
        )

        if self.config.selector.enabled:
            sections_before = len(sections)
            t0 = time.time()
            selector = ContextSelector(self.config.selector)
            sections = selector.select(retrieval_query, sections, ministry=ministry)
            selector_ms = (time.time() - t0) * 1000
            state.timing[f"selector_{name}_ms"] = selector_ms
            attempt.selector_decisions = selector.last_decisions
            attempt.selector_reasoning = selector.last_reasoning
            attempt.selector_raw_response = selector.last_raw_response
            attempt.selector_items_before = sections_before
            attempt.selector_items_after = len(sections)
            attempt.selector_all_rejected = selector.all_rejected
            attempt.selector_rejection_reason = selector.last_reasoning if selector.all_rejected else ""
            self._selector = selector
            selector_status = "all_rejected" if selector.all_rejected else "ok" if sections_before else "skipped_no_sections"
            state.trace_events.append(
                make_trace_event(
                    stage="context-selector",
                    attempt_name=name,
                    duration_ms=selector_ms,
                    status=selector_status,
                    input_ref={
                        "query": bounded_preview(retrieval_query, 1_000),
                        "section_ids": [str(section.section_id or "") for section in aggregation_result.sections],
                    },
                    output_ref={
                        "selector_decisions": selector.last_decisions,
                        "selected_sections": [section_ref(section, include_chunks=False) for section in sections],
                        "reason": bounded_preview(selector.last_reasoning, 2_000),
                    },
                    metrics={
                        "items_before": attempt.selector_items_before,
                        "items_after": attempt.selector_items_after,
                        "all_rejected": attempt.selector_all_rejected,
                    },
                )
            )

            if not sections and selector.all_rejected:
                logger.info("Selector rejected all sections on %s attempt", name)
                state.timing[f"context_build_{name}_ms"] = 0
                return attempt
        else:
            state.trace_events.append(
                make_trace_event(
                    stage="context-selector",
                    attempt_name=name,
                    duration_ms=0,
                    status="disabled",
                    input_ref={"section_ids": [str(section.section_id or "") for section in sections]},
                    output_ref={"selected_sections": [section_ref(section, include_chunks=False) for section in sections]},
                    metrics={"items_before": len(sections), "items_after": len(sections), "all_rejected": False},
                )
            )

        t0 = time.time()
        items = self._context_builder.build(sections)
        context_build_ms = (time.time() - t0) * 1000
        state.timing[f"context_build_{name}_ms"] = context_build_ms
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
        state.trace_events.append(
            make_trace_event(
                stage="context-builder",
                attempt_name=name,
                duration_ms=context_build_ms,
                status="ok" if items else "empty",
                input_ref={"selected_section_ids": [str(section.section_id or "") for section in sections]},
                output_ref={"context_items": [context_item_ref(item) for item in items]},
                metrics={
                    "context_item_count": len(items),
                    "context_tokens": sum(item.token_estimate for item in items),
                    "doc_entire_count": sum(1 for item in items if item.metadata.get("is_doc_entire", False)),
                },
            )
        )
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
        state.stage_refs["chunks_raw"] = latest.chunks_raw
        state.stage_refs["chunks_before_rerank"] = latest.chunks_before_rerank
        state.stage_refs["chunks_after_rerank"] = latest.chunks_after_rerank
        state.stage_refs["context_before_selector"] = latest.context_before_selector
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
            "intent": _intent_value(qr),
            "intent_confidence": qr.intent_confidence,
            "theme": qr.theme,
            "was_expanded": qr.was_expanded,
            "expanded_acronyms": qr.expanded_acronyms,
            "enriched_query": qr.enriched_query,
            "query_for_retrieval": qr.query_for_retrieval,
            "needs_legal_search": qr.needs_legal_search,
            "needs_legal_search_llm": qr.needs_legal_search_llm,
            "tables_searched": state.stage_refs.get("tables_searched", []),
            "retrieval_scope": state.stage_refs.get("retrieval_scope"),
            "selected_ministry": state.stage_refs.get("selected_ministry"),
            "scoped_table_keys": state.stage_refs.get("scoped_table_keys", []),
            "selector_enabled": self.config.selector.enabled,
            "generator_model": self.config.generation.model,
            "generator_provider": self.config.generation.provider.value,
            "embedding_model": self.config.retrieval.embedding_model.value,
            "retrieved_chunks": state.stage_refs.get("retrieved_chunks", []),
            "aggregated_sections": state.stage_refs.get("aggregated_sections", []),
            "context_items_ref": state.stage_refs.get("context_items_ref", []),
            "chunks_raw": state.stage_refs.get("chunks_raw", []),
            "chunks_before_rerank": state.stage_refs.get("chunks_before_rerank", []),
            "chunks_after_rerank": state.stage_refs.get("chunks_after_rerank", []),
            "context_before_selector": state.stage_refs.get("context_before_selector", []),
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
            "turn_id": state.turn_id,
            "trace_id": state.trace_id,
            "rag_trace_events": list(state.trace_events),
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

    def _record_query_processor_event(
        self,
        *,
        query: str,
        conversation_history: list[Dict[str, str]] | None,
        qr: QueryProcessResult,
        state: _RunState,
        duration_ms: float,
    ) -> None:
        state.trace_events.append(
            make_trace_event(
                stage="query-processor",
                duration_ms=duration_ms,
                status="ok" if qr.should_proceed else "short_circuit",
                input_ref={
                    "query": bounded_preview(query, 1_000),
                    "conversation_history_count": len(conversation_history or []),
                },
                output_ref={
                    "intent": _intent_value(qr),
                    "theme": qr.theme or "",
                    "should_proceed": qr.should_proceed,
                    "processed_query": bounded_preview(qr.processed_query, 1_000),
                    "enriched_query": bounded_preview(qr.enriched_query, 1_000),
                    "query_for_retrieval": bounded_preview(qr.query_for_retrieval, 1_000),
                    "needs_legal_search": qr.needs_legal_search,
                    "needs_legal_search_llm": qr.needs_legal_search_llm,
                    "expanded_acronyms": list(qr.expanded_acronyms or []),
                },
                metrics={"intent_confidence": qr.intent_confidence},
            )
        )

    def _record_generator_event(
        self,
        qr: QueryProcessResult,
        context_items: List[ContextItem],
        answer: str,
        state: _RunState,
        *,
        duration_ms: float,
        status: str = "ok",
    ) -> None:
        state.trace_events.append(
            make_trace_event(
                stage="generator",
                duration_ms=duration_ms,
                status=status,
                input_ref={
                    "query": bounded_preview(qr.query_for_retrieval, 1_000),
                    "context_item_count": len(context_items),
                    "context_section_ids": [str(item.section_id or "") for item in context_items],
                    "model": self.config.generation.model,
                    "provider": self.config.generation.provider.value,
                },
                output_ref={"answer_preview": bounded_preview(answer, 1_000), "sources_count": len(context_items)},
                metrics={"answer_tokens": estimate_tokens(answer)},
            )
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
                        "intent": _intent_value(qr),
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
