import asyncio
from dataclasses import FrozenInstanceError, fields
from unittest.mock import AsyncMock

import pytest
from assistant_rh_api.core.errors import DatabaseFailure, DatabaseUnavailable, RAGConfigurationError
from assistant_rh_api.core.models.configuration import Snapshot
from assistant_rh_api.core.models.rag_configuration import RAGConfig, RetrievalConfig
from assistant_rh_api.core.rag_configuration import RAGConfigurationService, from_runtime_values
from assistant_rh_api.db.revisions import freeze_json
from assistant_rh_rag_pipeline.admin import RuntimeRAGConfig, runtime_config_to_rag_config
from assistant_rh_rag_pipeline.config import RAGConfig as LegacyRAGConfig


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"v3_tables": []},
        {"v3_tables": None},
        {"v3_context_mode": "narrow", "v3_search_mode": "unknown"},
        {
            "v3_tables": ["mi", "dgafp", "mi"],
            "v3_context_mode": "wide",
            "v3_token_budget": 9500,
            "v3_doc_entire_threshold": 4000,
            "v3_triangulation_sections": 3,
            "v3_enable_selector": False,
            "v3_selector_model": "custom-selector",
            "v3_selector_prompt_name": "custom-selector.md",
            "enable_intent_gating": True,
            "enable_query_expansion": False,
            "v3_intent_prompt_name": "custom-intent.md",
            "enable_hyde": True,
            "v3_initial_top_k": 80,
            "v3_alpha": 0,
            "v3_search_mode": "lexical",
            "v3_enable_reranker": False,
            "v3_rerank_top_k": 25,
            "v3_rerank_input_k": 50,
            "v3_generator_model": "custom-generator",
            "v3_temperature": 0.7,
            "v3_system_prompt_name": "custom-generator.md",
            "verbose_mode": True,
            # These are deliberately not consumed by the historical mapping.
            "embedding_model": "bge_scaleway",
            "llm_provider": "scaleway",
            "unknown_future_key": {"nested": [1]},
        },
    ],
)
def test_exact_historical_mapping(values):
    expected = runtime_config_to_rag_config(RuntimeRAGConfig.from_dict(values))
    actual = from_runtime_values(freeze_json(values))
    assert actual.to_dict() == expected.to_dict()
    for method in ("get_token_budget", "get_max_full_docs", "get_doc_entire_threshold", "get_max_sections", "get_legal_refs_budget"):
        assert getattr(actual.context, method)() == getattr(expected.context, method)()


def test_bare_defaults_are_distinct_from_admin_defaults_and_match_legacy():
    assert RAGConfig().to_dict() == LegacyRAGConfig().to_dict()
    assert RAGConfig().selector.enabled is False
    assert from_runtime_values({}).selector.enabled is True
    assert RAGConfig().query_processor.enable_intent_gating is True
    assert from_runtime_values({}).query_processor.enable_intent_gating is False


def test_nested_configuration_is_immutable_and_serialization_is_detached():
    tables = ["mi", "dgafp"]
    config = RAGConfig(retrieval=RetrievalConfig(tables=tables))
    tables.append("matte")
    assert config.retrieval.tables == ("mi", "dgafp")
    with pytest.raises(FrozenInstanceError):
        config.verbose = True
    for section in ("retrieval", "aggregation", "context", "selector", "generation", "query_processor"):
        nested = getattr(config, section)
        first = fields(nested)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(nested, first, None)
    serialized = config.to_dict()
    serialized["retrieval"]["tables"].append("matte")
    serialized["generation"]["model"] = "changed"
    assert config.retrieval.tables == ("mi", "dgafp")
    assert config.generation.model == "openweight-large"


@pytest.mark.parametrize("values", [{"v3_temperature": "secret"}, {"v3_enable_selector": 1}, {"v3_tables": ("mi", 42)}])
def test_malformed_settings_are_not_silently_defaulted_or_disclosed(values):
    with pytest.raises(RAGConfigurationError, match="^rag_configuration_error$"):
        from_runtime_values(values)


@pytest.mark.anyio
async def test_next_request_refreshes_while_inflight_snapshot_is_stable():
    store = AsyncMock()
    store.load.side_effect = [
        Snapshot(freeze_json({"v3_generator_model": "before"}), "r1", "database"),
        Snapshot(freeze_json({"v3_generator_model": "after"}), "r2", "database"),
    ]
    service = RAGConfigurationService(store)
    first = await service.load()
    second = await service.load()
    assert first.config.value.generation.model == "before"
    assert second.config.value.generation.model == "after"
    assert first.config.revision == "r1" and second.config.revision == "r2"
    assert store.load.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,reason", [(None, "missing"), (DatabaseUnavailable(), "database_unavailable"), (DatabaseFailure(), "database_failure")]
)
async def test_default_fallback_is_fresh_at_each_call_and_recovers(failure, reason):
    store = AsyncMock()
    store.load.side_effect = [failure, Snapshot(freeze_json({"v3_token_budget": 9999}), "recovered", "database")]
    service = RAGConfigurationService(store)
    first = await service.load()
    second = await service.load()
    assert first.fallback == reason and first.config.origin == "default"
    assert first.config.value.to_dict() == runtime_config_to_rag_config(RuntimeRAGConfig()).to_dict()
    assert second.config.value.context.token_budget == 9999
    assert second.fallback is None


@pytest.mark.anyio
async def test_cancellation_is_not_replaced_with_defaults():
    store = AsyncMock()
    store.load.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await RAGConfigurationService(store).load()
