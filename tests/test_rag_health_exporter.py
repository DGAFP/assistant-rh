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
        "rag_chunks_test": {"chunk_id", "doc_id", "section_id"},
        "rag_chunk_embeddings": {"chunk_id", "embedding_raw", "embedding_bge"},
    }

    monkeypatch.setattr(collector, "_set_statement_timeout", lambda conn: None)
    monkeypatch.setattr(collector, "_load_columns", lambda conn: columns)
    monkeypatch.setattr(
        collector,
        "_count_by_column",
        lambda conn, table, column, default: {"service_public": 2 if table == "rag_documents" else 3},
    )
    monkeypatch.setattr(collector, "_count_rows", lambda conn, table: {"rag_sections": 2, "rag_chunks_test": 1}.get(table, 0))
    monkeypatch.setattr(collector, "_count_sections_by_document_source", lambda conn: {"service_public": 2})
    monkeypatch.setattr(collector, "_count_chunks_test_by_document_source", lambda conn: {"test": 1})
    monkeypatch.setattr(
        collector,
        "_count_embedding",
        lambda conn, table, column: (3, 2) if column == "embedding_m3" else (3, 3),
    )
    monkeypatch.setattr(
        collector,
        "_count_chunks_test_embedding",
        lambda conn, column: (1, 1) if column == "embedding_raw" else (1, 0),
    )
    monkeypatch.setattr(
        collector,
        "_count_missing_reference",
        lambda conn, source_table, source_column, target_table, target_column: 1 if source_column == "source_document_id" else 0,
    )
    monkeypatch.setattr(collector, "_count_chunks_without_embedding_row", lambda conn: 0)
    monkeypatch.setattr(collector, "_count_embeddings_without_chunk", lambda conn: 1)
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
    assert (
        _sample(
            samples,
            "assistant_rh_rag_embeddings_missing_total",
            table="rag_chunks_test",
            column="embedding_bge",
            model="bge_scaleway",
        ).value
        == 1
    )
    assert (
        _sample(
            samples,
            "assistant_rh_rag_embedding_column_present",
            table="rag_chunks_test",
            column="embedding",
            model="albert_context",
        ).value
        == 0
    )
    assert (
        _sample(
            samples,
            "assistant_rh_rag_integrity_issues_total",
            table="rag_chunk_embeddings",
            reason="embedding_without_chunk",
        ).value
        == 1
    )


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
