"""Authorized chat execution and atomic finalization before any success terminal."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timezone
from typing import Literal
from zoneinfo import ZoneInfo

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.errors import ApplicationError, InferenceFailure
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
        *,
        finalization_timeout: float = 10,
    ) -> None:
        self._configurations = configurations
        self._pipeline_factory = pipeline_factory
        self._runs = runs
        self._clock = clock
        self._ids = ids
        self._models = models or ModelService()
        self._finalization_timeout = finalization_timeout

    def new_context(self, *, sink: EventSinkPort | None = None, cancellation: CancellationPort | None = None) -> RunContext:
        created = self._clock.now().astimezone(timezone.utc)
        return RunContext(
            turn_id=self._ids.new_id(),
            trace_id=self._ids.new_id(),
            created=created,
            today=created.astimezone(ZoneInfo("Europe/Paris")).date().isoformat(),
            clock=self._clock,
            cancellation=cancellation or Cancellation(),
            sink=sink,
        )

    async def _persist(self, run: ChatRun) -> None:
        # Once finalization starts, finish that transaction even on disconnect.
        # A committed completed run must never be replaced with a cancelled run.
        async def persist() -> None:
            async with asyncio.timeout(self._finalization_timeout):
                await self._runs.finalize(run)

        saving = asyncio.create_task(persist(), name="chat-finalize-" + run.turn_id)
        while not saving.done():
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                continue
        saving.result()

    async def complete(
        self,
        request: ChatInput,
        auth: AuthContext,
        *,
        sink: EventSinkPort | None = None,
        cancellation: CancellationPort | None = None,
        context: RunContext | None = None,
        stream: bool = False,
    ) -> tuple[ChatRun, PipelineResult]:
        model = self._models.resolve(request.model, auth.group)
        context = context or self.new_context(sink=sink, cancellation=cancellation)
        created = context.created

        def record(status: Literal["completed", "failed", "cancelled"], answer: str = "", sources: tuple[RunSource, ...] = ()) -> ChatRun:
            return ChatRun(
                turn_id=context.turn_id,
                trace_id=context.trace_id,
                timestamp=created,
                group_slug=auth.group.slug,
                session_hash=auth.session.token_hash,
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
            result = await pipeline.run(request, model.ministry, context, stream=stream)
            sources = final_sources(result.items)
            run = record("completed", with_sources(result.answer, sources), sources)
            context.cancellation.checkpoint()
            await self._persist(run)
            return run, result
        except asyncio.CancelledError:
            # A cancelled run never grants access to candidate documents.
            context.diagnostics["partial"] = bool(context.partial_answer)
            context.diagnostics.setdefault("cancellation", "requested")
            try:
                await self._persist(record("cancelled", context.partial_answer))
            except Exception:
                logger.error("Chat cancellation finalization failed (turn_id=%s)", context.turn_id)
                raise ApplicationError() from None
            raise
        except Exception as exc:
            context.diagnostics["error"] = "execution_failed"
            context.diagnostics["partial"] = bool(context.partial_answer)
            if isinstance(exc, InferenceFailure):
                context.diagnostics["inference_attempts"] = attempt_trace(exc.attempts)
                context.diagnostics["partial"] = exc.partial
            try:
                await self._persist(record("failed", context.partial_answer))
            except Exception:
                logger.error("Chat finalization failed (turn_id=%s)", context.turn_id)
                raise ApplicationError() from None
            logger.error("Chat execution failed (turn_id=%s)", context.turn_id)
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationError() from None
