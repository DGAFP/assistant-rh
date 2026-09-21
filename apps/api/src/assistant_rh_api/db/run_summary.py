"""Compatibility projection for existing chat-log and timeline SQL readers."""

from collections.abc import Mapping

from assistant_rh_api.core.models.conversations import ChatRun

STAGE_TIMINGS = {
    "query-processor": "query_processing_ms",
    "retriever": "retrieval_ms",
    "section-aggregator": "aggregation_ms",
    "context-selector": "selector_ms",
    "context-builder": "context_building_ms",
    "generator": "generation_ms",
}


def legacy_summary(run: ChatRun) -> dict:
    stages = {event.stage: event.metrics for event in run.events if isinstance(event.metrics, Mapping)}
    query = stages.get("query-processor", {})
    retrieval = stages.get("retriever", {})
    aggregation = stages.get("section-aggregator", {})
    selection = stages.get("context-selector", {})
    context = stages.get("context-builder", {})
    generation = stages.get("generator", {})
    # Retries contribute to elapsed stage totals, rather than overwriting the first attempt.
    timing = {key: sum(event.duration_ms for event in run.events if event.stage == stage) for stage, key in STAGE_TIMINGS.items()}
    generation_ms = timing["generation_ms"]
    answer_chars = generation.get("answer_characters")
    return {
        "provider": generation.get("provider"),
        "model": generation.get("model") if run.metrics else run.model,
        "backend": "hexagonal_api",
        "rag_version": "v3",
        "total_time_ms": run.metrics.elapsed_ms if run.metrics else None,
        "pipeline_latency_ms": run.metrics.elapsed_ms if run.metrics else None,
        # Historical TTFT starts at generation; request TTFT lives in api_record.metrics.
        "ttft_ms": run.metrics.generation_first_token_ms if run.metrics else None,
        "v3_ttft_ms": run.metrics.generation_first_token_ms if run.metrics else None,
        "v3_chars_per_second": round(answer_chars * 1000 / generation_ms, 1)
        if isinstance(answer_chars, int) and generation_ms > 0 and generation.get("provider")
        else None,
        "sources_used_count": len(run.sources),
        "v3_intent_name": query.get("intent"),
        "intent_confidence": query.get("intent_confidence"),
        "v3_should_proceed": query.get("should_proceed"),
        "v3_needs_legal_final": query.get("needs_legal_search"),
        "v3_chunks_retrieved_count": retrieval.get("chunks_count"),
        "v3_embedding_model": retrieval.get("embedding_model"),
        "v3_sections_count": aggregation.get("sections_count"),
        "v3_reranker_status": aggregation.get("reranker_status"),
        "v3_selector_selected_count": selection.get("selected_count"),
        "llm_selector_model": selection.get("model"),
        "v3_context_items_count": context.get("context_items_count"),
        "v3_context_tokens": context.get("context_tokens"),
        "v3_doc_entire_count": context.get("full_doc_count"),
        **{"v3_" + key: value for key, value in timing.items()},
    }
