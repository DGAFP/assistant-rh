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
from .tracing import export_events_to_otel, normalize_trace_id

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

TRACE_EVENT_JSONB_COLUMNS: set[str] = {"input_ref", "output_ref", "metrics"}

TRACE_EVENT_COLUMNS: tuple[str, ...] = (
    "turn_id",
    "trace_id",
    "env",
    "event_index",
    "stage",
    "attempt_name",
    "duration_ms",
    "status",
    "input_ref",
    "output_ref",
    "metrics",
    "error_type",
    "error_message",
)


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


def _build_trace_event_upsert_sql() -> str:
    values = [f"CAST(:{c} AS jsonb)" if c in TRACE_EVENT_JSONB_COLUMNS else f":{c}" for c in TRACE_EVENT_COLUMNS]
    updates = [c for c in TRACE_EVENT_COLUMNS if c not in ("turn_id", "event_index")]
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
    return (
        f"INSERT INTO rag_trace_events ({', '.join(TRACE_EVENT_COLUMNS)}) VALUES ({', '.join(values)}) "
        f"ON CONFLICT (turn_id, event_index) DO UPDATE SET {update_set}"
    )


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


_TABLE_BY_RUNTIME_KEY = {
    "matte": "rag_chunks_matte",
    "mso": "rag_chunks_mso",
    "service_public": "rag_chunks_service_public",
    "service_public_scw": "rag_chunks_service_public_scw",
    "dgafp": "rag_chunks_dgafp",
    "dgafp_scw": "rag_chunks_dgafp_scw",
    "rgrh": "rag_chunks_rgrh",
}

_TABLE_BY_PUBLISHER = {
    "matte": "rag_chunks_matte",
    "mso": "rag_chunks_mso",
    "service-public": "rag_chunks_service_public",
    "service public": "rag_chunks_service_public",
    "service-public (scaleway)": "rag_chunks_service_public_scw",
    "dgafp": "rag_chunks_dgafp",
    "dgafp (scaleway)": "rag_chunks_dgafp_scw",
    "rgrh": "rag_chunks_rgrh",
}

_ALBERT_EMBED_COL_BY_TABLE = {
    "rag_chunks_matte": "embedding_m3",
    "rag_chunks_mso": "embedding_m3",
    "rag_chunks_service_public": "embedding_m3",
    "rag_chunks_service_public_scw": "embedding_m3",
    "rag_chunks_dgafp": "embedding_m3",
    "rag_chunks_dgafp_scw": "embedding_m3",
    "rag_chunks_rgrh": "embedding_m3",
}

_BGE_EMBED_COL_BY_TABLE = {
    "rag_chunks_matte": "embedding_bge_scw",
    "rag_chunks_mso": "embedding_bge_scw",
    "rag_chunks_service_public": "embedding_bge_scw",
    "rag_chunks_service_public_scw": "embedding_bge_scw",
    "rag_chunks_dgafp": "embedding_bge_scw",
    "rag_chunks_dgafp_scw": "embedding_bge_scw",
    "rag_chunks_rgrh": "embedding_bge_scw",
}

_TABLE_LABEL_BY_TABLE = {
    "rag_chunks_matte": "matte",
    "rag_chunks_mso": "mso",
    "rag_chunks_service_public": "sp",
    "rag_chunks_service_public_scw": "sp_scw",
    "rag_chunks_dgafp": "dgafp",
    "rag_chunks_dgafp_scw": "dgafp_scw",
    "rag_chunks_rgrh": "rgrh",
}

_LEGACY_VARCHAR_30_LIMIT = 30


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _selected_ministry_value(scope: Any) -> str | None:
    """Raw ministry id for ``chat_runs.selected_ministry`` (NULL when unknown).

    Accepts a ``RetrievalScope``, a plain ministry id string, or *None*.
    Never guesses from retrieved sources: shared sources (Service-Public,
    DGAFP) cannot identify the active ministry.
    """
    if scope is None:
        return None
    raw = getattr(scope, "selected_ministry", scope)
    text = str(raw or "").strip()
    return text or None


def _table_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("rag_chunks_"):
        return raw
    return _TABLE_BY_RUNTIME_KEY.get(raw, _TABLE_BY_PUBLISHER.get(raw.lower(), raw))


def _table_names(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _table_name(value)
        if name and name not in seen:
            out.append(name)
            seen.add(name)
    return out


def _embed_columns_for_tables(table_names: list[str], embedding_model: str) -> str:
    mapping = _BGE_EMBED_COL_BY_TABLE if "bge" in (embedding_model or "").lower() else _ALBERT_EMBED_COL_BY_TABLE
    cols: list[str] = []
    seen: set[str] = set()
    for table in table_names:
        col = mapping.get(table)
        if col and col not in seen:
            cols.append(col)
            seen.add(col)
    return ",".join(cols)


def _legacy_table_label(table_names: list[str]) -> str:
    """Compact table list for legacy ``chat_runs`` varchar(30) columns."""
    label = ",".join(_TABLE_LABEL_BY_TABLE.get(table, table) for table in table_names)
    if len(label) <= _LEGACY_VARCHAR_30_LIMIT:
        return label
    return label[:_LEGACY_VARCHAR_30_LIMIT]


def _chunk_table_for_ref(ref: dict[str, Any]) -> str:
    return _table_name(ref.get("table") or ref.get("doc_publisher") or ref.get("publisher") or ref.get("source") or ref.get("table_source"))


def _serialize_display_sources(chunks: list) -> list[dict[str, Any]]:
    """Serialize rendered source pills into the historical ``retrieved`` shape."""
    out: list[dict[str, Any]] = []
    for chunk in chunks or []:
        meta = getattr(chunk, "metadata", {}) or {}
        chunk_id = getattr(chunk, "id", "") or meta.get("source_document_id") or meta.get("doc_id") or meta.get("cid") or ""
        source = meta.get("source") or meta.get("source_name") or meta.get("doc_title") or meta.get("title") or meta.get("full_title") or ""
        out.append(
            {
                "id": str(chunk_id),
                "score": round(float(getattr(chunk, "score", 0.0) or 0.0), 4),
                "source": source,
                "source_name": meta.get("source_name") or source,
                "doc_title": meta.get("doc_title") or meta.get("title") or meta.get("full_title") or source,
                "page": meta.get("page"),
                "url": meta.get("url"),
            }
        )
    return out


def _legacy_chunk_refs(refs: Any) -> list[dict[str, Any]]:
    """Normalize current V3 chunk traces for legacy feedback-analysis columns."""
    if not isinstance(refs, list):
        return []
    out: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        chunk_id = ref.get("chunk_id") or ref.get("id")
        table = _chunk_table_for_ref(ref)
        if not chunk_id:
            continue
        out.append(
            {
                "chunk_id": str(chunk_id),
                "table": table,
                "score": ref.get("score", ref.get("final_score")),
                "section_id": str(ref.get("section_id") or ""),
                "source_name": ref.get("doc_title") or ref.get("source_name") or ref.get("heading") or ref.get("section_heading") or "",
                "preview": ref.get("preview") or ref.get("chunk_markdown") or "",
            }
        )
    return out


def _coerce_ref_entries(refs: Any) -> list[dict[str, Any]]:
    if not refs:
        return []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except (ValueError, json.JSONDecodeError):
            return []
    if isinstance(refs, dict):
        return [refs]
    if isinstance(refs, list):
        return [r for r in refs if isinstance(r, dict)]
    return []


def _legal_ref_details(context_items: list, resolved_refs: dict, legal_refs_v3: list) -> list[dict[str, Any]]:
    """Build the detailed legal-ref log from selected context, with matched/ref fallback."""
    details: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add_ref(*, number: Any, cid: Any = "", title: Any = "", url: Any = "", source: Any = "") -> None:
        number_s = str(number or "")
        if not number_s:
            return
        resolved = resolved_refs.get(number_s) if isinstance(resolved_refs, dict) else None
        if isinstance(resolved, dict):
            cid = cid or resolved.get("cid", "")
            title = title or resolved.get("title", "")
            url = url or resolved.get("url", "")
        cid_s = str(cid or "")
        title_s = str(title or "")
        source_s = str(source or "")
        key = (number_s, cid_s, title_s, source_s)
        if key in seen:
            return
        seen.add(key)
        details.append({"number": number_s, "cid": cid_s, "title": title_s, "url": str(url or ""), "source": source_s})

    for item in context_items or []:
        item_source = getattr(item, "publisher", "") or ""
        item_title = getattr(item, "document_title", "") or getattr(item, "heading", "") or ""
        for ref in _coerce_ref_entries(getattr(item, "references_juridiques", None)):
            add_ref(
                number=ref.get("number") or ref.get("article") or ref.get("id"),
                cid=ref.get("cid", ""),
                title=ref.get("title") or ref.get("full_title") or item_title,
                url=ref.get("url", ""),
                source=ref.get("source") or item_source,
            )

    if not details:
        for ref in legal_refs_v3 or []:
            add_ref(
                number=getattr(ref, "number", ""),
                cid=getattr(ref, "cid", ""),
                title=getattr(ref, "title", ""),
                url=getattr(ref, "url", ""),
                source="DGAFP" if getattr(ref, "cid", "") else "",
            )

    if not details and isinstance(resolved_refs, dict):
        for number, info in resolved_refs.items():
            if isinstance(info, dict):
                add_ref(number=number, cid=info.get("cid", ""), title=info.get("title", ""), url=info.get("url", ""), source="DGAFP")

    return details


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
    trace_id: str | None = None,
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
    reranker_status_meta = v3_metadata.get("reranker_status")
    section_reranker = reranker_status_meta.get("section") if isinstance(reranker_status_meta, dict) else None
    if not isinstance(section_reranker, dict):
        section_reranker = {}

    # Selector metrics
    selector_reasoning = v3_metadata.get("selector_reasoning", "")
    selector_items_before = v3_metadata.get("selector_items_before", 0)
    selector_items_after = v3_metadata.get("selector_items_after", 0)
    selector_confidence = (selector_items_after / selector_items_before) if selector_items_before > 0 else 0.0

    # Legal refs
    refs_in_context = _count_refs_in_context(context_items)
    refs_injected = len(getattr(pipeline._context_builder, "last_resolved_refs", {}) or {})
    resolved_refs = getattr(pipeline._context_builder, "last_resolved_refs", {}) or {}

    # Query processing results. `needs_legal_search` is the merged decision
    # (LLM ∪ heuristic). `needs_legal_search_llm` is the original LLM signal
    # (None when classify failed or gating was off). Log both so dashboards
    # can distinguish LLM-driven from heuristic-driven decisions.
    needs_legal_final = qr.needs_legal_search
    needs_legal_llm = qr.needs_legal_search_llm
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
    retrieval_config = getattr(config, "retrieval", None)
    query_processor_config = getattr(config, "query_processor", None)
    generation_config = getattr(config, "generation", None)

    configured_tables = list(getattr(retrieval_config, "tables", []) or [])
    table_names = _table_names(v3_metadata.get("tables_searched") or configured_tables)
    table_label = _legacy_table_label(table_names)
    embedding_model_logged = str(
        v3_metadata.get("embedding_model")
        or getattr(runtime_config, "embedding_model", "")
        or _enum_value(getattr(retrieval_config, "embedding_model", ""))
        or ""
    )
    generation_provider = str(
        v3_metadata.get("generator_provider")
        or getattr(runtime_config, "llm_provider", "")
        or _enum_value(getattr(generation_config, "provider", ""))
        or ""
    )
    generation_model = str(
        v3_metadata.get("generator_model")
        or getattr(runtime_config, "v3_generator_model", "")
        or getattr(generation_config, "model", "")
        or getattr(runtime_config, "llm_model", "")
        or ""
    )
    generation_temperature = getattr(
        generation_config,
        "temperature",
        getattr(runtime_config, "v3_temperature", getattr(runtime_config, "temperature", 0)),
    )
    legacy_retrieved = _serialize_display_sources(v1_chunks_for_display)
    legacy_chunk_refs = _legacy_chunk_refs(
        v3_metadata.get("context_before_selector")
        or v3_metadata.get("chunks_after_rerank")
        or v3_metadata.get("retrieved_chunks")
        or v3_metadata.get("chunks_raw")
        or []
    )
    legal_ref_details = _legal_ref_details(context_items, resolved_refs, legal_refs_v3)
    selector_before_count = int(selector_items_before or len(sections) or len(legacy_chunk_refs) or 0)
    selector_after_count = int(selector_items_after if v3_enable_selector else len(context_items))

    # ── Core identifiers ───────────────────────────────────────────────
    row: dict = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "turn_id": turn_id,
        "trace_id": trace_id or v3_metadata.get("trace_id", ""),
        "question": query,
        "answer": response,
        "provider": generation_provider,
        "model": generation_model,
        "temperature": generation_temperature,
        "rag_version": "v3",
        "backend": f"rag_v3_{v3_context_mode}",
        "session_id": session_state.get("session_id", ""),
        "conversation_id": session_state.get("conversation_id", ""),
        "turn_index": len(session_state.get("turns", [])),
        "user_group": session_state.get("user_group", "default"),
        "selected_ministry": _selected_ministry_value(v3_metadata.get("selected_ministry")),
        "total_time_ms": total_time_ms,
        "pipeline_latency_ms": total_time_ms,
    }

    # ── Config snapshot ───────────────────────────────────────────────
    row.update(
        {
            "system_prompt_name": config.generation.system_prompt_name or "",
            "use_intent_gating": config.query_processor.enable_intent_gating,
            "use_reranker": config.aggregation.enable_section_reranker,
            "reranker_name": getattr(runtime_config, "reranker_name", "") or "section_reranker",
            "rerank_top_k": v3_rerank_top_k if v3_enable_reranker else 0,
            "top_k": v3_initial_top_k,
            "filters": _jdumps({"context_mode": v3_context_mode, "selector": v3_enable_selector}),
            "table": table_label,
            "embed_col": _embed_columns_for_tables(table_names, embedding_model_logged),
            "retrieval_mode": v3_search_mode,
            "hybrid_alpha": getattr(runtime_config, "v3_alpha", getattr(runtime_config, "hybrid_alpha", getattr(retrieval_config, "alpha", 0))),
            "sparse_method": getattr(runtime_config, "sparse_method", ""),
            "use_hyde": bool(getattr(query_processor_config, "enable_hyde", False)),
            "hyde_document": "",
            "use_query_rewriting": False,
            "rewritten_query": "",
            "use_query_reformulation": False,
            "reformulated_query": "",
            "reformulation_model": "",
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
            "intent_confidence": qr.intent_confidence,
            "intent_model": getattr(query_processor_config, "intent_model", ""),
            "v3_intent": intent,
            "v3_intent_name": intent_name or "",
            "v3_intent_gating_enabled": config.query_processor.enable_intent_gating,
            "v3_should_proceed": should_proceed,
            "v3_needs_legal_llm": needs_legal_llm,
            "v3_needs_legal_final": needs_legal_final,
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
            "v3_chunks_raw": _jdumps(v3_metadata.get("chunks_raw", [])),
            "v3_chunks_before_rerank": _jdumps(v3_metadata.get("chunks_before_rerank", [])),
            "v3_chunks_after_rerank": _jdumps(v3_metadata.get("chunks_after_rerank", [])),
            "v3_context_before_selector": _jdumps(v3_metadata.get("context_before_selector", [])),
            "v3_reranker_status": str(section_reranker.get("status", "") or ""),
            "v3_reranker_error": str(section_reranker.get("error", "") or "")[:2000],
            "retrieved": _jdumps(legacy_retrieved),
            "chunks_before_pick": selector_before_count,
            "chunks_after_pick": selector_after_count,
            "chunks_sent_to_selector": _jdumps(legacy_chunk_refs),
            "chunk_selection_mode": f"V3_{str(v3_context_mode).upper()}",
            "cascade_source": table_label,
            "pick_mode": "llm_selector" if v3_enable_selector else "top_sections",
            "dist_before_rerank": _jdumps(_source_distribution(v3_metadata.get("retrieved_chunks", []), key="table")),
            "dist_after_rerank": _jdumps(source_dist_post_rerank),
            "boost_weights": "{}",
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
            "llm_selector_prompt_name": getattr(config.selector, "prompt_name", "") if v3_enable_selector else "",
            "llm_selector_reasoning": selector_reasoning,
            "llm_selector_time_ms": int(v3_timing.get("selector_ms", 0)),
            "llm_selector_response": (selector_llm_response or "")[:5000],
            "v3_source_distribution": _jdumps(source_dist_post_selector),
            "v3_need_more_context": False,
            "v3_suggested_expansion": "",
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
            "sources_used_indices": ",".join(str(i) for i in range(len(v1_chunks_for_display))),
            "sources_used_content": "[]",
            "fallbacks_used": "",
            "sources_raw_line": "",
        }
    )

    # ── Legal references ──────────────────────────────────────────────
    row.update(
        {
            "v3_legal_refs_total": refs_in_context,
            "v3_legal_refs_from_expansion": refs_in_context,
            "v3_legal_refs_from_dgafp": refs_injected,
            "v3_legal_refs_details": _jdumps(legal_ref_details),
            "expanded_refs_count": len(legal_refs_v3) if legal_refs_v3 is not None else len(legal_ref_details),
        }
    )

    # ── Generation & prompts (debugging) ──────────────────────────────
    row.update(
        {
            "v3_generator_prompt_name": config.generation.system_prompt_name or "",
            "v3_full_prompt": (full_prompt or "")[:200000],
            "v3_system_prompt_content": (system_prompt_content or "")[:5000],
            "v3_response_length": int(v3_timing.get("response_length_tokens", estimate_tokens(response))),
            "prompt": "",
            "system_prompt": "",
            "direct_response": response,
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
            "retrieval_time_ms": int(v3_timing.get("retrieval_ms", 0)),
            "rerank_time_ms": int(v3_timing.get("aggregation_ms", 0)),
            "llm_time_ms": int(v3_timing.get("generation_ms", 0)),
            "ttft_ms": int(v3_timing.get("ttft_ms", 0)),
            "tokens_per_second": round(v3_timing.get("tokens_per_second", v3_timing.get("chars_per_second", 0.0)), 1),
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
    trace_id: str | None = None,
    retrieval_scope: Any = None,
) -> dict:
    """Build a minimal log row for non-RAG turns (chit-chat, out-of-scope)."""
    _intent_val = qr.intent.value if qr.intent else "unknown"
    return {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "turn_id": turn_id,
        "trace_id": trace_id or "",
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
        "selected_ministry": _selected_ministry_value(retrieval_scope),
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
    path.parent.mkdir(parents=True, exist_ok=True)
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

    for col in ("table", "cascade_source"):
        if isinstance(out.get(col), str) and len(out[col]) > _LEGACY_VARCHAR_30_LIMIT:
            out[col] = out[col][:_LEGACY_VARCHAR_30_LIMIT]

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


def _env_label(explicit: str | None = None) -> str:
    import os

    value = (explicit or os.getenv("APP_ENV") or os.getenv("APP_SCALEWAY_ENV") or "").strip().lower()
    if value == "production":
        return "prod"
    return value


def _prepare_trace_event_rows(
    *,
    turn_id: str,
    trace_id: str,
    events: list[dict[str, Any]],
    env_label: str,
) -> list[dict[str, Any]]:
    normalized_trace_id = normalize_trace_id(trace_id)
    rows: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        rows.append(
            {
                "turn_id": turn_id,
                "trace_id": normalized_trace_id,
                "env": env_label,
                "event_index": index,
                "stage": str(event.get("stage", "") or ""),
                "attempt_name": str(event.get("attempt_name", "") or ""),
                "duration_ms": int(event.get("duration_ms", 0) or 0),
                "status": str(event.get("status", "ok") or "ok"),
                "input_ref": _jdumps(event.get("input_ref", {})),
                "output_ref": _jdumps(event.get("output_ref", {})),
                "metrics": _jdumps(event.get("metrics", {})),
                "error_type": str(event.get("error_type", "") or ""),
                "error_message": str(event.get("error_message", "") or "")[:2000],
            }
        )
    return rows


def log_trace_events(
    events: list[dict[str, Any]],
    *,
    turn_id: str,
    trace_id: str,
    engine=None,
    env_label: str | None = None,
) -> None:
    """Persist per-stage RAG trace events and export compact OTEL spans.

    This function is best effort. Database errors and OTEL export failures are
    logged but do not propagate to the caller.
    """
    if not events:
        return
    env = _env_label(env_label)
    normalized_trace_id = normalize_trace_id(trace_id)
    rows = _prepare_trace_event_rows(turn_id=turn_id, trace_id=normalized_trace_id, events=events, env_label=env)

    if engine and rows:
        try:
            from sqlalchemy import text

            sql = _build_trace_event_upsert_sql()
            with engine.connect() as conn:
                conn.execute(text(sql), rows)
                conn.commit()
        except Exception as exc:
            logger.warning("PostgreSQL trace-event log failed: %s", exc)

    export_events_to_otel(turn_id=turn_id, trace_id=normalized_trace_id, events=events, env_label=env)
