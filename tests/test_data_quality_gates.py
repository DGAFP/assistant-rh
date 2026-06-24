from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from assistant_rh_data_engineering.jobs.quality_gates import build_parser, validate_requested_sources
from assistant_rh_data_engineering.quality_gates import evaluate_quality_gates, render_markdown_report, resolve_expected_ids
from assistant_rh_data_ingestion_cli.main import _resolve_command


class FakeQualityDatabase:
    def __init__(self) -> None:
        self.columns = {
            "rag_documents": {"short_id", "source", "doc_markdown", "updated_at"},
            "rag_sections": {"doc_id", "section_markdown"},
            "rag_chunks_service_public": {"short_id", "chunk_text", "updated_at", "embedding_m3", "embedding_bge_scw"},
        }
        self.row_counts = {"rag_documents": 2, "rag_chunks_service_public": 2}
        self.distinct_counts = {("rag_documents", "short_id"): 2, ("rag_chunks_service_public", "short_id"): 2}
        self.missing_ids: dict[tuple[str, str], list[str]] = {}
        self.blank_counts: dict[tuple[str, str], int] = {}
        self.timestamps = {
            ("rag_documents", "updated_at"): datetime.now(tz=UTC),
            ("rag_chunks_service_public", "updated_at"): datetime.now(tz=UTC),
        }
        self.section_rows = 2
        self.section_distinct = 2
        self.section_missing: list[str] = []
        self.section_blank_counts: dict[str, int] = {}

    def table_columns(self, table: str) -> set[str]:
        return self.columns.get(table, set())

    def row_count(self, table: str, source_filter: Any = None) -> int:
        return self.row_counts.get(table, 0)

    def distinct_expected_count(self, table: str, id_column: str, expected_ids: list[str], source_filter: Any = None) -> int:
        return self.distinct_counts.get((table, id_column), 0)

    def missing_expected_ids(self, table: str, id_column: str, expected_ids: list[str], source_filter: Any = None) -> list[str]:
        return self.missing_ids.get((table, id_column), [])

    def blank_text_count(self, table: str, text_column: str, source_filter: Any = None) -> int:
        return self.blank_counts.get((table, text_column), 0)

    def max_timestamp(self, table: str, column: str, source_filter: Any = None) -> datetime | None:
        return self.timestamps.get((table, column))

    def section_row_count_for_documents(self, expected_ids: list[str], document_source: Any) -> int:
        return self.section_rows

    def section_distinct_document_count(self, expected_ids: list[str], document_source: Any) -> int:
        return self.section_distinct

    def missing_section_document_ids(self, expected_ids: list[str], document_source: Any) -> list[str]:
        return self.section_missing

    def blank_section_text_count(self, text_column: str, expected_ids: list[str], document_source: Any) -> int:
        return self.section_blank_counts.get(text_column, 0)


def test_quality_gates_pass_for_selected_source(tmp_path: Path) -> None:
    config = _write_config(tmp_path)

    report = evaluate_quality_gates(
        FakeQualityDatabase(),
        config,
        repo_root=tmp_path,
        target_env="staging",
        sources=["service_public"],
        blocking=True,
    )

    assert report["status"] == "pass"
    assert report["summary"]["fail"] == 0
    assert {check["table"] for check in report["checks"]} == {"rag_documents", "rag_sections", "rag_chunks_service_public"}


def test_quality_gates_report_missing_expected_ids(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    db = FakeQualityDatabase()
    db.distinct_counts[("rag_chunks_service_public", "short_id")] = 1
    db.missing_ids[("rag_chunks_service_public", "short_id")] = ["F2"]

    report = evaluate_quality_gates(
        db,
        config,
        repo_root=tmp_path,
        target_env="staging",
        sources=["service_public"],
        blocking=False,
    )

    failed = [check for check in report["checks"] if check["status"] == "fail"]
    assert report["status"] == "fail"
    assert report["blocking"] is False
    assert failed[0]["details"]["missing_sample"] == ["F2"]
    assert "Data quality gate: FAIL" in render_markdown_report(report)


def test_quality_gates_fail_on_stale_freshness(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    db = FakeQualityDatabase()
    stale = datetime.now(tz=UTC) - timedelta(hours=3)
    db.timestamps[("rag_documents", "updated_at")] = stale

    report = evaluate_quality_gates(
        db,
        config,
        repo_root=tmp_path,
        target_env="staging",
        sources=["service_public"],
        blocking=True,
    )

    failures = [check for check in report["checks"] if check["status"] == "fail"]
    assert any(check["table"] == "rag_documents" and check["check"] == "freshness" for check in failures)


def test_quality_gates_does_not_emit_embedding_checks(tmp_path: Path) -> None:
    config = _write_config(tmp_path)

    report = evaluate_quality_gates(
        FakeQualityDatabase(),
        config,
        repo_root=tmp_path,
        target_env="staging",
        sources=["service_public"],
        blocking=True,
    )

    # Embedding coverage is delegated to `data-ingestion embeddings --check-only`.
    assert not any(check["check"].startswith("embedding_coverage") for check in report["checks"])


def test_quality_gates_cli_route_is_registered() -> None:
    resolved = _resolve_command(["quality", "gates", "--target-env", "staging"])

    assert resolved is not None
    spec, job_args = resolved
    assert spec.module == "assistant_rh_data_engineering.jobs.quality_gates"
    assert job_args == ["--target-env", "staging"]


def test_resolve_expected_ids_reports_missing_config() -> None:
    with pytest.raises(ValueError, match="missing 'expected_ids'"):
        resolve_expected_ids(Path.cwd(), "custom", {})


def test_resolve_expected_ids_requires_path_and_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing 'path'"):
        resolve_expected_ids(tmp_path, "custom", {"expected_ids": {"field": "ids"}})

    with pytest.raises(ValueError, match="missing 'field'"):
        resolve_expected_ids(tmp_path, "custom", {"expected_ids": {"path": "ids.json"}})


def test_resolve_expected_ids_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Expected IDs file not found"):
        resolve_expected_ids(tmp_path, "custom", {"expected_ids": {"path": "missing.json", "field": "ids"}})


def test_resolve_expected_ids_reports_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="Failed to parse JSON"):
        resolve_expected_ids(tmp_path, "custom", {"expected_ids": {"path": "ids.json", "field": "ids"}})


def test_resolve_expected_ids_rejects_null_json(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    path.write_text("null", encoding="utf-8")

    with pytest.raises(ValueError, match="resolved to null"):
        resolve_expected_ids(tmp_path, "custom", {"expected_ids": {"path": "ids.json", "field": "ids"}})


def test_quality_gates_cli_accepts_config_driven_sources() -> None:
    parser = build_parser()
    args = parser.parse_args(["--target-env", "staging", "--source", "custom"])

    validate_requested_sources(parser, args, {"sources": {"custom": {}}})

    assert args.source == ["custom"]


def test_quality_gates_cli_rejects_unknown_config_sources() -> None:
    parser = build_parser()
    args = parser.parse_args(["--target-env", "staging", "--source", "unknown"])

    with pytest.raises(SystemExit):
        validate_requested_sources(parser, args, {"sources": {"service_public": {}}})


def test_quality_gates_cli_has_no_embedding_flags() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--target-env", "staging", "--include-embeddings"])


def _write_config(tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "ids.json").write_text(json.dumps({"fiche_ids": ["F1", "F2"]}), encoding="utf-8")
    return {
        "version": 1,
        "sources": {
            "service_public": {
                "expected_ids": {"path": "ids.json", "field": "fiche_ids"},
                "source_filter": {"column": "source", "value": "service_public"},
                "freshness_max_age_hours": {"staging": 1, "prod": 1},
                "tables": [
                    {
                        "name": "rag_documents",
                        "id_column": "short_id",
                        "apply_source_filter": True,
                        "text_columns": ["doc_markdown"],
                        "freshness_column": "updated_at",
                    },
                    {
                        "name": "rag_sections",
                        "kind": "sections",
                        "text_columns": ["section_markdown"],
                    },
                    {
                        "name": "rag_chunks_service_public",
                        "id_column": "short_id",
                        "text_columns": ["chunk_text"],
                        "freshness_column": "updated_at",
                    },
                ],
            }
        },
    }
