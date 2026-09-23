"""Assemble chat services from database and provider adapters."""

from collections.abc import Mapping

import httpx

from assistant_rh_api.core.chat import ChatService
from assistant_rh_api.core.models.rag_configuration import RAGConfig
from assistant_rh_api.core.pipeline.pipeline import Pipeline
from assistant_rh_api.core.pipeline.steps.aggregation import SectionAggregator
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
from assistant_rh_api.core.pipeline.steps.generator import Generator
from assistant_rh_api.core.pipeline.steps.query_processor import QueryProcessor
from assistant_rh_api.core.pipeline.steps.retrieval import Retriever
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.content_store import ContentStore
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.db.search_catalog import search_catalog
from assistant_rh_api.db.search_store import SearchStore
from assistant_rh_api.db.settings_stores import AcronymStore, PromptStore
from assistant_rh_api.gateways.auth import SystemClock
from assistant_rh_api.gateways.chat import ChatGateway
from assistant_rh_api.gateways.embeddings import EmbeddingCircuit, EmbeddingGateway
from assistant_rh_api.gateways.ids import RunIds
from assistant_rh_api.gateways.packaged_prompts import PackagedPromptStore
from assistant_rh_api.gateways.rerank import RerankerGateway
from assistant_rh_api.gateways.settings import Endpoint


def create_chat_service(
    database: Database, configurations: RAGConfigurationService, client: httpx.AsyncClient, environ: Mapping[str, str]
) -> ChatService:
    environment = dict(environ)
    clock = SystemClock()
    circuit = EmbeddingCircuit(clock)
    catalogue = search_catalog()
    search = SearchStore(database, catalogue)
    content = ContentStore(database)
    prompts = PromptStore(database)
    acronyms = AcronymStore(database)
    packaged = PackagedPromptStore()

    def endpoint(provider: str, model: str) -> Endpoint:
        if provider == "albert":
            return Endpoint(
                provider="albert",
                model=model,
                base_url=environment.get("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1"),
                api_key=environment.get("ALBERT_API_KEY", ""),
            )
        if provider == "scaleway":
            return Endpoint(
                provider="scaleway",
                model=model,
                base_url=environment.get("SCALEWAY_BASE_URL", "https://api.scaleway.ai/v1"),
                api_key=environment.get("SCALEWAY_API_KEY", ""),
            )
        raise ValueError("unsupported inference provider")

    def pipeline(config: RAGConfig) -> Pipeline:
        albert_embed = endpoint("albert", environment.get("ALBERT_EMBED_MODEL", "openweight-embeddings"))
        scaleway_embed = endpoint("scaleway", "bge-multilingual-gemma2") if environment.get("SCALEWAY_API_KEY") else None
        embeddings = {
            "albert": EmbeddingGateway(client, albert_embed, scaleway_embed, circuit=circuit),
        }
        if scaleway_embed is not None:
            embeddings["bge_scaleway"] = EmbeddingGateway(client, scaleway_embed)
        intent_llm = ChatGateway(client, endpoint("albert", config.query_processor.intent_model))
        selector_llm = ChatGateway(client, endpoint(config.selector.provider.value, config.selector.model))
        fallback = (
            endpoint(config.generation.fallback_provider.value, config.generation.fallback_model) if environment.get("SCALEWAY_API_KEY") else None
        )
        generator_llm = ChatGateway(client, primary=endpoint(config.generation.provider.value, config.generation.model), fallback=fallback)
        reranker = RerankerGateway(client, endpoint("albert", environment.get("ALBERT_RERANK_MODEL", "openweight-rerank")))
        return Pipeline(
            config=config,
            query=QueryProcessor(config=config.query_processor, acronyms=acronyms, prompts=prompts, packaged_prompts=packaged, llm=intent_llm),
            retriever=Retriever(search=search, embeddings=embeddings, sources=tuple(table.source for table in catalogue)),
            aggregator=SectionAggregator(config=config.aggregation, content_store=content, reranker=reranker),
            selector=ContextSelector(config=config.selector, prompts=prompts, packaged_prompts=packaged, llm=selector_llm),
            builder=ContextBuilder(config=config.context, content_store=content),
            generator=Generator(config=config.generation, prompts=prompts, packaged_prompts=packaged, llm=generator_llm),
        )

    runs = ChatRunStore(database, environment=environment.get("APP_ENV") or environment.get("APP_SCALEWAY_ENV") or "")
    return ChatService(configurations=configurations, pipeline_factory=pipeline, runs=runs, clock=clock, ids=RunIds())
