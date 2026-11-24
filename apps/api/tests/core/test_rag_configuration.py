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


def _expected_dict_with_api_extensions(values: dict) -> dict:
    expected = runtime_config_to_rag_config(RuntimeRAGConfig.from_dict(values)).to_dict()
    expected["retrieval"]["ivfflat_probes"] = values.get("v3_ivfflat_probes", 5)
    expected["retrieval"]["enable_selector_retry"] = values.get("v3_enable_selector_retry", True)
    expected["retrieval"]["selector_retry_search_mode"] = (
        values.get("v3_selector_retry_search_mode", "hybrid")
        if values.get("v3_selector_retry_search_mode") in ("semantic", "hybrid", "lexical")
        else "hybrid"
    )
    expected["retrieval"]["selector_retry_top_k"] = values.get("v3_selector_retry_top_k", 30)
    expected["aggregation"]["rerank_input_k"] = values.get("v3_rerank_input_k", 40)
    expected["aggregation"]["per_publisher_rerank_quota"] = values.get("v3_per_publisher_rerank_quota", True)
    expected["context"]["max_full_docs"] = values.get("v3_max_full_docs", 1)
    expected["context"]["max_sections"] = values.get("v3_max_sections", 12)
    expected["context"]["triangulation_min_score"] = values.get("v3_triangulation_min_score", 0.05)
    expected["context"]["legal_refs_budget"] = values.get("v3_legal_refs_budget", 1000)
    expected["context"]["token_budget_wide"] = values.get("v3_token_budget_wide", 12000)
    expected["context"]["max_full_docs_wide"] = values.get("v3_max_full_docs_wide", 2)
    expected["context"]["doc_entire_threshold_wide"] = values.get("v3_doc_entire_threshold_wide", 9000)
    expected["context"]["max_sections_wide"] = values.get("v3_max_sections_wide", 20)
    expected["context"]["legal_refs_budget_wide"] = values.get("v3_legal_refs_budget_wide", 2000)
    expected["selector"]["min_kept_sections"] = values.get("v3_min_kept_sections", 0)
    expected["selector"]["reranker_union_top_k"] = values.get("v3_reranker_union_top_k", 2)
    expected["selector"]["reranker_union_min_score"] = values.get("v3_reranker_union_min_score", 0.5)
    expected["query_processor"]["intent_model"] = values.get("v3_intent_model", "openweight-medium")
    expected["query_processor"]["intent_confidence_threshold"] = values.get(
        "v3_intent_confidence_threshold", values.get("intent_confidence_threshold", 0.7)
    )
    expected["query_processor"]["max_history_message_chars"] = values.get("v3_max_history_message_chars", 800)
    return expected


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
            "v3_token_budget_wide": 15000,
            "v3_doc_entire_threshold": 4000,
            "v3_doc_entire_threshold_wide": 8500,
            "v3_max_sections": 10,
            "v3_max_sections_wide": 18,
            "v3_max_full_docs": 2,
            "v3_max_full_docs_wide": 3,
            "v3_legal_refs_budget": 1200,
            "v3_legal_refs_budget_wide": 2400,
            "v3_triangulation_sections": 3,
            "v3_triangulation_min_score": 0.12,
            "v3_enable_selector": False,
            "v3_selector_model": "custom-selector",
            "v3_selector_prompt_name": "custom-selector.md",
            "v3_min_kept_sections": 4,
            "v3_reranker_union_top_k": 3,
            "v3_reranker_union_min_score": 0.6,
            "enable_intent_gating": True,
            "enable_query_expansion": False,
            "v3_intent_model": "custom-intent-model",
            "v3_intent_prompt_name": "custom-intent.md",
            "intent_confidence_threshold": 0.8,
            "v3_max_history_message_chars": 1000,
            "enable_hyde": True,
            "v3_initial_top_k": 80,
            "v3_ivfflat_probes": 12,
            "v3_alpha": 0,
            "v3_search_mode": "lexical",
            "v3_enable_reranker": False,
            "v3_rerank_top_k": 25,
            "v3_rerank_input_k": 50,
            "v3_per_publisher_rerank_quota": False,
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
    expected_dict = _expected_dict_with_api_extensions(values)
    actual = from_runtime_values(freeze_json(values))
    assert actual.to_dict() == expected_dict
    assert actual.context.get_token_budget() == (
        expected_dict["context"]["token_budget_wide"]
        if expected_dict["context"]["context_mode"] == "wide"
        else expected_dict["context"]["token_budget"]
    )
    assert actual.context.get_max_sections() == (
        expected_dict["context"]["max_sections_wide"]
        if expected_dict["context"]["context_mode"] == "wide"
        else expected_dict["context"]["max_sections"]
    )


def test_bare_defaults_are_distinct_from_admin_defaults_and_match_legacy():
    legacy = LegacyRAGConfig().to_dict()
    legacy["aggregation"]["rerank_input_k"] = 40
    legacy["aggregation"]["per_publisher_rerank_quota"] = True
    legacy["context"]["triangulation_min_score"] = 0.05
    legacy["selector"]["reranker_union_top_k"] = 2
    legacy["selector"]["reranker_union_min_score"] = 0.5
    legacy["query_processor"]["intent_confidence_threshold"] = 0.7
    legacy["query_processor"]["max_history_message_chars"] = 800
    assert RAGConfig().to_dict() == legacy
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


@pytest.mark.parametrize("tables", [False, 0, "", {}])
def test_falsy_malformed_tables_are_rejected_before_defaulting(tables):
    with pytest.raises(RAGConfigurationError, match="^rag_configuration_error$"):
        from_runtime_values(freeze_json({"v3_tables": tables}))


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
    assert first.config.value.to_dict() == _expected_dict_with_api_extensions({})
    assert second.config.value.context.token_budget == 9999
    assert second.fallback is None


@pytest.mark.anyio
async def test_cancellation_is_not_replaced_with_defaults():
    store = AsyncMock()
    store.load.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await RAGConfigurationService(store).load()
