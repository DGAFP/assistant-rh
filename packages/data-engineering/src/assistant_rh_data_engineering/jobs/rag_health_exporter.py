from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

import psycopg
from assistant_rh_shared import get_dsn
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MetricSample:
    name: str
    labels: dict[str, str]
    value: float


@dataclass(frozen=True)
class EmbeddingColumn:
    column: str
    model: str


@dataclass(frozen=True)
class ChunkTable:
    table: str
    default_source: str
    source_column: str | None = None
    section_column: str | None = None
    document_column: str | None = None
    embeddings: tuple[EmbeddingColumn, ...] = (
        EmbeddingColumn("embedding_m3", "albert"),
        EmbeddingColumn("embedding_bge_scw", "bge_scaleway"),
    )


DIRECT_CHUNK_TABLES: tuple[ChunkTable, ...] = (
    ChunkTable("rag_chunks_matte", "matte", source_column="source", section_column="section_id", document_column="source_document_id"),
    ChunkTable("rag_chunks_mi", "mi", source_column="source", section_column="section_id", document_column="source_document_id"),
    ChunkTable("rag_chunks_masa", "masa", source_column="source", section_column="section_id", document_column="source_document_id"),
    ChunkTable(
        "rag_chunks_service_public",
        "service_public",
        source_column="source",
        section_column="section_id",
        document_column="source_document_id",
    ),
    ChunkTable("rag_chunks_dgafp", "dgafp", section_column=None, document_column=None),
    ChunkTable(
        "rag_chunks_legifrance",
        "dgafp",
        source_column="source",
        section_column="section_id",
        document_column="source_document_id",
    ),
    ChunkTable("rag_chunks_rgrh", "rgrh", source_column="source", section_column="section_id", document_column="source_document_id"),
)

EXPECTED_TABLES = (
    "rag_documents",
    "rag_sections",
    "rag_trace_events",
    *(table.table for table in DIRECT_CHUNK_TABLES),
)

METRIC_HELP = {
    "assistant_rh_rag_table_present": "Whether an expected RAG table exists in the configured schema.",
    "assistant_rh_rag_documents_total": "Total RAG documents by source.",
    "assistant_rh_rag_sections_total": "Total RAG sections by source.",
    "assistant_rh_rag_chunks_total": "Total RAG chunks by table and source.",
    "assistant_rh_rag_embedding_column_present": "Whether an expected embedding column exists on its table.",
    "assistant_rh_rag_embeddings_present_total": "Rows with a non-null embedding by table, column, and model.",
    "assistant_rh_rag_embeddings_missing_total": "Rows missing an embedding by table, column, and model.",
    "assistant_rh_rag_embedding_coverage_ratio": "Non-null embedding ratio by table, column, and model.",
    "assistant_rh_rag_integrity_issues_total": "Detected RAG data integrity issues by table and reason.",
    "assistant_rh_rag_table_last_update_timestamp_seconds": "Unix timestamp of the latest updated_at or created_at value for a table.",
    "assistant_rh_rag_table_freshness_seconds": "Seconds since the latest updated_at or created_at value for a table.",
    "assistant_rh_rag_trace_turns_24h_total": "Distinct RAG turns with trace events created in the last 24 hours.",
    "assistant_rh_rag_trace_events_24h_total": "RAG trace events created in the last 24 hours by stage and status.",
    "assistant_rh_rag_trace_stage_duration_seconds": "RAG trace stage duration quantiles over events created in the last 24 hours.",
    "assistant_rh_rag_trace_errors_24h_total": "RAG trace error or fallback events created in the last 24 hours by stage and error type.",
    "assistant_rh_rag_trace_last_event_timestamp_seconds": "Unix timestamp of the latest RAG trace event.",
    "assistant_rh_rag_trace_freshness_seconds": "Seconds since the latest RAG trace event.",
    "assistant_rh_rag_last_poll_success": "Whether the last database polling attempt succeeded.",
    "assistant_rh_rag_last_poll_timestamp_seconds": "Unix timestamp of the last database polling attempt.",
    "assistant_rh_rag_last_successful_poll_timestamp_seconds": "Unix timestamp of the last successful database polling attempt.",
    "assistant_rh_rag_poll_duration_seconds": "Duration of the last database polling attempt.",
    "assistant_rh_rag_poll_errors_total": "Total database polling errors since exporter startup.",
}

METRIC_TYPES = {
    "assistant_rh_rag_poll_errors_total": "counter",
}


def quote_identifier(value: str) -> str:
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f'"{value}"'


def normalize_env_label(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "production":
        return "prod"
    if normalized in {"staging", "prod"}:
        return normalized
    raise ValueError("--env-label must be one of: staging, prod, production")


def normalize_label_value(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    return text or default


def metric(name: str, env_label: str, value: float, **labels: str) -> MetricSample:
    return MetricSample(name, {"env": env_label, **labels}, float(value))


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.12g}"


def render_prometheus(samples: Iterable[MetricSample]) -> str:
    samples = list(samples)
    lines: list[str] = []
    seen: set[str] = set()
    for sample in samples:
        if sample.name not in seen:
            seen.add(sample.name)
            lines.append(f"# HELP {sample.name} {METRIC_HELP.get(sample.name, sample.name)}")
            lines.append(f"# TYPE {sample.name} {METRIC_TYPES.get(sample.name, 'gauge')}")
        labels = ",".join(f'{key}="{_escape_label(value)}"' for key, value in sorted(sample.labels.items()))
        suffix = f"{{{labels}}}" if labels else ""
        lines.append(f"{sample.name}{suffix} {_format_value(sample.value)}")
    return "\n".join(lines) + "\n"


class RagHealthCollector:
    def __init__(self, *, env_label: str, schema: str = "public", statement_timeout_ms: int = 20_000):
        self.env_label = normalize_env_label(env_label)
        self.schema = schema
        self.statement_timeout_ms = statement_timeout_ms
        self.schema_sql = quote_identifier(schema)

    def collect(self, dsn: str) -> list[MetricSample]:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            return self.collect_from_connection(conn)

    def collect_from_connection(self, conn: psycopg.Connection) -> list[MetricSample]:
        started = time.time()
        self._set_statement_timeout(conn)
        columns = self._load_columns(conn)
        now = time.time()
        samples: list[MetricSample] = []

        for table in EXPECTED_TABLES:
            samples.append(
                metric(
                    "assistant_rh_rag_table_present",
                    self.env_label,
                    1 if table in columns else 0,
                    table=table,
                    kind=self._table_kind(table),
                )
            )

        samples.extend(self._document_metrics(conn, columns))
        samples.extend(self._section_metrics(conn, columns))

        for table_spec in DIRECT_CHUNK_TABLES:
            samples.extend(self._chunk_metrics(conn, columns, table_spec))
            samples.extend(self._direct_embedding_metrics(conn, columns, table_spec))
            samples.extend(self._integrity_metrics(conn, columns, table_spec))

        samples.extend(self._trace_metrics(conn, columns, now))

        for table in EXPECTED_TABLES:
            if table == "rag_trace_events":
                continue
            samples.extend(self._freshness_metrics(conn, columns, table, now))

        samples.append(metric("assistant_rh_rag_poll_duration_seconds", self.env_label, time.time() - started))
        return samples

    def _set_statement_timeout(self, conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, false)", (str(max(0, self.statement_timeout_ms)),))

    def _load_columns(self, conn: psycopg.Connection) -> dict[str, set[str]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = ANY(%s)
                """,
                (self.schema, list(EXPECTED_TABLES)),
            )
            rows = cur.fetchall()
        columns: dict[str, set[str]] = {}
        for table_name, column_name in rows:
            columns.setdefault(str(table_name), set()).add(str(column_name))
        return columns

    @staticmethod
    def _table_kind(table: str) -> str:
        if table == "rag_documents":
            return "documents"
        if table == "rag_sections":
            return "sections"
        if table == "rag_trace_events":
            return "traces"
        return "chunks"

    def _document_metrics(self, conn: psycopg.Connection, columns: dict[str, set[str]]) -> list[MetricSample]:
        if "rag_documents" not in columns:
            return []
        if "source" in columns["rag_documents"]:
            counts = self._count_by_column(conn, "rag_documents", "source", "unknown")
        elif "publisher" in columns["rag_documents"]:
            counts = self._count_by_column(conn, "rag_documents", "publisher", "unknown")
        else:
            counts = {"unknown": self._count_rows(conn, "rag_documents")}
        return [metric("assistant_rh_rag_documents_total", self.env_label, count, source=source) for source, count in counts.items()]

    def _section_metrics(self, conn: psycopg.Connection, columns: dict[str, set[str]]) -> list[MetricSample]:
        if "rag_sections" not in columns:
            return []
        can_join_documents = (
            "rag_documents" in columns
            and "doc_id" in columns["rag_sections"]
            and "doc_id" in columns["rag_documents"]
            and "source" in columns["rag_documents"]
        )
        if not can_join_documents:
            counts = {"unknown": self._count_rows(conn, "rag_sections")}
        else:
            counts = self._count_sections_by_document_source(conn)
        return [metric("assistant_rh_rag_sections_total", self.env_label, count, source=source) for source, count in counts.items()]

    def _chunk_metrics(self, conn: psycopg.Connection, columns: dict[str, set[str]], table_spec: ChunkTable) -> list[MetricSample]:
        if table_spec.table not in columns:
            return []
        if table_spec.source_column and table_spec.source_column in columns[table_spec.table]:
            counts = self._count_by_column(conn, table_spec.table, table_spec.source_column, table_spec.default_source)
        else:
            counts = {table_spec.default_source: self._count_rows(conn, table_spec.table)}
        if not counts:
            counts = {table_spec.default_source: 0}
        return [
            metric("assistant_rh_rag_chunks_total", self.env_label, count, table=table_spec.table, source=source) for source, count in counts.items()
        ]

    def _direct_embedding_metrics(
        self,
        conn: psycopg.Connection,
        columns: dict[str, set[str]],
        table_spec: ChunkTable,
    ) -> list[MetricSample]:
        if table_spec.table not in columns:
            return []
        samples: list[MetricSample] = []
        present_embeddings: list[EmbeddingColumn] = []
        for embedding in table_spec.embeddings:
            labels = {"table": table_spec.table, "column": embedding.column, "model": embedding.model}
            if embedding.column not in columns[table_spec.table]:
                samples.append(metric("assistant_rh_rag_embedding_column_present", self.env_label, 0, **labels))
                continue
            present_embeddings.append(embedding)
            samples.append(metric("assistant_rh_rag_embedding_column_present", self.env_label, 1, **labels))

        if not present_embeddings:
            return samples

        counts = self._count_embeddings(conn, table_spec.table, tuple(embedding.column for embedding in present_embeddings))
        for embedding in present_embeddings:
            labels = {"table": table_spec.table, "column": embedding.column, "model": embedding.model}
            total, present = counts.get(embedding.column, (0, 0))
            missing = max(0, total - present)
            samples.append(metric("assistant_rh_rag_embeddings_present_total", self.env_label, present, **labels))
            samples.append(metric("assistant_rh_rag_embeddings_missing_total", self.env_label, missing, **labels))
            samples.append(metric("assistant_rh_rag_embedding_coverage_ratio", self.env_label, present / total if total else 1, **labels))
        return samples

    def _integrity_metrics(self, conn: psycopg.Connection, columns: dict[str, set[str]], table_spec: ChunkTable) -> list[MetricSample]:
        if table_spec.table not in columns:
            return []
        samples: list[MetricSample] = []
        if table_spec.section_column and table_spec.section_column in columns[table_spec.table] and "rag_sections" in columns:
            count = self._count_missing_reference(
                conn,
                table_spec.table,
                table_spec.section_column,
                "rag_sections",
                "section_id",
            )
            samples.append(metric("assistant_rh_rag_integrity_issues_total", self.env_label, count, table=table_spec.table, reason="missing_section"))
        if table_spec.document_column and table_spec.document_column in columns[table_spec.table] and "rag_documents" in columns:
            count = self._count_missing_reference(
                conn,
                table_spec.table,
                table_spec.document_column,
                "rag_documents",
                "doc_id",
            )
            samples.append(
                metric("assistant_rh_rag_integrity_issues_total", self.env_label, count, table=table_spec.table, reason="missing_document")
            )
        return samples

    def _trace_metrics(self, conn: psycopg.Connection, columns: dict[str, set[str]], now: float) -> list[MetricSample]:
        expected_columns = {"turn_id", "env", "stage", "duration_ms", "status", "error_type", "error_message", "created_at"}
        if "rag_trace_events" not in columns or not expected_columns.issubset(columns["rag_trace_events"]):
            return []

        samples: list[MetricSample] = []
        samples.append(
            metric(
                "assistant_rh_rag_trace_turns_24h_total",
                self.env_label,
                self._count_recent_trace_turns(conn),
            )
        )

        for stage, status, count in self._trace_event_counts(conn):
            samples.append(
                metric(
                    "assistant_rh_rag_trace_events_24h_total",
                    self.env_label,
                    count,
                    stage=stage,
                    status=status,
                )
            )

        for stage, quantile, duration_seconds in self._trace_stage_duration_quantiles(conn):
            samples.append(
                metric(
                    "assistant_rh_rag_trace_stage_duration_seconds",
                    self.env_label,
                    duration_seconds,
                    stage=stage,
                    quantile=quantile,
                )
            )

        for stage, error_type, count in self._trace_error_counts(conn):
            samples.append(
                metric(
                    "assistant_rh_rag_trace_errors_24h_total",
                    self.env_label,
                    count,
                    stage=stage,
                    error_type=error_type,
                )
            )

        timestamp = self._trace_last_event_epoch(conn)
        if timestamp is not None:
            samples.append(metric("assistant_rh_rag_trace_last_event_timestamp_seconds", self.env_label, timestamp))
            samples.append(metric("assistant_rh_rag_trace_freshness_seconds", self.env_label, max(0, now - timestamp)))
        return samples

    def _freshness_metrics(
        self,
        conn: psycopg.Connection,
        columns: dict[str, set[str]],
        table: str,
        now: float,
    ) -> list[MetricSample]:
        if table not in columns:
            return []
        freshness_column = "updated_at" if "updated_at" in columns[table] else "created_at" if "created_at" in columns[table] else ""
        if not freshness_column:
            return []
        timestamp = self._max_epoch(conn, table, freshness_column)
        if timestamp is None:
            return []
        return [
            metric("assistant_rh_rag_table_last_update_timestamp_seconds", self.env_label, timestamp, table=table),
            metric("assistant_rh_rag_table_freshness_seconds", self.env_label, max(0, now - timestamp), table=table),
        ]

    def _count_rows(self, conn: psycopg.Connection, table: str) -> int:
        query = f"SELECT COUNT(*) FROM {self.schema_sql}.{quote_identifier(table)}"
        return int(self._fetch_one(conn, query) or 0)

    def _count_by_column(self, conn: psycopg.Connection, table: str, column: str, default_source: str) -> dict[str, int]:
        query = f"""
            SELECT COALESCE(NULLIF(TRIM({quote_identifier(column)}::text), ''), %s) AS source, COUNT(*)
            FROM {self.schema_sql}.{quote_identifier(table)}
            GROUP BY 1
        """
        return self._fetch_count_map(conn, query, (default_source,), default_source)

    def _count_sections_by_document_source(self, conn: psycopg.Connection) -> dict[str, int]:
        query = f"""
            SELECT COALESCE(NULLIF(TRIM(d."source"::text), ''), %s) AS source, COUNT(*)
            FROM {self.schema_sql}."rag_sections" s
            LEFT JOIN {self.schema_sql}."rag_documents" d ON d."doc_id"::text = s."doc_id"::text
            GROUP BY 1
        """
        return self._fetch_count_map(conn, query, ("unknown",), "unknown")

    def _count_embeddings(self, conn: psycopg.Connection, table: str, embedding_columns: tuple[str, ...]) -> dict[str, tuple[int, int]]:
        if not embedding_columns:
            return {}
        select_parts = ["COUNT(*) AS total"]
        select_parts.extend(f"COUNT({quote_identifier(column)}) AS present_{index}" for index, column in enumerate(embedding_columns))
        query = f"SELECT {', '.join(select_parts)} FROM {self.schema_sql}.{quote_identifier(table)}"
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
        if not row:
            return {column: (0, 0) for column in embedding_columns}
        total = int(row[0] or 0)
        return {column: (total, int(row[index + 1] or 0)) for index, column in enumerate(embedding_columns)}

    def _count_missing_reference(
        self,
        conn: psycopg.Connection,
        source_table: str,
        source_column: str,
        target_table: str,
        target_column: str,
    ) -> int:
        query = f"""
            SELECT COUNT(*)
            FROM {self.schema_sql}.{quote_identifier(source_table)} source_table
            LEFT JOIN {self.schema_sql}.{quote_identifier(target_table)} target_table
              ON target_table.{quote_identifier(target_column)}::text = source_table.{quote_identifier(source_column)}::text
            WHERE source_table.{quote_identifier(source_column)} IS NOT NULL
              AND target_table.{quote_identifier(target_column)} IS NULL
        """
        return int(self._fetch_one(conn, query) or 0)

    def _count_recent_trace_turns(self, conn: psycopg.Connection) -> int:
        query = f"""
            SELECT COUNT(DISTINCT "turn_id")
            FROM {self.schema_sql}."rag_trace_events"
            WHERE "env" = %s
              AND "created_at" >= now() - interval '24 hours'
        """
        return int(self._fetch_one(conn, query, (self.env_label,)) or 0)

    def _trace_event_counts(self, conn: psycopg.Connection) -> list[tuple[str, str, int]]:
        query = f"""
            SELECT
                COALESCE(NULLIF(TRIM("stage"), ''), 'unknown') AS stage,
                COALESCE(NULLIF(TRIM("status"), ''), 'unknown') AS status,
                COUNT(*) AS count
            FROM {self.schema_sql}."rag_trace_events"
            WHERE "env" = %s
              AND "created_at" >= now() - interval '24 hours'
            GROUP BY 1, 2
        """
        with conn.cursor() as cur:
            cur.execute(query, (self.env_label,))
            rows = cur.fetchall()
        return [(normalize_label_value(stage), normalize_label_value(status), int(count or 0)) for stage, status, count in rows]

    def _trace_stage_duration_quantiles(self, conn: psycopg.Connection) -> list[tuple[str, str, float]]:
        query = f"""
            SELECT
                COALESCE(NULLIF(TRIM("stage"), ''), 'unknown') AS stage,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY GREATEST("duration_ms", 0)) / 1000.0 AS p50_seconds,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY GREATEST("duration_ms", 0)) / 1000.0 AS p95_seconds
            FROM {self.schema_sql}."rag_trace_events"
            WHERE "env" = %s
              AND "created_at" >= now() - interval '24 hours'
              AND "duration_ms" IS NOT NULL
            GROUP BY 1
        """
        with conn.cursor() as cur:
            cur.execute(query, (self.env_label,))
            rows = cur.fetchall()
        quantiles: list[tuple[str, str, float]] = []
        for stage, p50_seconds, p95_seconds in rows:
            normalized_stage = normalize_label_value(stage)
            if p50_seconds is not None:
                quantiles.append((normalized_stage, "0.50", float(p50_seconds)))
            if p95_seconds is not None:
                quantiles.append((normalized_stage, "0.95", float(p95_seconds)))
        return quantiles

    def _trace_error_counts(self, conn: psycopg.Connection) -> list[tuple[str, str, int]]:
        query = f"""
            SELECT
                COALESCE(NULLIF(TRIM("stage"), ''), 'unknown') AS stage,
                COALESCE(NULLIF(TRIM("error_type"), ''), NULLIF(TRIM("status"), ''), 'unknown') AS error_type,
                COUNT(*) AS count
            FROM {self.schema_sql}."rag_trace_events"
            WHERE "env" = %s
              AND "created_at" >= now() - interval '24 hours'
              AND (
                "error_type" <> ''
                OR "error_message" <> ''
                OR "status" NOT IN ('ok', 'success')
              )
            GROUP BY 1, 2
        """
        with conn.cursor() as cur:
            cur.execute(query, (self.env_label,))
            rows = cur.fetchall()
        return [(normalize_label_value(stage), normalize_label_value(error_type), int(count or 0)) for stage, error_type, count in rows]

    def _trace_last_event_epoch(self, conn: psycopg.Connection) -> float | None:
        query = f"""
            SELECT EXTRACT(EPOCH FROM MAX("created_at"))
            FROM {self.schema_sql}."rag_trace_events"
            WHERE "env" = %s
        """
        value = self._fetch_one(conn, query, (self.env_label,))
        return float(value) if value is not None else None

    def _max_epoch(self, conn: psycopg.Connection, table: str, column: str) -> float | None:
        query = f"SELECT EXTRACT(EPOCH FROM MAX({quote_identifier(column)})) FROM {self.schema_sql}.{quote_identifier(table)}"
        value = self._fetch_one(conn, query)
        return float(value) if value is not None else None

    @staticmethod
    def _fetch_one(conn: psycopg.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        return row[0] if row else None

    @staticmethod
    def _fetch_count_map(
        conn: psycopg.Connection,
        query: str,
        params: tuple[Any, ...],
        default_source: str,
    ) -> dict[str, int]:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        counts: dict[str, int] = {}
        for source, count in rows:
            counts[normalize_label_value(source, default_source)] = int(count or 0)
        return counts


class MetricsState:
    def __init__(self, env_label: str):
        self.env_label = env_label
        self._lock = threading.Lock()
        self._data_samples: list[MetricSample] = []
        self._last_poll_timestamp = 0.0
        self._last_successful_poll_timestamp = 0.0
        self._last_duration = 0.0
        self._last_success = 0.0
        self._poll_errors = 0.0
        self._last_error = ""

    def record_success(self, samples: list[MetricSample], duration: float) -> None:
        now = time.time()
        with self._lock:
            self._data_samples = [sample for sample in samples if sample.name != "assistant_rh_rag_poll_duration_seconds"]
            self._last_poll_timestamp = now
            self._last_successful_poll_timestamp = now
            self._last_duration = duration
            self._last_success = 1.0
            self._last_error = ""

    def record_failure(self, exc: Exception, duration: float) -> None:
        logger.exception("RAG health polling failed: %s", exc)
        with self._lock:
            self._last_poll_timestamp = time.time()
            self._last_duration = duration
            self._last_success = 0.0
            self._poll_errors += 1.0
            self._last_error = str(exc)

    def render(self) -> str:
        with self._lock:
            samples = [
                *self._data_samples,
                metric("assistant_rh_rag_last_poll_success", self.env_label, self._last_success),
                metric("assistant_rh_rag_last_poll_timestamp_seconds", self.env_label, self._last_poll_timestamp),
                metric("assistant_rh_rag_last_successful_poll_timestamp_seconds", self.env_label, self._last_successful_poll_timestamp),
                metric("assistant_rh_rag_poll_duration_seconds", self.env_label, self._last_duration),
                metric("assistant_rh_rag_poll_errors_total", self.env_label, self._poll_errors),
            ]
        return render_prometheus(samples)

    def health_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "last_poll_success": bool(self._last_success),
                "last_poll_timestamp": self._last_poll_timestamp,
                "last_successful_poll_timestamp": self._last_successful_poll_timestamp,
                "last_error": self._last_error,
            }


def poll_forever(collector: RagHealthCollector, state: MetricsState, dsn: str, interval_seconds: int, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        poll_once(collector, state, dsn)
        stop_event.wait(interval_seconds)


def poll_once(collector: RagHealthCollector, state: MetricsState, dsn: str) -> None:
    started = time.time()
    try:
        samples = collector.collect(dsn)
    except Exception as exc:  # pragma: no cover - covered through state behavior in tests.
        state.record_failure(exc, time.time() - started)
        return
    state.record_success(samples, time.time() - started)


def make_handler(state: MetricsState) -> type[BaseHTTPRequestHandler]:
    class RagHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
            if self.path == "/metrics":
                body = state.render().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/healthz":
                body = (json.dumps(state.health_payload(), ensure_ascii=False) + "\n").encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("HTTP %s - " + fmt, self.address_string(), *args)

    return RagHealthHandler


def resolve_dsn(dsn_env: str) -> str:
    dsn = os.getenv(dsn_env, "").strip()
    if dsn:
        return dsn
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env in {"", "local", "dev", "development"}:
        return get_dsn()
    raise RuntimeError(f"{dsn_env} is required when APP_ENV={app_env!r}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Expose read-only RAG corpus health metrics for Prometheus/Grafana.")
    parser.add_argument("--env-label", default="")
    parser.add_argument("--schema", default="")
    parser.add_argument("--dsn-env", default="RAG_HEALTH_POSTGRES_DSN")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int)
    parser.add_argument("--poll-interval-seconds", type=int)
    parser.add_argument("--statement-timeout-ms", type=int)
    parser.add_argument("--once", action="store_true", help="Collect once, print Prometheus metrics to stdout, and exit.")
    parser.add_argument("--log-level", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(Path(args.env_file))
    log_level = args.log_level or os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(level=getattr(logging, str(log_level).upper(), logging.INFO), format="%(levelname)s:%(name)s:%(message)s")

    env_label = args.env_label or os.getenv("RAG_HEALTH_ENV_LABEL") or os.getenv("APP_SCALEWAY_ENV") or os.getenv("APP_ENV") or "local"
    schema = args.schema or os.getenv("RAG_HEALTH_SCHEMA", "public")
    host = args.host or os.getenv("RAG_HEALTH_EXPORTER_HOST", "0.0.0.0")
    port = args.port if args.port is not None else int(os.getenv("RAG_HEALTH_EXPORTER_PORT", "9108"))
    poll_interval_seconds = (
        args.poll_interval_seconds if args.poll_interval_seconds is not None else int(os.getenv("DB_HEALTH_POLL_INTERVAL_SECONDS", "300"))
    )
    statement_timeout_ms = (
        args.statement_timeout_ms if args.statement_timeout_ms is not None else int(os.getenv("RAG_HEALTH_STATEMENT_TIMEOUT_MS", "20000"))
    )

    collector = RagHealthCollector(env_label=env_label, schema=schema, statement_timeout_ms=statement_timeout_ms)
    dsn = resolve_dsn(args.dsn_env)

    if args.once:
        print(render_prometheus(collector.collect(dsn)), end="")
        return 0

    state = MetricsState(collector.env_label)
    poll_once(collector, state, dsn)
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_forever,
        args=(collector, state, dsn, max(15, poll_interval_seconds), stop_event),
        daemon=True,
    )
    poll_thread.start()

    server = ThreadingHTTPServer((host, port), make_handler(state))
    logger.info("Serving RAG health metrics on http://%s:%s/metrics", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping RAG health exporter.")
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
