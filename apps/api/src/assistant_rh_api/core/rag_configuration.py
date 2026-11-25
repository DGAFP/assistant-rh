"""Capture one immutable configuration per request, without a process cache."""

from dataclasses import dataclass
from typing import Literal, cast

from assistant_rh_api.core.errors import DatabaseFailure, DatabaseUnavailable, RAGConfigurationError
from assistant_rh_api.core.models.configuration import ConfigValues, Snapshot
from assistant_rh_api.core.models.rag_configuration import (
    ContextBuildConfig,
    ContextMode,
    GenerationConfig,
    QueryProcessorConfig,
    RAGConfig,
    RetrievalConfig,
    SearchMode,
    SectionAggregationConfig,
    SelectorConfig,
)
from assistant_rh_api.core.ports.configuration import ConfigStorePort


def from_runtime_values(values: ConfigValues) -> RAGConfig:
    """Preserve RuntimeRAGConfig → RAGConfig defaults, including enum fallbacks.

    Only the historical nested mapping is interpreted here. Unknown keys remain
    the responsibility of C2–C5, which still need to extract direct runtime reads.
    No admin range rules are reapplied: existing evaluated values may exceed them.
    Malformed types fail explicitly, without including their values in errors.
    """

    def setting[T: (str, bool, int, float)](key: str, default: T) -> T:
        value = values.get(key, default)
        if type(default) is float:
            valid = type(value) in (int, float)
        else:
            valid = type(value) is type(default)
        if not valid:
            raise RAGConfigurationError()
        return cast(T, value)

    tables = values.get("v3_tables")
    if tables is not None and (not isinstance(tables, tuple) or not all(isinstance(table, str) for table in tables)):
        raise RAGConfigurationError()
    if tables is None or tables == ():
        tables = ("matte", "service_public", "dgafp", "rgrh")
    return RAGConfig(
        retrieval=RetrievalConfig(
            tables=cast(tuple[str, ...], tables),
            initial_top_k=setting("v3_initial_top_k", 30),
            ivfflat_probes=setting("v3_ivfflat_probes", 5),
            alpha=setting("v3_alpha", 0.5),
            search_mode={"semantic": SearchMode.SEMANTIC, "hybrid": SearchMode.HYBRID, "lexical": SearchMode.LEXICAL}.get(
                setting("v3_search_mode", "semantic"), SearchMode.SEMANTIC
            ),
            enable_selector_retry=setting("v3_enable_selector_retry", True),
            selector_retry_search_mode={"semantic": SearchMode.SEMANTIC, "hybrid": SearchMode.HYBRID, "lexical": SearchMode.LEXICAL}.get(
                setting("v3_selector_retry_search_mode", "hybrid"), SearchMode.HYBRID
            ),
            selector_retry_top_k=setting("v3_selector_retry_top_k", 30),
        ),
        aggregation=SectionAggregationConfig(
            enable_section_reranker=setting("v3_enable_reranker", True),
            section_rerank_top_k=setting("v3_rerank_top_k", 20),
            rerank_input_k=setting("v3_rerank_input_k", 40),
            per_publisher_rerank_quota=setting("v3_per_publisher_rerank_quota", True),
        ),
        context=ContextBuildConfig(
            context_mode={"standard": ContextMode.STANDARD, "wide": ContextMode.WIDE}.get(
                setting("v3_context_mode", "standard"), ContextMode.STANDARD
            ),
            token_budget=setting("v3_token_budget", 8000),
            max_full_docs=setting("v3_max_full_docs", 1),
            doc_entire_threshold=setting("v3_doc_entire_threshold", 3500),
            max_sections=setting("v3_max_sections", 12),
            triangulation_sections=setting("v3_triangulation_sections", 2),
            triangulation_min_score=setting("v3_triangulation_min_score", 0.05),
            legal_refs_budget=setting("v3_legal_refs_budget", 1000),
            token_budget_wide=setting("v3_token_budget_wide", 12000),
            max_full_docs_wide=setting("v3_max_full_docs_wide", 2),
            doc_entire_threshold_wide=setting("v3_doc_entire_threshold_wide", 9000),
            max_sections_wide=setting("v3_max_sections_wide", 20),
            legal_refs_budget_wide=setting("v3_legal_refs_budget_wide", 2000),
        ),
        selector=SelectorConfig(
            enabled=setting("v3_enable_selector", True),
            model=setting("v3_selector_model", "openweight-large"),
            prompt_name=setting("v3_selector_prompt_name", "v3_selector_business.md"),
            min_kept_sections=setting("v3_min_kept_sections", 0),
            reranker_union_top_k=setting("v3_reranker_union_top_k", 0),
            reranker_union_min_score=setting("v3_reranker_union_min_score", 0.5),
        ),
        generation=GenerationConfig(
            model=setting("v3_generator_model", "openweight-large"),
            temperature=setting("v3_temperature", 0.0),
            system_prompt_name=setting("v3_system_prompt_name", "system_prompt_V6_optimized.md"),
        ),
        query_processor=QueryProcessorConfig(
            enable_intent_gating=setting("enable_intent_gating", False),
            enable_acronym_expansion=setting("enable_query_expansion", True),
            intent_model=setting("v3_intent_model", "openweight-medium"),
            intent_prompt_name=setting("v3_intent_prompt_name", "intent_unified.md"),
            intent_confidence_threshold=setting("v3_intent_confidence_threshold", setting("intent_confidence_threshold", 0.7)),
            max_history_message_chars=setting("v3_max_history_message_chars", 800),
            enable_hyde=setting("enable_hyde", False),
        ),
        verbose=setting("verbose_mode", False),
    )


@dataclass(frozen=True, slots=True)
class RequestRAGConfiguration:
    config: Snapshot[RAGConfig]
    fallback: Literal["missing", "database_unavailable", "database_failure"] | None = None


class RAGConfigurationService:
    def __init__(self, store: ConfigStorePort) -> None:
        self._store = store

    async def load(self) -> RequestRAGConfiguration:
        """Call once at request entry and retain the result through every stage.

        DB failure retains the legacy default fallback but records why it happened.
        Cancellation and malformed configuration are never converted to defaults.
        The next call always retries the store, so admin edits and recovery are seen.
        """
        fallback: Literal["missing", "database_unavailable", "database_failure"] | None = None
        try:
            raw = await self._store.load()
        except DatabaseUnavailable:
            raw, fallback = None, "database_unavailable"
        except DatabaseFailure:
            raw, fallback = None, "database_failure"
        if raw is None:
            return RequestRAGConfiguration(Snapshot(from_runtime_values({}), "rag-default-v1", "default"), fallback or "missing")
        return RequestRAGConfiguration(Snapshot(from_runtime_values(raw.value), raw.revision, raw.origin))
