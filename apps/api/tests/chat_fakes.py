"""Synthetic ports composing every real C2–C6 component, without provider/DB I/O."""

import asyncio
import json
from contextlib import asynccontextmanager

from assistant_rh_api.core.chat import ChatService
from assistant_rh_api.core.models.configuration import Prompt, Snapshot
from assistant_rh_api.core.models.inference import Attempt, Completion, Embedding, RankedDocument, Reranking, StreamCompleted, TextDelta, TokenUsage
from assistant_rh_api.core.models.retrieval import RawChunk
from assistant_rh_api.core.pipeline.pipeline import Pipeline
from assistant_rh_api.core.pipeline.steps.aggregation import SectionAggregator
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
from assistant_rh_api.core.pipeline.steps.generator import Generator
from assistant_rh_api.core.pipeline.steps.query_processor import QueryProcessor
from assistant_rh_api.core.pipeline.steps.retrieval import Retriever
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.search_catalog import search_catalog
from assistant_rh_api.gateways.ids import RunIds

from apps.api.tests.auth_fakes import Clock


class Config:
    def __init__(self, **values):
        self.values = {"enable_intent_gating": True, "v3_enable_selector": True, **values}
        self.calls = 0

    async def load(self):
        self.calls += 1
        return Snapshot(self.values, "configuration-revision", "db")


class Prompts:
    async def get(self, name):
        if "intent" in name:
            text = "INTENT {ministere_label} {today}\n{history}\n{query}\n{acronyms_section}"
        elif "selector" in name:
            text = "SELECT {ministere_label} {today}\n{query}\n{context}"
        else:
            text = "GENERATE {ministere_label} {today}"
        return Snapshot(Prompt(name, text), name, "db")


class Acronyms:
    async def load(self):
        return Snapshot((), "empty-acronyms", "db")


class LLM:
    def __init__(self):
        self.calls = []
        self.intent = "rag_query"
        self.selector_responses = []
        self.failure = None
        self.entered = None
        self.release = None

    async def complete(self, request):
        self.calls.append(request)
        await asyncio.sleep(0)  # Force concurrent requests to interleave.
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.failure:
            raise self.failure
        first = request.messages[0].content
        if first.startswith("INTENT"):
            text = json.dumps({"intent": self.intent, "confidence": 0.9})
        elif first.startswith("SELECT"):
            text = self.selector_responses.pop(0) if self.selector_responses else '{"selected_ids":[0],"reason":"synthetic"}'
        else:
            ministry = first.split()[1]
            text = f"Réponse {ministry} selon le document fourni."
        return Completion(text, "albert", "synthetic", (Attempt("albert", "synthetic"),), "stop", TokenUsage(10, 4, 14))

    @asynccontextmanager
    async def stream(self, request):
        result = await self.complete(request)

        async def events():
            for start in range(0, len(result.text), 8):
                yield TextDelta(result.text[start : start + 8])
            yield StreamCompleted(result.provider, result.model, result.attempts, result.finish_reason, result.usage)

        yield events()


class Search:
    def __init__(self):
        self.calls = []
        self.empty = False

    async def search(self, request):
        self.calls.append(request)
        await asyncio.sleep(0)
        if self.empty or request.source not in ("matte", "mi", "mso", "masa") or request.mode == "heading":
            return ()
        return (
            RawChunk(
                request.source,
                request.source + "-chunk",
                f"Document interne {request.source.upper()}",
                None,
                0.8,
                1,
                {
                    "source_name": "Guide " + request.source,
                    "source_document_id": "guide-" + request.source,
                    "url": "https://storage.invalid/doc?X-Amz-Signature=SECRET",
                },
            ),
        )

    async def hybrid_candidates(self, request):
        return await self.search(request), ()


class Embeddings:
    async def embed(self, text):
        return Embedding((1.0,), "albert", "albert", ())


class Content:
    async def sections(self, ids):
        return ()

    async def documents(self, ids):
        return ()

    async def references(self, numbers):
        return ()


class Reranker:
    async def rerank(self, query, documents, *, top_k=None):
        return Reranking(tuple(RankedDocument(i, 0.9) for i in range(min(len(documents), top_k or len(documents)))), ())


class Runs:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.failure = None
        self.entered = None
        self.release = None

    async def finalize(self, run):
        self.calls.append(run)
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.failure:
            raise self.failure
        assert run.turn_id not in self.rows
        self.rows[run.turn_id] = run


class Runtime:
    def __init__(self, runs=None, **config):
        self.config = Config(**config)
        self.llm = LLM()
        self.search = Search()
        self.runs = runs or Runs()
        self.clock = Clock()
        self.pipelines = []

        def factory(settings):
            prompts = Prompts()
            content = Content()
            pipeline = Pipeline(
                settings,
                QueryProcessor(settings.query_processor, Acronyms(), prompts, prompts, self.llm),
                Retriever(self.search, {"albert": Embeddings()}, tuple(t.source for t in search_catalog())),
                SectionAggregator(settings.aggregation, content, Reranker()),
                ContextSelector(settings.selector, prompts, prompts, self.llm),
                ContextBuilder(settings.context, content),
                Generator(settings.generation, prompts, prompts, self.llm),
            )
            self.pipelines.append(pipeline)
            return pipeline

        self.service = ChatService(RAGConfigurationService(self.config), factory, self.runs, self.clock, RunIds())
