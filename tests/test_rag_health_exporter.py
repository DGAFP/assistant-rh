from __future__ import annotations

import pytest
from assistant_rh_data_engineering.jobs import rag_health_exporter as exporter


def _sample(samples: list[exporter.MetricSample], name: str, **labels: str) -> exporter.MetricSample:
    for sample in samples:
        if sample.name != name:
            continue
        if all(sample.labels.get(key) == value for key, value in labels.items()):
            return sample
    raise AssertionError(f"Missing metric {name} with labels {labels}")


def test_render_prometheus_escapes_labels() -> None:
    text = exporter.render_prometheus(
        [
            exporter.MetricSample(
                "assistant_rh_rag_documents_total",
                {"env": "staging", "source": 'service "public"'},
                2,
            )
        ]
    )

    assert "# HELP assistant_rh_rag_documents_total" in text
    assert 'source="service \\"public\\""' in text
    assert text.endswith("\n")


def test_collector_sets_statement_timeout_with_set_config() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query: str, params: tuple[str, ...]) -> None:
            calls.append((query, params))

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    collector = exporter.RagHealthCollector(env_label="staging", statement_timeout_ms=20_000)
    collector._set_statement_timeout(FakeConnection())

    assert calls == [("SELECT set_config('statement_timeout', %s, false)", ("20000",))]


def test_missing_reference_check_casts_join_columns_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    collector = exporter.RagHealthCollector(env_label="staging")
    captured: dict[str, str] = {}

    def fake_fetch_one(conn, query: str, params=()):
        captured["query"] = query
        return 0

    monkeypatch.setattr(collector, "_fetch_one", fake_fetch_one)

    assert collector._count_missing_reference(object(), "rag_chunks_service_public", "source_document_id", "rag_documents", "doc_id") == 0
    assert 'target_table."doc_id"::text = source_table."source_document_id"::text' in captured["query"]


def test_trace_events_table_is_reported_as_trace_kind() -> None:
    assert exporter.RagHealthCollector._table_kind("rag_trace_events") == "traces"


def test_collector_emits_counts_coverage_and_integrity_for_available_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    collector = exporter.RagHealthCollector(env_label="production")
    columns = {
        "rag_documents": {"doc_id", "source", "updated_at"},
        "rag_sections": {"section_id", "doc_id"},
        "rag_chunks_service_public": {
            "hash_id",
            "source",
            "section_id",
            "source_document_id",
            "embedding_m3",
            "embedding_bge_scw",
            "updated_at",
        },
    }

    monkeypatch.setattr(collector, "_set_statement_timeout", lambda conn: None)
    monkeypatch.setattr(collector, "_load_columns", lambda conn: columns)
    monkeypatch.setattr(
        collector,
        "_count_by_column",
        lambda conn, table, column, default: {"service_public": 2 if table == "rag_documents" else 3},
    )
    monkeypatch.setattr(collector, "_count_rows", lambda conn, table: {"rag_sections": 2}.get(table, 0))
    monkeypatch.setattr(collector, "_count_sections_by_document_source", lambda conn: {"service_public": 2})
    direct_embedding_calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_count_embeddings(conn, table: str, embedding_columns: tuple[str, ...]) -> dict[str, tuple[int, int]]:
        direct_embedding_calls.append((table, embedding_columns))
        return {column: (3, 2) if column == "embedding_m3" else (3, 3) for column in embedding_columns}

    monkeypatch.setattr(collector, "_count_embeddings", fake_count_embeddings)
    monkeypatch.setattr(
        collector,
        "_count_missing_reference",
        lambda conn, source_table, source_column, target_table, target_column: 1 if source_column == "source_document_id" else 0,
    )
    monkeypatch.setattr(
        collector,
        "_max_epoch",
        lambda conn, table, column: 100.0 if table in {"rag_documents", "rag_chunks_service_public"} else None,
    )

    samples = collector.collect_from_connection(object())

    assert collector.env_label == "prod"
    assert _sample(samples, "assistant_rh_rag_table_present", table="rag_chunks_matte", kind="chunks").value == 0
    assert _sample(samples, "assistant_rh_rag_documents_total", source="service_public").value == 2
    assert _sample(samples, "assistant_rh_rag_chunks_total", table="rag_chunks_service_public", source="service_public").value == 3
    assert _sample(
        samples,
        "assistant_rh_rag_embedding_coverage_ratio",
        table="rag_chunks_service_public",
        column="embedding_m3",
        model="albert",
    ).value == pytest.approx(2 / 3)
    assert direct_embedding_calls == [("rag_chunks_service_public", ("embedding_m3", "embedding_bge_scw"))]


def test_collector_emits_trace_metrics_when_trace_table_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    collector = exporter.RagHealthCollector(env_label="staging")
    columns = {
        "rag_trace_events": {
            "turn_id",
            "env",
            "stage",
            "duration_ms",
            "status",
            "error_type",
            "error_message",
            "created_at",
        }
    }

    monkeypatch.setattr(collector, "_set_statement_timeout", lambda conn: None)
    monkeypatch.setattr(collector, "_load_columns", lambda conn: columns)
    monkeypatch.setattr(collector, "_count_recent_trace_turns", lambda conn: 4)
    monkeypatch.setattr(collector, "_trace_event_counts", lambda conn: [("retriever", "ok", 5), ("generator", "failed", 1)])
    monkeypatch.setattr(collector, "_trace_stage_duration_quantiles", lambda conn: [("retriever", "0.95", 0.42)])
    monkeypatch.setattr(collector, "_trace_error_counts", lambda conn: [("generator", "provider_error", 1)])
    monkeypatch.setattr(collector, "_trace_last_event_epoch", lambda conn: 100.0)

    samples = collector.collect_from_connection(object())

    assert _sample(samples, "assistant_rh_rag_trace_turns_24h_total").value == 4
    assert _sample(samples, "assistant_rh_rag_trace_events_24h_total", stage="retriever", status="ok").value == 5
    assert _sample(samples, "assistant_rh_rag_trace_stage_duration_seconds", stage="retriever", quantile="0.95").value == 0.42
    assert _sample(samples, "assistant_rh_rag_trace_errors_24h_total", stage="generator", error_type="provider_error").value == 1
    assert _sample(samples, "assistant_rh_rag_trace_last_event_timestamp_seconds").value == 100


def test_trace_metrics_noop_when_trace_table_is_partially_migrated() -> None:
    collector = exporter.RagHealthCollector(env_label="staging")
    columns = {
        "rag_trace_events": {
            "turn_id",
            "env",
            "stage",
            "duration_ms",
            "status",
            "error_type",
            "created_at",
        }
    }

    assert collector._trace_metrics(object(), columns, now=100.0) == []


def test_trace_queries_filter_by_collector_env() -> None:
    executed: list[tuple[str, tuple[str, ...]]] = []

    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, exc_type, exc, traceback) -> bool:
            return False

        def execute(self, query: str, params: tuple[str, ...] = ()) -> None:
            executed.append((query, params))

        def fetchone(self) -> tuple[int]:
            return (0,)

        def fetchall(self) -> list:
            return []

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    collector = exporter.RagHealthCollector(env_label="production")
    conn = FakeConnection()

    collector._count_recent_trace_turns(conn)
    collector._trace_event_counts(conn)
    collector._trace_stage_duration_quantiles(conn)
    collector._trace_error_counts(conn)
    collector._trace_last_event_epoch(conn)

    assert executed
    assert all('"env" = %s' in query for query, _params in executed)
    assert all(params == ("prod",) for _query, params in executed)


@pytest.mark.parametrize(
    ("table_spec", "columns"),
    [
        (
            exporter.DIRECT_CHUNK_TABLES[1],
            {"rag_chunks_service_public": {"source"}},
        ),
    ],
)
def test_chunk_metrics_emit_zero_for_empty_grouped_counts(
    table_spec: exporter.ChunkTable,
    columns: dict[str, set[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = exporter.RagHealthCollector(env_label="staging")
    monkeypatch.setattr(collector, "_count_by_column", lambda conn, table, column, default: {})

    samples = collector._chunk_metrics(object(), columns, table_spec)

    assert _sample(samples, "assistant_rh_rag_chunks_total", table=table_spec.table, source=table_spec.default_source).value == 0


def test_metrics_state_reports_failure_without_dropping_previous_samples() -> None:
    state = exporter.MetricsState("staging")
    state.record_success(
        [exporter.metric("assistant_rh_rag_documents_total", "staging", 2, source="service_public")],
        duration=0.1,
    )

    state.record_failure(RuntimeError("db down"), duration=0.2)
    text = state.render()

    assert 'assistant_rh_rag_documents_total{env="staging",source="service_public"} 2' in text
    assert 'assistant_rh_rag_last_poll_success{env="staging"} 0' in text
    assert 'assistant_rh_rag_poll_errors_total{env="staging"} 1' in text
    assert state.health_payload()["last_error"] == "db down"


def test_resolve_dsn_requires_monitoring_dsn_in_deployed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("RAG_HEALTH_POSTGRES_DSN", raising=False)

    with pytest.raises(RuntimeError, match="RAG_HEALTH_POSTGRES_DSN"):
        exporter.resolve_dsn("RAG_HEALTH_POSTGRES_DSN")
