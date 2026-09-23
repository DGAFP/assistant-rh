"""Small numeric summaries, measured before detailed traces can be truncated."""

from assistant_rh_api.core.models.configuration import JsonValue
from assistant_rh_api.core.models.context import ContextBuildResult, SectionAggregationResult
from assistant_rh_api.core.models.generation import GenerationResult
from assistant_rh_api.core.models.inference import Completion, StreamCompleted
from assistant_rh_api.core.models.query_processing import QueryProcessing
from assistant_rh_api.core.models.selection import SelectionResult
from assistant_rh_api.core.pipeline.steps.retrieval import RetrievalResult


def completion_metrics(outcome: Completion | StreamCompleted | None) -> dict[str, JsonValue]:
    if outcome is None:
        return {"provider": None, "model": None, "usage_known": False}
    usage = outcome.usage
    return {
        "provider": outcome.provider,
        "model": outcome.model,
        "attempt_count": len(outcome.attempts),
        "failed_attempt_count": sum(attempt.error is not None for attempt in outcome.attempts),
        "fallback_used": bool(outcome.attempts and outcome.provider != outcome.attempts[0].provider),
        "usage_known": usage is not None,
        "prompt_tokens": usage.prompt_tokens if usage else None,
        "completion_tokens": usage.completion_tokens if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
    }


def query_metrics(value: QueryProcessing) -> JsonValue:
    return {
        **completion_metrics(value.diagnostics.completion),
        "intent": value.result.intent.value,
        "intent_confidence": value.result.intent_confidence,
        "should_proceed": value.result.should_proceed,
        "needs_legal_search": value.result.needs_legal_search,
        "classification_status": value.diagnostics.classification_status,
    }


def retrieval_metrics(value: RetrievalResult) -> JsonValue:
    return {
        "chunks_count": len(value.chunks),
        "source_count": len(value.sources),
        "failed_lanes_count": len(value.failures),
        "embedding_provider": value.embedding.provider if value.embedding else None,
        "embedding_model": value.embedding.model if value.embedding else None,
        "embedding_attempt_count": len(value.embedding_attempts),
    }


def aggregation_metrics(value: SectionAggregationResult) -> JsonValue:
    return {
        "sections_count": len(value.sections),
        "sections_before_rerank": value.diagnostics.sections_before_rerank,
        "sections_after_rerank": value.diagnostics.sections_after_rerank,
        "reranker_status": value.diagnostics.reranker_status,
        "reranker_fallback": value.diagnostics.reranker_fallback,
    }


def selection_metrics(value: SelectionResult) -> JsonValue:
    return {
        **completion_metrics(value.diagnostics.completion),
        "selection_status": value.diagnostics.status,
        "selected_count": len(value.sections),
        "all_rejected": value.all_rejected,
    }


def context_metrics(value: ContextBuildResult) -> JsonValue:
    return {
        "context_items_count": len(value.items),
        "context_tokens": value.diagnostics.tokens_used,
        "token_budget": value.diagnostics.token_budget,
        "full_doc_count": value.diagnostics.full_doc_count,
        "triangulation_count": value.diagnostics.triangulation_count,
        "reference_tokens": value.diagnostics.reference_tokens,
        "resolved_refs_count": len(value.resolved_refs),
    }


def generation_metrics(value: GenerationResult) -> JsonValue:
    return {
        **completion_metrics(value.diagnostics.outcome),
        "generation_status": value.diagnostics.status,
        "answer_characters": len(value.answer),
    }
