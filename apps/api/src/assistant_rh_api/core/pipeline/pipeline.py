"""Compose the real C2–C5 stages without retaining request data on the pipeline."""

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.chat import ChatInput, PipelineResult, RunContext
from assistant_rh_api.core.models.context import ContextBuildDiagnostics, ContextBuildResult
from assistant_rh_api.core.models.rag_configuration import RAGConfig
from assistant_rh_api.core.pipeline.steps.aggregation import SectionAggregator
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
from assistant_rh_api.core.pipeline.steps.generator import Generator
from assistant_rh_api.core.pipeline.steps.query_processor import QueryProcessor
from assistant_rh_api.core.pipeline.steps.retrieval import RetrievalResult, Retriever


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

    async def run(self, request: ChatInput, ministry: str, context: RunContext) -> PipelineResult:
        history = tuple({"role": message.role, "content": message.content} for message in request.history)
        processing = await context.stage("query-processor", lambda: self._query.process(request.question, history, ministry, today=context.today))
        query = processing.result
        if not query.should_proceed:
            return PipelineResult(query.direct_response or "")

        async def retrieve_attempt(name: str) -> tuple[ContextBuildResult, bool]:
            retry = name == "selector_retry"
            config = self._config.retrieval

            async def retrieve() -> RetrievalResult:
                retrieved = await self._retriever.retrieve(
                    query.query_for_retrieval,
                    config,
                    selected_ministry=ministry,
                    search_mode=config.selector_retry_search_mode if retry else config.search_mode,
                    top_k=config.selector_retry_top_k if retry else config.initial_top_k,
                )
                if retrieved.embedding_failed:
                    # C3 preserves legacy empty-result signaling. C1 must fail a
                    # technical outage before generating any successful answer.
                    context.diagnostics["embedding_failed"] = True
                    raise InferenceFailure(())
                return retrieved

            retrieved = await context.stage("retriever", retrieve, attempt=name)
            aggregated = await context.stage(
                "section-aggregator",
                lambda: self._aggregator.aggregate_with_diagnostics(retrieved.chunks, query=query.query_for_retrieval),
                attempt=name,
            )
            selected = await context.stage(
                "context-selector",
                lambda: self._selector.select(query.query_for_retrieval, aggregated.sections, ministry, today=context.today),
                attempt=name,
            )
            if selected.all_rejected and not selected.sections:
                return ContextBuildResult((), {}, ContextBuildDiagnostics()), True
            built = await context.stage("context-builder", lambda: self._builder.build(selected.sections), attempt=name)
            return built, selected.all_rejected

        built, rejected = await retrieve_attempt("initial")
        retry = not built.items and rejected and self._config.retrieval.enable_selector_retry
        context.diagnostics["selector_retry_triggered"] = retry
        if retry:
            built, rejected = await retrieve_attempt("selector_retry")
            # A retry with no candidates preserves the initial explicit rejection.
            rejected = rejected or not built.items
            context.diagnostics["selector_retry_succeeded"] = bool(built.items) and not rejected
        context.diagnostics["selector_all_rejected"] = rejected
        generated = await context.stage(
            "generator",
            lambda: self._generator.generate(query.query_for_retrieval, built.items, ministry, today=context.today, all_rejected=rejected),
        )
        outcome = generated.diagnostics.outcome
        result = PipelineResult(generated.answer, built.items)
        if outcome is not None and outcome.usage is not None:
            result = PipelineResult(generated.answer, built.items, outcome.usage)
        return result
