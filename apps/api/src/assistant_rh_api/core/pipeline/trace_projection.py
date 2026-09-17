"""Explicit persisted fields for each stage, independent of result serialization."""

from assistant_rh_api.core.models.configuration import ConfigValues, JsonValue, Prompt, Snapshot
from assistant_rh_api.core.models.context import AggregatedSection, ContextBuildResult, ContextItem, SectionAggregationResult
from assistant_rh_api.core.models.generation import GenerationResult
from assistant_rh_api.core.models.inference import Completion, StreamCompleted
from assistant_rh_api.core.models.query_processing import QueryProcessing
from assistant_rh_api.core.models.retrieval import RetrievedChunk
from assistant_rh_api.core.models.selection import SelectionResult
from assistant_rh_api.core.pipeline.steps.retrieval import RetrievalResult
from assistant_rh_api.core.rag_configuration import RequestRAGConfiguration
from assistant_rh_api.core.trace_values import attempt_trace


def _prompt(snapshot: Snapshot[Prompt] | None) -> JsonValue:
    if snapshot is None:
        return None
    return {"name": snapshot.value.name, "content": snapshot.value.content, "revision": snapshot.revision, "origin": snapshot.origin}


def _outcome(outcome: Completion | StreamCompleted | None) -> JsonValue:
    if outcome is None:
        return None
    usage = outcome.usage
    return {
        "provider": outcome.provider,
        "model": outcome.model,
        "attempts": attempt_trace(outcome.attempts),
        "finish_reason": outcome.finish_reason,
        "usage": None
        if usage is None
        else {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens, "total_tokens": usage.total_tokens},
    }


def _metadata(metadata: ConfigValues) -> JsonValue:
    return {
        key: metadata[key]
        for key in ("doc_id", "doc_short_id", "cid", "number", "is_doc_entire", "is_triangulation", "retrieval_path", "source_score_mode")
        if key in metadata
    }


def _chunk(chunk: RetrievedChunk) -> JsonValue:
    return {
        "chunk_id": chunk.chunk_id,
        "section_id": chunk.section_id,
        "table_source": chunk.table_source,
        "score": chunk.score,
        "metadata": _metadata(chunk.metadata),
    }


def _section(section: AggregatedSection) -> JsonValue:
    return {
        "section_id": section.section_id,
        "document_id": section.document_id,
        "heading": section.heading,
        "publisher": section.publisher,
        "score": section.score,
        "chunk_ids": tuple(chunk.chunk_id for chunk in section.chunks),
        "metadata": _metadata(section.metadata),
    }


def _item(item: ContextItem) -> JsonValue:
    return {
        "section_id": item.section_id,
        "document_title": item.document_title,
        "heading": item.heading,
        "publisher": item.publisher,
        "score": item.score,
        "token_estimate": item.token_estimate,
        "metadata": _metadata(item.metadata),
    }


def _ranked_chunks(chunks: tuple[ConfigValues, ...]) -> tuple[JsonValue, ...]:
    return tuple({key: chunk[key] for key in ("doc_id", "chunk_id", "section_id", "final_score", "rerank_score") if key in chunk} for chunk in chunks)


def configuration_trace(configuration: RequestRAGConfiguration) -> JsonValue:
    return {
        "config": configuration.config.value.to_dict(),
        "revision": configuration.config.revision,
        "origin": configuration.config.origin,
        "fallback": configuration.fallback,
    }


def query_trace(processing: QueryProcessing) -> JsonValue:
    result, diagnostics = processing.result, processing.diagnostics
    acronyms = diagnostics.acronyms
    return {
        "result": {
            "original_query": result.original_query,
            "processed_query": result.processed_query,
            "enriched_query": result.enriched_query,
            "intent": result.intent.value,
            "intent_confidence": result.intent_confidence,
            "intent_reason": result.intent_reason,
            "needs_legal_search": result.needs_legal_search,
            "needs_legal_search_llm": result.needs_legal_search_llm,
            "theme": result.theme,
            "direct_response": result.direct_response,
            "expanded_acronyms": result.expanded_acronyms,
            "detected_acronyms": tuple({"short": a.short, "expansion": a.expansion} for a in result.detected_acronyms),
        },
        "diagnostics": {
            "classification_status": diagnostics.classification_status,
            "classification_error": diagnostics.classification_error,
            "prompt": _prompt(diagnostics.prompt),
            "completion": _outcome(diagnostics.completion),
            "raw_response": diagnostics.completion.text if diagnostics.completion else "",
            "failed_attempts": attempt_trace(diagnostics.failed_attempts),
            "store_errors": diagnostics.store_errors,
            "acronyms_revision": acronyms.revision if acronyms else None,
            "acronyms_origin": acronyms.origin if acronyms else None,
            "acronyms": tuple({"short": a.short, "expansion": a.expansion} for a in acronyms.value) if acronyms else (),
        },
    }


def retrieval_trace(result: RetrievalResult) -> JsonValue:
    embedding = result.embedding
    return {
        "chunks": tuple(_chunk(chunk) for chunk in result.chunks),
        "chunk_count": len(result.chunks),
        "sources": result.sources,
        "failures": tuple({"source": failure.source, "lane": failure.lane} for failure in result.failures),
        "embedding_failed": result.embedding_failed,
        "embedding_attempts": attempt_trace(result.embedding_attempts),
        "embedding": None
        if embedding is None
        else {
            "model": embedding.model,
            "provider": embedding.provider,
            "dimensions": len(embedding.vector),
            "attempts": attempt_trace(embedding.attempts),
        },
    }


def aggregation_trace(result: SectionAggregationResult) -> JsonValue:
    diagnostics = result.diagnostics
    return {
        "sections": tuple(_section(section) for section in result.sections),
        "diagnostics": {
            "sections_before_rerank": diagnostics.sections_before_rerank,
            "sections_after_rerank": diagnostics.sections_after_rerank,
            "reranker_status": diagnostics.reranker_status,
            "reranker_error": diagnostics.reranker_error,
            "reranker_attempts": attempt_trace(diagnostics.reranker_attempts),
            "reranker_fallback": diagnostics.reranker_fallback,
            "chunks_before_rerank": _ranked_chunks(diagnostics.chunks_before_rerank),
            "chunks_after_rerank": _ranked_chunks(diagnostics.chunks_after_rerank),
            "store_errors": diagnostics.store_errors,
        },
    }


def selection_trace(result: SelectionResult) -> JsonValue:
    diagnostics = result.diagnostics
    return {
        "sections": tuple(_section(section) for section in result.sections),
        "diagnostics": {
            "status": diagnostics.status,
            "decisions": diagnostics.decisions,
            "reason": diagnostics.reason,
            "raw_response": diagnostics.raw_response,
            "prompt": _prompt(diagnostics.prompt),
            "user_prompt": diagnostics.user_prompt,
            "completion": _outcome(diagnostics.completion),
            "failed_attempts": attempt_trace(diagnostics.failed_attempts),
            "store_errors": diagnostics.store_errors,
        },
    }


def context_trace(result: ContextBuildResult) -> JsonValue:
    diagnostics = result.diagnostics
    return {
        "items": tuple(_item(item) for item in result.items),
        "resolved_refs": result.resolved_refs,
        "diagnostics": {
            "tokens_used": diagnostics.tokens_used,
            "token_budget": diagnostics.token_budget,
            "full_doc_count": diagnostics.full_doc_count,
            "triangulation_count": diagnostics.triangulation_count,
            "reference_tokens": diagnostics.reference_tokens,
            "store_errors": diagnostics.store_errors,
        },
    }


def generation_trace(result: GenerationResult) -> JsonValue:
    diagnostics = result.diagnostics
    request = diagnostics.request
    return {
        "answer": result.answer,
        "diagnostics": {
            "status": diagnostics.status,
            "prompt": _prompt(diagnostics.prompt),
            "request": None
            if request is None
            else {
                "messages": tuple({"role": message.role, "content": message.content} for message in request.messages),
                "temperature": request.temperature,
            },
            "outcome": _outcome(diagnostics.outcome),
            "store_errors": diagnostics.store_errors,
        },
    }
