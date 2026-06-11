"""
Chat run logger for the RAG V3 Clean pipeline.

Handles INSERT/UPSERT of pipeline run data into the ``chat_runs`` table
with automatic CSV fallback.  Uses **dynamic SQL generation** so adding a
new column only requires adding a key to the dict returned by
``build_log_row`` — no separate column list to maintain.

Usage from 01_Chatbot.py::

    from assistant_rh_rag_pipeline.chat_logger import build_log_row, log_non_rag_row, log_run

    row = build_log_row(turn_id, query, response, pipeline, result, qr, config, runtime_cfg, session)
    log_run(row, engine=get_engine())
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from .models import estimate_tokens

if TYPE_CHECKING:
    from .config import RAGConfig
    from .pipeline import Pipeline
    from .query_processor import QueryProcessResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON serialization helpers
# ---------------------------------------------------------------------------


class SafeEncoder(json.JSONEncoder):
    """Handles UUID and other non-standard types to prevent silent logging failures."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


def _jdumps(obj: Any) -> str:
    return json.dumps(obj, cls=SafeEncoder, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Column type registry — drives dynamic SQL CAST
# ---------------------------------------------------------------------------

JSONB_COLUMNS: set[str] = {
    "filters",
    "retrieved",
    "sources_used_content",
    "dist_before_rerank",
    "dist_after_rerank",
    "boost_weights",
    "chunks_sent_to_selector",
    "v3_legal_refs_details",
    "v3_timing_breakdown",
    "v3_context_items_summary",
    "v3_source_distribution",
    "v3_context_items_full",
    "v3_selector_input_items",
    "v3_selector_decisions",
    "v3_chunks_raw",
    "v3_sections_raw",
    "v3_context_before_selector",
    "v3_retrieval_params",
    "v3_chunks_before_rerank",
    "v3_chunks_after_rerank",
    "v3_aggregation_params",
    "v3_sections_before_rerank",
    "v3_sections_after_rerank",
}


# ---------------------------------------------------------------------------
# Dynamic SQL generation
# ---------------------------------------------------------------------------


def _build_upsert_sql(data: dict) -> str:
    """Generate an INSERT … ON CONFLICT DO UPDATE for the given keys.

    JSONB columns are wrapped with ``CAST(:col AS jsonb)`` automatically.
    """
    cols = list(data.keys())
    values = [f"CAST(:{c} AS jsonb)" if c in JSONB_COLUMNS else f":{c}" for c in cols]
    update_cols = [c for c in cols if c not in ("turn_id", "ts")]
    update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' if c == "table" else f"{c} = EXCLUDED.{c}" for c in update_cols)
    col_names = ", ".join(f'"{c}"' if c == "table" else c for c in cols)
    return f"INSERT INTO chat_runs ({col_names}) VALUES ({', '.join(values)}) ON CONFLICT (turn_id) DO UPDATE SET {update_set}"


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _count_refs_in_context(context_items: list) -> int:
    """Count individual legal references across all context items."""
    total = 0
    for it in context_items:
        refs = getattr(it, "references_juridiques", None)
        if not refs:
            continue
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except (ValueError, json.JSONDecodeError):
                continue
        if isinstance(refs, list):
            total += len(refs)
        elif isinstance(refs, dict):
            total += 1
    return total


def _source_distribution(items: list, key: str = "publisher") -> dict:
    """Count occurrences by publisher."""
    dist: Dict[str, int] = {}
    for it in items:
        if isinstance(it, dict):
            pub = it.get(key) or "unknown"
        else:
            pub = getattr(it, key, None) or "unknown"
        dist[pub] = dist.get(pub, 0) + 1
    return dist


def build_log_row(
    turn_id: str,
    query: str,
    response: str,
    pipeline: "Pipeline",
    qr: "QueryProcessResult",
    config: "RAGConfig",
    runtime_config: Any,
    session_state: dict,
    total_time_ms: float,
    context_items: list,
    v1_chunks_for_display: list,
    legal_refs_v3: list,
) -> dict:
    """Build a complete logging dict from pipeline objects.

    This replaces the ~200-line dict literal that was previously inline in
    ``01_Chatbot.py``.
    """
    result = pipeline.last_result
    v3_metadata = result.metadata if result else {}
    v3_timing = result.timing if result else {}
    total_tokens = sum(getattr(it, "token_estimate", 0) for it in context_items)

    # Distributions
    source_dist_post_rerank = _source_distribution(v3_metadata.get("aggregated_sections", []))
    selector_decisions = v3_metadata.get("selector_decisions", {})
    if selector_decisions and selector_decisions.get("kept"):
        source_dist_post_selector = _source_distribution(selector_decisions["kept"])
    else:
        source_dist_post_selector = dict(source_dist_post_rerank)

    # Reranker status (issue #87: make rerank failures visible per run)
    reranker_status_meta = v3_metadata.get("reranker_status", {})
    section_reranker = reranker_status_meta.get("section", {}) if isinstance(reranker_status_meta, dict) else {}

    # Selector metrics
    selector_reasoning = v3_metadata.get("selector_reasoning", "")
    selector_items_before = v3_metadata.get("selector_items_before", 0)
    selector_items_after = v3_metadata.get("selector_items_after", 0)
    selector_confidence = (selector_items_after / selector_items_before) if selector_items_before > 0 else 0.0

    # Legal refs
    refs_in_context = _count_refs_in_context(context_items)
    refs_injected = len(getattr(pipeline._context_builder, "last_resolved_refs", {}) or {})
    resolved_refs = getattr(pipeline._context_builder, "last_resolved_refs", {}) or {}

    # Query processing results
    needs_legal_llm = qr.needs_legal_search
    query_for_retrieval = qr.query_for_retrieval
    intent = qr.intent_reason or ""
    should_proceed = qr.should_proceed
    acronyms_expanded = ",".join(qr.expanded_acronyms or [])
    acronyms_detected = ",".join(f"{a}={f}" for a, f in (qr.detected_acronyms or {}).items())
    was_enriched = qr.was_enriched
    enriched_query = qr.enriched_query or ""
    chunks_retrieved = v3_metadata.get("retrieved_chunks", [])
    sections = v3_metadata.get("aggregated_sections", [])

    # Prompt debugging
    full_prompt = getattr(pipeline, "last_full_prompt", "") or ""
    system_prompt_content = getattr(pipeline, "last_system_prompt", "") or ""
    selector_llm_response = v3_metadata.get("selector_raw_response", "")
    intent_raw_response = qr.intent_raw_response
    intent_name = qr.intent.value if qr.intent else None
    detected_theme = qr.theme
    reformulated_query = qr.enriched_query

    # Config shortcuts
    v3_context_mode = getattr(runtime_config, "v3_context_mode", "standard")
    v3_enable_selector = getattr(runtime_config, "v3_enable_selector", True)
    v3_search_mode = getattr(runtime_config, "v3_search_mode", "semantic")
    v3_initial_top_k = getattr(runtime_config, "v3_initial_top_k", 10)
    v3_rerank_top_k = getattr(runtime_config, "v3_rerank_top_k", 5)
    v3_enable_reranker = getattr(runtime_config, "v3_enable_reranker", True)

    # ── Core identifiers ───────────────────────────────────────────────
    row: dict = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "turn_id": turn_id,
        "question": query,
        "answer": response,
        "rag_version": "v3",
        "backend": f"rag_v3_{v3_context_mode}",
        "session_id": session_state.get("session_id", ""),
        "conversation_id": session_state.get("conversation_id", ""),
        "turn_index": len(session_state.get("turns", [])),
        "user_group": session_state.get("user_group", "default"),
        "total_time_ms": total_time_ms,
        "pipeline_latency_ms": total_time_ms,
    }

    # ── Config snapshot ───────────────────────────────────────────────
    row.update(
        {
            "system_prompt_name": config.generation.system_prompt_name or "",
            "use_intent_gating": config.query_processor.enable_intent_gating,
            "use_reranker": config.aggregation.enable_section_reranker,
            "top_k": v3_initial_top_k,
            "filters": _jdumps({"context_mode": v3_context_mode, "selector": v3_enable_selector}),
        }
    )

    # ── Query processing ──────────────────────────────────────────────
    row.update(
        {
            "query_for_retrieval": query_for_retrieval,
            "use_query_expansion": bool(acronyms_expanded),
            "acronyms_used": acronyms_expanded,
            "expanded_query": enriched_query if was_enriched else "",
            "intent_result": intent,
            "v3_intent": intent,
            "v3_intent_name": intent_name or "",
            "v3_intent_gating_enabled": config.query_processor.enable_intent_gating,
            "v3_should_proceed": should_proceed,
            "v3_needs_legal_llm": needs_legal_llm,
            "v3_detected_theme": detected_theme or "",
            "v3_reformulated_query": reformulated_query or "",
            "v3_was_enriched": was_enriched,
            "v3_enriched_query": (enriched_query or "")[:2000],
            "v3_acronyms_expanded": f"detected:[{acronyms_detected}] expanded:[{acronyms_expanded}]" if acronyms_detected else acronyms_expanded,
            "v3_intent_llm_response": (intent_raw_response or "")[:5000],
        }
    )

    # ── Retrieval & aggregation ───────────────────────────────────────
    row.update(
        {
            "v3_chunks_retrieved_count": len(chunks_retrieved),
            "v3_sections_count": len(sections),
            "v3_embedding_model": getattr(runtime_config, "embedding_model", ""),
            "v3_search_mode": v3_search_mode,
            "v3_reranker_enabled": config.aggregation.enable_section_reranker,
            "v3_rerank_top_k": v3_rerank_top_k if v3_enable_reranker else None,
            "v3_sections_before_rerank": _jdumps(v3_metadata.get("sections_before_rerank", 0)),
            "v3_sections_after_rerank": _jdumps(v3_metadata.get("sections_after_rerank", 0)),
            "v3_reranker_status": str(section_reranker.get("status", "") or ""),
            "v3_reranker_error": str(section_reranker.get("error", "") or "")[:2000],
            "dist_after_rerank": _jdumps(source_dist_post_rerank),
        }
    )

    # ── Selector ──────────────────────────────────────────────────────
    row.update(
        {
            "v3_context_mode": v3_context_mode,
            "v3_selector_confidence": selector_confidence,
            "v3_selector_selected_count": selector_items_after if v3_enable_selector else len(sections),
            "v3_selector_decisions": _jdumps(v3_metadata.get("selector_decisions", {})),
            "v3_selector_kept_indices": ",".join(str(d["idx"]) for d in selector_decisions.get("kept", [])),
            "v3_selector_removed_indices": ",".join(str(d["idx"]) for d in selector_decisions.get("removed", [])),
            "v3_selector_llm_response": (selector_llm_response or "")[:5000],
            "llm_selector_model": config.selector.model if v3_enable_selector else "",
            "llm_selector_reasoning": selector_reasoning,
            "v3_source_distribution": _jdumps(source_dist_post_selector),
        }
    )

    # ── Context ───────────────────────────────────────────────────────
    row.update(
        {
            "v3_context_items_count": len(context_items),
            "v3_context_tokens": total_tokens,
            "v3_doc_entire_count": sum(1 for item in context_items if getattr(item, "metadata", {}).get("is_doc_entire", False)),
            "v3_context_items_summary": _jdumps(v3_metadata.get("context_items_ref", [])),
            "v3_context_items_full": _jdumps(v3_metadata.get("aggregated_sections", [])),
            "sources_used_count": len(v1_chunks_for_display),
        }
    )

    # ── Legal references ──────────────────────────────────────────────
    row.update(
        {
            "v3_legal_refs_total": refs_in_context,
            "v3_legal_refs_from_expansion": refs_in_context,
            "v3_legal_refs_from_dgafp": refs_injected,
            "v3_legal_refs_details": _jdumps(
                [{"number": num, "cid": info.get("cid", ""), "title": info.get("title", "")} for num, info in resolved_refs.items()]
            )
            or "[]",
            "expanded_refs_count": len(legal_refs_v3),
        }
    )

    # ── Generation & prompts (debugging) ──────────────────────────────
    row.update(
        {
            "v3_generator_prompt_name": config.generation.system_prompt_name or "",
            "v3_full_prompt": (full_prompt or "")[:200000],
            "v3_system_prompt_content": (system_prompt_content or "")[:5000],
            "v3_response_length": int(v3_timing.get("response_length_tokens", estimate_tokens(response))),
        }
    )

    # ── Timing ────────────────────────────────────────────────────────
    row.update(
        {
            "v3_query_processing_ms": int(v3_timing.get("query_processing_ms", 0)),
            "v3_intent_ms": int(v3_timing.get("query_processing_ms", 0)),
            "v3_retrieval_ms": int(v3_timing.get("retrieval_ms", 0)),
            "v3_aggregation_ms": int(v3_timing.get("aggregation_ms", 0)),
            "v3_selector_ms": int(v3_timing.get("selector_ms", 0)),
            "v3_context_building_ms": int(v3_timing.get("context_build_ms", 0)),
            "v3_generation_ms": int(v3_timing.get("generation_ms", 0)),
            "v3_ttft_ms": int(v3_timing.get("ttft_ms", 0)),
            "v3_chars_per_second": round(v3_timing.get("chars_per_second", 0.0), 1),
            "v3_timing_breakdown": _jdumps(v3_timing),
        }
    )

    return row


def build_non_rag_row(
    turn_id: str,
    query: str,
    response: str,
    qr: "QueryProcessResult",
    pipeline: "Pipeline",
    session_state: dict,
    runtime_config: Any = None,
) -> dict:
    """Build a minimal log row for non-RAG turns (chit-chat, out-of-scope)."""
    _intent_val = qr.intent.value if qr.intent else "unknown"
    return {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "turn_id": turn_id,
        "question": query,
        "answer": response,
        "provider": getattr(runtime_config, "llm_provider", "") if runtime_config else "",
        "model": "",
        "temperature": 0,
        "backend": "intent_gating",
        "table": "",
        "embed_col": "",
        "filters": "{}",
        "top_k": 0,
        "use_reranker": False,
        "reranker_name": "",
        "rerank_top_k": 0,
        "retrieved": "[]",
        "prompt": "",
        "system_prompt": "",
        "system_prompt_name": "",
        "use_query_rewriting": False,
        "rewritten_query": "",
        "use_hyde": False,
        "hyde_document": "",
        "use_query_expansion": False,
        "expanded_query": "",
        "acronyms_used": ",".join(qr.expanded_acronyms or []),
        "query_for_retrieval": "",
        "retrieval_mode": "",
        "hybrid_alpha": 0,
        "sparse_method": "",
        "session_id": session_state.get("session_id", ""),
        "conversation_id": session_state.get("conversation_id", ""),
        "turn_index": len(session_state.get("turns", [])),
        "sources_used_count": 0,
        "sources_used_indices": "",
        "sources_used_content": "[]",
        "fallbacks_used": "",
        "sources_raw_line": "",
        "retrieval_time_ms": 0,
        "rerank_time_ms": 0,
        "llm_time_ms": 0,
        "total_time_ms": 0,
        "ttft_ms": 0,
        "tokens_per_second": 0.0,
        "dist_before_rerank": "{}",
        "dist_after_rerank": "{}",
        "boost_weights": "{}",
        "use_intent_gating": True,
        "intent_result": _intent_val,
        "intent_confidence": qr.intent_confidence,
        "intent_model": "",
        "use_query_reformulation": False,
        "reformulated_query": "",
        "reformulation_model": "",
        "pipeline_latency_ms": 0,
        "direct_response": response,
        "rag_version": "v3",
        "chunk_selection_mode": "INTENT_GATED",
        "cascade_source": "",
        "expanded_refs_count": 0,
        "user_group": session_state.get("user_group", "default"),
        "llm_selector_model": "",
        "llm_selector_prompt_name": "",
        "llm_selector_reasoning": "",
        "llm_selector_time_ms": 0,
        "chunks_sent_to_selector": "[]",
        "llm_selector_response": "",
        "pick_mode": "",
        "chunks_before_pick": 0,
        "chunks_after_pick": 0,
        "intent_gating_prompt_name": "",
        # V3 observability
        "v3_context_mode": "",
        "v3_intent": _intent_val,
        "v3_intent_gating_enabled": True,
        "v3_should_proceed": False,
        "v3_detected_theme": qr.theme or "",
        "v3_intent_llm_response": (qr.intent_raw_response or "")[:5000],
        "v3_intent_name": _intent_val,
        "v3_acronyms_expanded": ",".join(qr.expanded_acronyms or []),
        "v3_query_processing_ms": int(getattr(pipeline, "_timing", {}).get("query_processing_ms", 0)),
    }


# ---------------------------------------------------------------------------
# Persistence (DB + CSV fallback)
# ---------------------------------------------------------------------------

_CSV_REDACTED_FIELDS = {"v3_full_prompt", "v3_system_prompt_content"}


def _append_csv_row(path: Path, fieldnames: list[str], row: dict):
    """Fallback CSV logging if PostgreSQL is unavailable."""
    from filelock import FileLock

    row = {k: ("" if k in _CSV_REDACTED_FIELDS else row.get(k, "")) for k in fieldnames}
    lock = FileLock(str(path) + ".lock")
    with lock:
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                w.writeheader()
            w.writerow(row)


def _prepare_data(data: dict) -> dict:
    """Ensure JSON columns are strings and fill missing V3 columns with defaults."""
    out = data.copy()

    for col in ("filters", "retrieved", "sources_used_content", "boost_weights", "dist_before_rerank", "dist_after_rerank"):
        val = out.get(col)
        if isinstance(val, (dict, list)):
            out[col] = json.dumps(val, ensure_ascii=False)
        elif not val:
            default = "{}" if col in ("filters", "boost_weights", "dist_before_rerank", "dist_after_rerank") else "[]"
            out[col] = default

    if isinstance(out.get("chunks_sent_to_selector"), (dict, list)):
        out["chunks_sent_to_selector"] = json.dumps(out["chunks_sent_to_selector"], ensure_ascii=False)

    for col in ("rag_version", "chunk_selection_mode", "cascade_source", "expanded_refs_count", "user_group"):
        if col not in out:
            out[col] = None

    for col in (
        "llm_selector_model",
        "llm_selector_prompt_name",
        "llm_selector_reasoning",
        "pick_mode",
        "intent_gating_prompt_name",
        "chunks_sent_to_selector",
        "llm_selector_response",
    ):
        if col not in out:
            out[col] = None
    for col in ("llm_selector_time_ms", "chunks_before_pick", "chunks_after_pick"):
        if col not in out:
            out[col] = None

    for col in JSONB_COLUMNS:
        if col in out and out[col] is None:
            out[col] = "[]"

    return out


def log_run(row: dict, engine=None, csv_path: Optional[Path] = None, csv_fields: Optional[list] = None):
    """Upsert a row into chat_runs; CSV fallback on failure.

    *engine* should be a SQLAlchemy engine (or ``None`` to skip DB).
    *csv_path* / *csv_fields* are used for CSV fallback.
    """
    data = _prepare_data(row)

    if engine:
        try:
            from sqlalchemy import text

            sql = _build_upsert_sql(data)
            with engine.connect() as conn:
                conn.execute(text(sql), data)
                conn.commit()
            return
        except Exception as exc:
            logger.warning("PostgreSQL log failed, using CSV fallback: %s", exc)

    if csv_path and csv_fields:
        _append_csv_row(csv_path, csv_fields, data)
