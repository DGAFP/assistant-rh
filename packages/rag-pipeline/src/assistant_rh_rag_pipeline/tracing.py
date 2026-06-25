"""RAG trace helpers for per-turn diagnostics and optional OTLP export.

The pipeline stores bounded, structured trace events in ``PipelineResult``.
The chat logger persists them in Postgres and may export compact OpenInference
spans to an OpenTelemetry HTTP endpoint. Export failures are intentionally
non-blocking: tracing must never break the user-facing RAG path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = "2026-06-25"
DEFAULT_PREVIEW_CHARS = 500
MAX_OTEL_JSON_CHARS = 16_000

OPENINFERENCE_SPAN_KIND_BY_STAGE = {
    "query-processor": "CHAIN",
    "retriever": "RETRIEVER",
    "section-aggregator": "RERANKER",
    "context-selector": "CHAIN",
    "context-builder": "CHAIN",
    "generator": "LLM",
}


def normalize_trace_id(value: str | None = None) -> str:
    """Return a valid 32-character lowercase hex OpenTelemetry trace id."""
    raw = (value or "").strip().lower().replace("-", "")
    if len(raw) == 32 and all(ch in "0123456789abcdef" for ch in raw) and raw != "0" * 32:
        return raw
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return uuid.uuid4().hex


def bounded_preview(value: Any, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """Normalize and bound text before it is stored in trace payloads."""
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max(max_chars, 0)
    return f"{text[: max_chars - 3]}..."


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def make_trace_event(
    *,
    stage: str,
    duration_ms: float = 0.0,
    status: str = "ok",
    attempt_name: str = "",
    input_ref: dict[str, Any] | None = None,
    output_ref: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    error_type: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    """Create a JSON-serializable trace event."""
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "stage": stage,
        "attempt_name": attempt_name,
        "duration_ms": int(round(duration_ms or 0)),
        "status": status or "ok",
        "input_ref": input_ref or {},
        "output_ref": output_ref or {},
        "metrics": metrics or {},
        "error_type": error_type or "",
        "error_message": bounded_preview(error_message, 2_000),
    }


def chunk_ref(chunk: Any) -> dict[str, Any]:
    metadata = getattr(chunk, "metadata", {}) or {}
    document_id = (
        metadata.get("source_document_id")
        or metadata.get("doc_id")
        or metadata.get("doc_short_id")
        or metadata.get("short_id")
        or metadata.get("cid")
    )
    heading = (
        metadata.get("heading")
        or metadata.get("matched_heading")
        or metadata.get("doc_title")
        or metadata.get("source_name")
        or metadata.get("full_title")
        or metadata.get("title")
        or metadata.get("number")
        or ""
    )
    return {
        "chunk_id": str(getattr(chunk, "chunk_id", "") or ""),
        "table": str(getattr(chunk, "table_source", "") or ""),
        "score": round(float(getattr(chunk, "score", 0.0) or 0.0), 4),
        "section_id": str(getattr(chunk, "section_id", "") or ""),
        "document_id": str(document_id or ""),
        "heading": bounded_preview(heading, 160),
        "retrieval_path": metadata.get("retrieval_path", "chunk"),
        "preview": bounded_preview(getattr(chunk, "text", ""), DEFAULT_PREVIEW_CHARS),
    }


def section_ref(section: Any, *, include_chunks: bool = True) -> dict[str, Any]:
    metadata = getattr(section, "metadata", {}) or {}
    chunks = getattr(section, "chunks", []) or []
    document_id = getattr(section, "document_id", "") or metadata.get("doc_id") or metadata.get("doc_short_id") or metadata.get("cid")
    heading = (
        getattr(section, "heading", "")
        or metadata.get("doc_title")
        or metadata.get("full_title")
        or metadata.get("title")
        or metadata.get("number")
        or ""
    )
    out = {
        "section_id": str(getattr(section, "section_id", "") or ""),
        "document_id": str(document_id or ""),
        "heading": bounded_preview(heading, 180),
        "publisher": str(getattr(section, "publisher", "") or ""),
        "score": round(float(getattr(section, "score", 0.0) or 0.0), 4),
        "chunk_count": len(chunks),
        "tokens": int(getattr(section, "token_estimate", 0) or 0),
        "preview": bounded_preview(getattr(section, "markdown", ""), DEFAULT_PREVIEW_CHARS),
    }
    if include_chunks:
        out["chunks"] = [chunk_ref(chunk) for chunk in chunks]
    return out


def context_item_ref(item: Any) -> dict[str, Any]:
    metadata = getattr(item, "metadata", {}) or {}
    document_id = metadata.get("doc_id") or metadata.get("doc_short_id") or metadata.get("cid")
    heading = (
        getattr(item, "heading", "")
        or metadata.get("doc_title")
        or metadata.get("full_title")
        or metadata.get("title")
        or metadata.get("number")
        or ""
    )
    return {
        "section_id": str(getattr(item, "section_id", "") or ""),
        "document_id": str(document_id or ""),
        "heading": bounded_preview(heading, 180),
        "publisher": str(getattr(item, "publisher", "") or ""),
        "score": round(float(getattr(item, "score", 0.0) or 0.0), 4),
        "tokens": int(getattr(item, "token_estimate", 0) or 0),
        "is_doc_entire": bool(metadata.get("is_doc_entire", False)),
        "preview": bounded_preview(getattr(item, "content", ""), DEFAULT_PREVIEW_CHARS),
    }


def selected_chunk_ids(event: dict[str, Any]) -> list[str]:
    """Extract chunk ids from a trace event for searchable OTEL attributes."""
    ids: list[str] = []
    output_ref = event.get("output_ref") if isinstance(event, dict) else {}
    if not isinstance(output_ref, dict):
        return ids
    for key in ("retrieved_chunks", "aggregated_sections", "context_items"):
        items = output_ref.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            chunk_id = str(item.get("chunk_id", "") or "")
            if chunk_id:
                ids.append(chunk_id)
            for chunk in item.get("chunks", []) if isinstance(item.get("chunks"), list) else []:
                if isinstance(chunk, dict) and chunk.get("chunk_id"):
                    ids.append(str(chunk["chunk_id"]))
    return sorted(set(ids))


def source_tables(event: dict[str, Any]) -> list[str]:
    output_ref = event.get("output_ref") if isinstance(event, dict) else {}
    input_ref = event.get("input_ref") if isinstance(event, dict) else {}
    tables: list[str] = []
    if isinstance(input_ref, dict):
        tables.extend(str(t) for t in input_ref.get("tables_searched", []) if t)
    if isinstance(output_ref, dict):
        for key in ("retrieved_chunks", "aggregated_sections", "context_items"):
            items = output_ref.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    table = item.get("table") or item.get("publisher")
                    if table:
                        tables.append(str(table))
                    for chunk in item.get("chunks", []) if isinstance(item.get("chunks"), list) else []:
                        if isinstance(chunk, dict) and chunk.get("table"):
                            tables.append(str(chunk["table"]))
    return sorted(set(tables))


def export_events_to_otel(
    *,
    turn_id: str,
    trace_id: str,
    events: list[dict[str, Any]],
    env_label: str = "",
) -> None:
    """Export trace events to an OTLP HTTP endpoint when enabled.

    This uses the OTLP/HTTP JSON encoding directly to avoid adding a hard
    dependency on OpenTelemetry packages to the production image. It is best
    effort and logs warnings only.
    """
    if not _tracing_enabled():
        return
    endpoint = _resolve_otlp_traces_endpoint()
    if not endpoint:
        logger.warning("RAG tracing enabled but no OTLP endpoint is configured")
        return

    try:
        payload = _build_otlp_payload(
            turn_id=turn_id,
            trace_id=normalize_trace_id(trace_id),
            events=events,
            env_label=env_label,
        )
        threading.Thread(target=_send_otlp_payload, args=(endpoint, payload), daemon=True).start()
    except Exception as exc:
        logger.warning("RAG trace OTLP export failed: %s", exc)


def _send_otlp_payload(endpoint: str, payload: dict[str, Any]) -> None:
    try:
        requests.post(endpoint, headers=_otlp_headers(), data=json.dumps(payload), timeout=3).raise_for_status()
    except Exception as exc:
        logger.warning("RAG trace OTLP export failed: %s", exc)


def _tracing_enabled() -> bool:
    return (os.getenv("RAG_TRACING_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_otlp_traces_endpoint() -> str:
    explicit = (os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    if explicit:
        return explicit
    base = (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def _otlp_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    raw = (os.getenv("OTEL_EXPORTER_OTLP_HEADERS") or "").strip()
    if not raw:
        return headers
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def _build_otlp_payload(
    *,
    turn_id: str,
    trace_id: str,
    events: list[dict[str, Any]],
    env_label: str,
) -> dict[str, Any]:
    service_name = os.getenv("OTEL_SERVICE_NAME", "assistant-rh")
    now_ns = int(time.time() * 1_000_000_000)
    total_duration_ns = sum(max(0, int(event.get("duration_ms", 0) or 0)) for event in events) * 1_000_000
    cursor_ns = now_ns - total_duration_ns
    root_span_id = _span_id(trace_id, "root")

    spans = [
        {
            "traceId": trace_id,
            "spanId": root_span_id,
            "name": "rag.pipeline",
            "kind": 1,
            "startTimeUnixNano": str(cursor_ns),
            "endTimeUnixNano": str(now_ns),
            "attributes": _attrs(
                {
                    "openinference.span.kind": "CHAIN",
                    "rag.turn_id": turn_id,
                    "rag.trace_id": trace_id,
                    "rag.event_count": len(events),
                }
            ),
            "status": {"code": 1},
        }
    ]

    event_cursor = cursor_ns
    for index, event in enumerate(events):
        duration_ns = max(0, int(event.get("duration_ms", 0) or 0)) * 1_000_000
        end_ns = event_cursor + duration_ns
        status = str(event.get("status", "ok") or "ok")
        stage = str(event.get("stage", "unknown") or "unknown")
        attrs = {
            "openinference.span.kind": OPENINFERENCE_SPAN_KIND_BY_STAGE.get(stage, "CHAIN"),
            "rag.turn_id": turn_id,
            "rag.trace_id": trace_id,
            "rag.stage": stage,
            "rag.attempt_name": str(event.get("attempt_name", "") or ""),
            "rag.status": status,
            "rag.duration_ms": int(event.get("duration_ms", 0) or 0),
            "rag.chunk_ids": ",".join(selected_chunk_ids(event)),
            "rag.source_tables": ",".join(source_tables(event)),
            "input.value": _safe_json_for_otel(event.get("input_ref", {})),
            "output.value": _safe_json_for_otel(_compact_output_ref(event.get("output_ref", {}))),
            "input.mime_type": "application/json",
            "output.mime_type": "application/json",
        }
        error_message = str(event.get("error_message", "") or "")
        if error_message:
            attrs["error.message"] = error_message
        spans.append(
            {
                "traceId": trace_id,
                "spanId": _span_id(trace_id, str(index)),
                "parentSpanId": root_span_id,
                "name": f"rag.{stage}",
                "kind": 1,
                "startTimeUnixNano": str(event_cursor),
                "endTimeUnixNano": str(max(end_ns, event_cursor + 1)),
                "attributes": _attrs(attrs),
                "status": {"code": 2 if status in {"failed", "error"} else 1, "message": error_message},
            }
        )
        event_cursor = end_ns

    resource_attrs = {
        "service.name": service_name,
        "deployment.environment": env_label or os.getenv("APP_ENV", ""),
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attrs(resource_attrs)},
                "scopeSpans": [{"scope": {"name": "assistant-rh-rag-pipeline"}, "spans": spans}],
            }
        ]
    }


def _span_id(trace_id: str, seed: str) -> str:
    return hashlib.sha256(f"{trace_id}:{seed}".encode("utf-8")).hexdigest()[:16]


def _safe_json_for_otel(value: Any) -> str:
    text = json_dumps(value)
    if len(text) <= MAX_OTEL_JSON_CHARS:
        return text
    return text[: MAX_OTEL_JSON_CHARS - 3] + "..."


def _compact_output_ref(value: Any) -> Any:
    """Remove chunk/content previews from OTEL attrs while preserving ids/scores."""
    if isinstance(value, list):
        return [_compact_output_ref(item) for item in value]
    if not isinstance(value, dict):
        return value
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key == "preview":
            continue
        compact[key] = _compact_output_ref(item)
    return compact


def _attrs(values: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, value in values.items():
        if value is None:
            continue
        out.append({"key": key, "value": _otlp_value(value)})
    return out


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}
