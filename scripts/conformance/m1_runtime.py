"""Evaluation-only adapters: run both real engines and verify their local DB writes."""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import psycopg
from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.models.auth import Group, Session
from assistant_rh_api.core.models.chat import ChatInput, RunContext
from assistant_rh_rag_pipeline import create_pipeline
from assistant_rh_rag_pipeline.chat_logger import build_log_row, log_run, log_trace_events
from assistant_rh_rag_pipeline.models import (
    AggregatedSection,
    Chunk,
    ContextItem,
    PipelineResult,
    RetrievedChunk,
    _chunk_log_dict,
    context_item_document_id,
    section_document_id,
)

from scripts.conformance.m0b_values import legacy_sources, plain
from src.ui.chatbot_sources import context_items_to_v1_chunks


@dataclass
class CapturedContext(RunContext):
    """Capture full results for evaluation, independently of bounded operational traces."""

    outputs: dict = field(default_factory=dict)

    async def stage(self, name, operation, *, project, measure=None, attempt=""):
        result = await super().stage(name, operation, project=project, measure=measure, attempt=attempt)
        self.outputs[name, attempt] = result
        return result


def core_eval_result(query, result, run, context):
    attempts = {}
    for (stage, attempt), value in context.outputs.items():
        if not attempt:
            continue
        current = attempts.setdefault(attempt, {"name": attempt})
        if stage == "retriever":
            current["chunks_raw"] = [_chunk_log_dict(RetrievedChunk(**plain(chunk))) for chunk in value.chunks]
            current["retrieved_chunks"] = [{"chunk_id": chunk.chunk_id, "section_id": chunk.section_id} for chunk in value.chunks]
        elif stage == "section-aggregator":
            sections = [
                AggregatedSection(**{**plain(section), "chunks": [RetrievedChunk(**plain(chunk)) for chunk in section.chunks]})
                for section in value.sections
            ]
            current["aggregated_sections"] = [{"section_id": section.section_id, "document_id": section_document_id(section)} for section in sections]
            current["chunks_before_rerank"] = plain(value.diagnostics.chunks_before_rerank)
            current["chunks_after_rerank"] = plain(value.diagnostics.chunks_after_rerank)
        elif stage == "context-selector":
            current["selector"] = {"decisions": plain(value.diagnostics.decisions), "status": value.diagnostics.status}
        elif stage == "context-builder":
            current["context_items_ref"] = [
                {"section_id": item.section_id, "doc_id": context_item_document_id(ContextItem(**plain(item)))} for item in value.items
            ]
    metadata = {**plain(run.diagnostics), **next(reversed(attempts.values()), {}), "retrieval_attempts": list(attempts.values())}
    generation = context.outputs.get(("generator", ""))
    outcome = generation.diagnostics.outcome if generation else None
    metadata.update(
        {
            "turn_id": run.turn_id,
            "trace_id": run.trace_id,
            "runtime": "core",
            "selected_ministry": run.selected_ministry,
            "generator_provider_used": outcome.provider if outcome else None,
            "generator_model_used": outcome.model if outcome else None,
            "generator_used_fallback": bool(generation and generation.diagnostics.fallback_count),
        }
    )
    timing = {}
    for event in run.events:
        timing[event.stage + "_ms"] = timing.get(event.stage + "_ms", 0) + event.duration_ms
    return PipelineResult(
        query=query,
        answer=result.answer,
        context_items=[ContextItem(**plain(item)) for item in result.items],
        sources=legacy_sources(result.items),
        timing=timing,
        metadata=metadata,
    )


class CoreEvaluator:
    def __init__(self, service, loop, label):
        self.service, self.loop, self.label = service, loop, label
        self.last_full_prompt = self.last_system_prompt = ""

    def run_with_trace(self, query, *, retrieval_scope):
        future = asyncio.run_coroutine_threadsafe(self.run(query, retrieval_scope.selected_ministry), self.loop)
        try:
            return future.result(timeout=300)
        except TimeoutError:
            future.cancel()
            raise

    async def run(self, query, ministry):
        base = self.service.new_context()
        created = base.created
        auth = AuthContext(
            Group("m1-core-" + ministry, "M1", 0, False, False, None, (ministry,), ministry),
            Session("0" * 64, "m1-core-" + ministry, created, created + timedelta(hours=8), "local-eval"),
        )
        context = CapturedContext(
            turn_id=base.turn_id,
            trace_id=base.trace_id,
            created=created,
            today=base.today,
            clock=base.clock,
        )
        run, result = await self.service.complete(
            ChatInput("assistant-rh-" + ministry, query, conversation_id=self.label),
            auth,
            context=context,
        )
        generation = context.outputs.get(("generator", ""))
        if generation and generation.diagnostics.request:
            self.last_system_prompt = generation.diagnostics.request.messages[0].content
            self.last_full_prompt = generation.diagnostics.request.messages[-1].content
        return core_eval_result(query, result, run, context)


class LegacyEvaluator:
    def __init__(self, config, runtime_config, dsn, engine, label):
        self.pipe = create_pipeline(config=config, dsn=dsn)
        self.config, self.runtime_config, self.dsn, self.engine, self.label = config, runtime_config, dsn, engine, label

    @property
    def last_full_prompt(self):
        return self.pipe.last_full_prompt

    @property
    def last_system_prompt(self):
        return self.pipe.last_system_prompt

    def run_with_trace(self, query, *, retrieval_scope):
        turn_id, trace_id = uuid4().hex[:8], uuid4().hex
        started = time.monotonic()
        result = self.pipe.run_with_trace(query, retrieval_scope=retrieval_scope, turn_id=turn_id, trace_id=trace_id)
        row = build_log_row(
            turn_id=turn_id,
            query=query,
            response=result.answer,
            pipeline=self.pipe,
            qr=self.pipe.last_query_result,
            config=self.config,
            runtime_config=self.runtime_config,
            session_state={"user_group": "m1-legacy-" + retrieval_scope.selected_ministry, "conversation_id": self.label},
            total_time_ms=(time.monotonic() - started) * 1000,
            context_items=result.context_items,
            v1_chunks_for_display=context_items_to_v1_chunks(result.context_items, Chunk),
            legal_refs_v3=[],
            trace_id=trace_id,
        )
        log_run(row, engine=self.engine)
        events = result.metadata.get("rag_trace_events", [])
        log_trace_events(events, turn_id=turn_id, trace_id=trace_id, engine=self.engine, env_label="local")
        # Legacy logging is best effort; evaluation must detect a silent write failure.
        with psycopg.connect(self.dsn) as connection:
            saved = connection.execute("SELECT answer FROM chat_runs WHERE turn_id=%s", (turn_id,)).fetchone()
            traces = connection.execute("SELECT count(*) FROM rag_trace_events WHERE turn_id=%s", (turn_id,)).fetchone()[0]
        if saved != (result.answer,) or traces != len(events):
            raise RuntimeError("legacy run/trace persistence incomplete")
        result.metadata.update({"turn_id": turn_id, "trace_id": trace_id, "runtime": "legacy"})
        return result
