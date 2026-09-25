"""Authorized chat execution and atomic finalization before non-stream success."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timezone
from typing import Literal
from zoneinfo import ZoneInfo

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.errors import ApplicationError, DatabaseUnavailable, InferenceFailure
from assistant_rh_api.core.models.chat import Cancellation, CancellationPort, ChatInput, EventSinkPort, PipelineResult, RunContext
from assistant_rh_api.core.models.conversations import ChatRun, RunSource
from assistant_rh_api.core.models.rag_configuration import RAGConfig
from assistant_rh_api.core.pipeline.pipeline import Pipeline
from assistant_rh_api.core.pipeline.trace_projection import configuration_trace
from assistant_rh_api.core.ports.conversations import ChatRunStorePort
from assistant_rh_api.core.ports.system import ClockPort, IdGeneratorPort
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.core.sources import final_sources, with_sources
from assistant_rh_api.core.trace_values import attempt_trace, trace_payload

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        configurations: RAGConfigurationService,
        pipeline_factory: Callable[[RAGConfig], Pipeline],
        runs: ChatRunStorePort,
        clock: ClockPort,
        ids: IdGeneratorPort,
        models: ModelService | None = None,
    ) -> None:
        self._configurations = configurations
        self._pipeline_factory = pipeline_factory
        self._runs = runs
        self._clock = clock
        self._ids = ids
        self._models = models or ModelService()

    async def complete(
        self, request: ChatInput, auth: AuthContext, *, sink: EventSinkPort | None = None, cancellation: CancellationPort | None = None
    ) -> tuple[ChatRun, PipelineResult]:
        model = self._models.resolve(request.model, auth.group)
        created = self._clock.now().astimezone(timezone.utc)
        context = RunContext(
            turn_id=self._ids.new_id(),
            trace_id=self._ids.new_id(),
            created=created,
            today=created.astimezone(ZoneInfo("Europe/Paris")).date().isoformat(),
            clock=self._clock,
            cancellation=cancellation or Cancellation(),
            sink=sink,
        )

        def record(status: Literal["completed", "failed", "cancelled"], answer: str = "", sources: tuple[RunSource, ...] = ()) -> ChatRun:
            return ChatRun(
                turn_id=context.turn_id,
                trace_id=context.trace_id,
                timestamp=created,
                group_slug=auth.group.slug,
                session_hash=auth.audit_session_hash,
                conversation_id=request.conversation_id,
                question=request.question,
                answer=answer,
                selected_ministry=model.ministry,
                model=model.id,
                sources=sources,
                events=tuple(context.events),
                diagnostics=trace_payload(context.diagnostics),
                status=status,
            )

        try:
            configuration = await context.stage("configuration", self._configurations.load, project=configuration_trace)
            context.diagnostics["configuration_revision"] = configuration.config.revision
            context.diagnostics["configuration_fallback"] = configuration.fallback
            pipeline = self._pipeline_factory(configuration.config.value)
            result = await pipeline.run(request, model.ministry, context)
            sources = final_sources(result.items)
            run = record("completed", with_sources(result.answer, sources), sources)
            context.cancellation.checkpoint()
            await self._runs.finalize(run)
            return run, result
        except asyncio.CancelledError:
            # A cancelled run never grants access to candidate documents.
            context.diagnostics["partial"] = bool(context.partial_answer)
            await self._runs.finalize(record("cancelled", context.partial_answer))
            raise
        except Exception as exc:
            context.diagnostics["error"] = "execution_failed"
            if isinstance(exc, InferenceFailure):
                context.diagnostics["inference_attempts"] = attempt_trace(exc.attempts)
                context.diagnostics["partial"] = exc.partial
            try:
                await self._runs.finalize(record("failed", context.partial_answer))
            except Exception as finalization_error:
                logger.error("Chat finalization failed (turn_id=%s)", context.turn_id)
                if isinstance(finalization_error, DatabaseUnavailable):
                    raise DatabaseUnavailable() from None
                raise ApplicationError() from None
            logger.error("Chat execution failed (turn_id=%s)", context.turn_id)
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationError() from None
