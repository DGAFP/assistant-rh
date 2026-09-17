"""Compose the real C2–C5 stages without retaining request data on the pipeline."""

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.chat import ChatInput, PipelineResult, RunContext
from assistant_rh_api.core.models.context import ContextBuildDiagnostics, ContextBuildResult
from assistant_rh_api.core.models.generation import GenerationResult
from assistant_rh_api.core.models.inference import TextDelta
from assistant_rh_api.core.models.rag_configuration import RAGConfig, SearchMode
from assistant_rh_api.core.pipeline.steps.aggregation import SectionAggregator
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
from assistant_rh_api.core.pipeline.steps.generator import Generator
from assistant_rh_api.core.pipeline.steps.query_processor import QueryProcessor
from assistant_rh_api.core.pipeline.steps.retrieval import RetrievalResult, Retriever
from assistant_rh_api.core.pipeline.trace_projection import (
    aggregation_trace,
    context_trace,
    generation_trace,
    query_trace,
    retrieval_trace,
    selection_trace,
)


class Pipeline:
    def __init__(
        self,
        config: RAGConfig,
        query: QueryProcessor,
        retriever: Retriever,
        aggregator: SectionAggregator,
        selector: ContextSelector,
        builder: ContextBuilder,
        generator: Generator,
    ) -> None:
        self._config = config
        self._query = query
        self._retriever = retriever
        self._aggregator = aggregator
        self._selector = selector
        self._builder = builder
        self._generator = generator

    async def run(self, request: ChatInput, ministry: str, context: RunContext, *, stream: bool = False) -> PipelineResult:
        history = tuple({"role": message.role, "content": message.content} for message in request.history)
        processing = await context.stage(
            "query-processor",
            lambda: self._query.process(request.question, history, ministry, today=context.today),
            project=query_trace,
        )
        query = processing.result
        if not query.should_proceed:
            if stream:
                await context.delta(query.direct_response or "")
            return PipelineResult(answer=query.direct_response or "")

        config = self._config.retrieval
        built, rejected = await self._retrieve_and_build(
            query.query_for_retrieval,
            ministry,
            context,
            attempt="initial",
            search_mode=config.search_mode,
            top_k=config.initial_top_k,
        )
        retry = not built.items and rejected and config.enable_selector_retry
        context.diagnostics["selector_retry_triggered"] = retry
        if retry:
            built, rejected = await self._retrieve_and_build(
                query.query_for_retrieval,
                ministry,
                context,
                attempt="selector_retry",
                search_mode=config.selector_retry_search_mode,
                top_k=config.selector_retry_top_k,
            )
            # A retry with no candidates preserves the initial explicit rejection.
            rejected = rejected or not built.items
            context.diagnostics["selector_retry_succeeded"] = bool(built.items) and not rejected
        context.diagnostics["selector_all_rejected"] = rejected
        async def generate() -> GenerationResult:
            if not stream:
                return await self._generator.generate(query.query_for_retrieval, built.items, ministry, today=context.today, all_rejected=rejected)
            # C1 keeps API generation inputs identical across transports.
            # C6 passes history only to the query processor.
            async with self._generator.stream(
                query.query_for_retrieval, built.items, ministry=ministry, today=context.today, all_rejected=rejected
            ) as events:
                async for event in events:
                    context.cancellation.checkpoint()
                    if isinstance(event, TextDelta):
                        await context.delta(event.text)
                    else:
                        return event
            raise InferenceFailure((), partial=bool(context.partial_answer))

        generated = await context.stage("generator", generate, project=generation_trace)
        outcome = generated.diagnostics.outcome
        if outcome is not None and outcome.usage is not None:
            return PipelineResult(answer=generated.answer, items=built.items, usage=outcome.usage)
        return PipelineResult(answer=generated.answer, items=built.items)

    async def _retrieve_and_build(
        self, query: str, ministry: str, context: RunContext, *, attempt: str, search_mode: SearchMode, top_k: int
    ) -> tuple[ContextBuildResult, bool]:
        retrieved = await context.stage(
            "retriever",
            lambda: self._retrieve(query, ministry, context, search_mode=search_mode, top_k=top_k),
            project=retrieval_trace,
            attempt=attempt,
        )
        aggregated = await context.stage(
            "section-aggregator",
            lambda: self._aggregator.aggregate_with_diagnostics(retrieved.chunks, query=query),
            project=aggregation_trace,
            attempt=attempt,
        )
        selected = await context.stage(
            "context-selector",
            lambda: self._selector.select(query, aggregated.sections, ministry, today=context.today),
            project=selection_trace,
            attempt=attempt,
        )
        if selected.all_rejected and not selected.sections:
            return ContextBuildResult(items=(), resolved_refs={}, diagnostics=ContextBuildDiagnostics()), True
        built = await context.stage("context-builder", lambda: self._builder.build(selected.sections), project=context_trace, attempt=attempt)
        return built, selected.all_rejected

    async def _retrieve(self, query: str, ministry: str, context: RunContext, *, search_mode: SearchMode, top_k: int) -> RetrievalResult:
        retrieved = await self._retriever.retrieve(query, self._config.retrieval, selected_ministry=ministry, search_mode=search_mode, top_k=top_k)
        if retrieved.embedding_failed:
            # A provider outage must fail the run, not generate an answer without context.
            context.diagnostics["embedding_failed"] = True
            raise InferenceFailure(retrieved.embedding_attempts)
        return retrieved
