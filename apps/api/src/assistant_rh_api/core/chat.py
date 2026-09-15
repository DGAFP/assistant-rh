"""Authorized chat execution and atomic finalization before non-stream success."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timezone
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.errors import ApplicationError, InferenceFailure
from assistant_rh_api.core.models.chat import Cancellation, CancellationPort, ChatInput, EventSinkPort, PipelineResult, RunContext, evidence
from assistant_rh_api.core.models.context import ContextItem
from assistant_rh_api.core.models.conversations import ChatRun, RunSource
from assistant_rh_api.core.models.rag_configuration import RAGConfig
from assistant_rh_api.core.pipeline.pipeline import Pipeline
from assistant_rh_api.core.ports.conversations import ChatRunStorePort
from assistant_rh_api.core.ports.system import ClockPort, IdGeneratorPort
from assistant_rh_api.core.rag_configuration import RAGConfigurationService

logger = logging.getLogger(__name__)
SOURCES_MARKER = "\n\n---\n**Sources :**\n"


def final_sources(items: tuple[ContextItem, ...]) -> tuple[RunSource, ...]:
    """Only served context grants source authority; internal URLs never escape."""
    sources: list[RunSource] = []
    seen: set[str] = set()
    for item in items:
        metadata = item.metadata
        raw_document_id = str(metadata.get("doc_id") or "")
        try:
            document_id = str(UUID(raw_document_id))
        except ValueError:
            document_id = None
        title = item.document_title or str(metadata.get("full_title") or metadata.get("title") or item.heading or "Document")
        reference = str(document_id or metadata.get("doc_short_id") or raw_document_id or metadata.get("cid") or "")
        if not reference:
            reference = "source-" + sha256(f"{item.publisher}\n{item.section_id}\n{title}\n{item.document_url}".encode()).hexdigest()
        if reference in seen:
            continue
        seen.add(reference)
        raw_url = item.document_url or ""
        try:
            url = urlsplit(raw_url)
            public = (
                url.scheme == "https"
                and url.hostname
                in {
                    "www.service-public.fr",
                    "www.service-public.gouv.fr",
                    "www.legifrance.gouv.fr",
                    "legifrance.gouv.fr",
                }
                and not (url.username or url.password or url.query or url.fragment)
                and url.port in (None, 443)
            )
        except ValueError:
            public = False
        sources.append(
            RunSource(reference, title, raw_url if public else "", document_id, item.publisher or "", "public" if public else "authenticated")
        )
    return tuple(sources)


def source_text(value: str) -> str:
    # Metadata remains plain text even if a document title contains Markdown/HTML.
    value = " ".join(value.split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in "\\`*_{}[]()#!|":
        value = value.replace(character, "\\" + character)
    return value


def with_sources(answer: str, sources: tuple[RunSource, ...]) -> str:
    if not sources:
        return answer
    lines = []
    for index, source in enumerate(sources, 1):
        title = source_text(source.title)
        if source.url:
            safe_url = source.url.replace("(", "%28").replace(")", "%29").replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")
            title = f"[{title}]({safe_url})"
        publisher = f" — {source_text(source.publisher)}" if source.publisher else ""
        lines.append(f"{index}. {title}{publisher}")
    return answer + SOURCES_MARKER + "\n".join(lines)


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
            self._ids.new_id(),
            self._ids.new_id(),
            created,
            created.astimezone(ZoneInfo("Europe/Paris")).date().isoformat(),
            self._clock,
            cancellation or Cancellation(),
            sink,
        )

        def record(status: Literal["completed", "failed", "cancelled"], answer: str = "", sources: tuple[RunSource, ...] = ()) -> ChatRun:
            return ChatRun(
                context.turn_id,
                context.trace_id,
                created,
                auth.group.slug,
                auth.session.token_hash,
                request.conversation_id,
                request.question,
                answer,
                model.ministry,
                model.id,
                sources,
                tuple(context.events),
                evidence(context.diagnostics),
                status,
            )

        try:
            configuration = await context.stage("configuration", self._configurations.load)
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
            # C7 owns disconnect/worker shielding. Cooperative cancellation reaches
            # this boundary after a stage stops, with no candidate source authority.
            context.diagnostics["partial"] = bool(context.partial_answer)
            await self._runs.finalize(record("cancelled", context.partial_answer))
            raise
        except Exception as exc:
            context.diagnostics["error"] = "execution_failed"
            if isinstance(exc, InferenceFailure):
                context.diagnostics["inference_attempts"] = evidence(exc.attempts)
                context.diagnostics["partial"] = exc.partial
            try:
                await self._runs.finalize(record("failed", context.partial_answer))
            except Exception:
                logger.error("Chat finalization failed (turn_id=%s)", context.turn_id)
                raise ApplicationError() from None
            logger.error("Chat execution failed (turn_id=%s)", context.turn_id)
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationError() from None
