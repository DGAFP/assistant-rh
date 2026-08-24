from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from assistant_rh_data_engineering.jobs import quality_gates as quality_gates_job
from assistant_rh_data_engineering.jobs.quality_gates import build_parser, validate_requested_sources
from assistant_rh_data_engineering.quality_gates import TableSnapshot, evaluate_quality_gates, render_markdown_report, resolve_expected_ids
from assistant_rh_data_ingestion_cli.main import _resolve_command


class FakeQualityDatabase:
    def __init__(self) -> None:
        self.columns = {
            "rag_documents": {"short_id", "source", "doc_markdown", "updated_at"},
            "rag_sections": {"doc_id", "section_markdown"},
            "rag_chunks_service_public": {"short_id", "chunk_text", "updated_at", "embedding_m3", "embedding_bge_scw"},
        }
        self.row_counts = {"rag_documents": 2, "rag_chunks_service_public": 2}
        self.missing_ids: dict[tuple[str, str], list[str]] = {}
        self.blank_counts: dict[tuple[str, str], int] = {}
        self.timestamps = {
            ("rag_documents", "updated_at"): datetime.now(tz=UTC),
            ("rag_chunks_service_public", "updated_at"): datetime.now(tz=UTC),
        }
        self.section_rows = 2
        self.section_missing: list[str] = []
        self.section_blank_counts: dict[str, int] = {}

    def inspect_table(self, table_config: dict[str, Any], expected_ids: list[str], source_filter: Any) -> TableSnapshot | None:
        table = str(table_config["name"])
        columns = self.columns.get(table, set())
        if not columns:
            return None
        if table_config.get("kind") == "sections":
            observed_ids = set(expected_ids) - set(self.section_missing)
            return TableSnapshot(
                columns=columns,
                row_count=self.section_rows,
                observed_ids=observed_ids,
                blank_counts={str(column): self.section_blank_counts.get(str(column), 0) for column in table_config.get("text_columns", [])},
                max_timestamp=None,
            )

        id_column = str(table_config.get("id_column") or "")
        missing_ids = set(self.missing_ids.get((table, id_column), []))
        observed_ids = set(expected_ids) - missing_ids if id_column and id_column in columns else None
        return TableSnapshot(
            columns=columns,
            row_count=self.row_counts.get(table, 0),
            observed_ids=observed_ids,
            blank_counts={str(column): self.blank_counts.get((table, str(column)), 0) for column in table_config.get("text_columns", [])},
            max_timestamp=self.timestamps.get((table, str(table_config.get("freshness_column") or ""))),
        )


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
    assert set(report["summary"]) == {"pass", "fail"}
    assert {check["table"] for check in report["checks"]} == {"rag_documents", "rag_sections", "rag_chunks_service_public"}


def test_quality_gates_report_missing_expected_ids(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    db = FakeQualityDatabase()
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


def test_quality_gates_support_absolute_minimum_for_non_article_table(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    config["sources"]["service_public"]["tables"] = [
        {
            "name": "rag_chunks_legifrance",
            "min_rows": 1,
            "text_columns": ["chunk_text"],
            "freshness_column": "updated_at",
        }
    ]
    db = FakeQualityDatabase()
    db.columns["rag_chunks_legifrance"] = {"chunk_text", "updated_at"}
    db.row_counts["rag_chunks_legifrance"] = 1
    db.timestamps[("rag_chunks_legifrance", "updated_at")] = datetime.now(tz=UTC)

    report = evaluate_quality_gates(
        db,
        config,
        repo_root=tmp_path,
        target_env="staging",
        sources=["service_public"],
        blocking=True,
    )

    min_rows = next(check for check in report["checks"] if check["check"] == "min_rows")
    assert report["status"] == "pass"
    assert min_rows["expected"] == 1
    assert not any(check["check"] == "expected_id_coverage" for check in report["checks"])


def test_legifrance_modern_table_does_not_apply_article_manifest() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config/data_quality_gates.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    modern_table = next(table for table in config["sources"]["legifrance"]["tables"] if table["name"] == "rag_chunks_legifrance")

    assert modern_table["min_rows"] == 1
    assert "id_column" not in modern_table
    assert "min_rows_per_expected_id" not in modern_table


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


def test_resolve_expected_ids_rejects_empty_id_list(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"ids": ["", "   "]}), encoding="utf-8")

    with pytest.raises(ValueError, match="contains no usable IDs"):
        resolve_expected_ids(tmp_path, "custom", {"expected_ids": {"path": "ids.json", "field": "ids"}})


def test_evaluate_quality_gates_rejects_empty_source_selection(tmp_path: Path) -> None:
    config = _write_config(tmp_path)

    with pytest.raises(ValueError, match="No sources selected"):
        evaluate_quality_gates(
            FakeQualityDatabase(),
            config,
            repo_root=tmp_path,
            target_env="staging",
            sources=[],
            blocking=True,
        )


def test_quality_gates_cli_requires_at_least_one_source() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--target-env", "staging"])


def test_run_gates_reports_config_errors_as_failing_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    config = _write_config(tmp_path)
    (tmp_path / "ids.json").write_text(json.dumps({"fiche_ids": []}), encoding="utf-8")
    monkeypatch.setattr(quality_gates_job.psycopg, "connect", lambda *args, **kwargs: FakeConnection())
    monkeypatch.setattr(quality_gates_job, "REPO_ROOT", tmp_path)
    parser = build_parser()
    args = parser.parse_args(["--target-env", "staging", "--source", "service_public", "--dsn", "postgresql://gate-test"])

    report = quality_gates_job.run_gates(args, config)

    assert report["status"] == "fail"
    assert report["checks"][0]["id"] == "config.configuration"
    assert "contains no usable IDs" in report["checks"][0]["message"]


def test_run_gates_keeps_database_error_detail_out_of_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError('connection failed: password authentication failed for user "gate"')

    config = _write_config(tmp_path)
    monkeypatch.setattr(quality_gates_job.psycopg, "connect", explode)
    parser = build_parser()
    args = parser.parse_args(["--target-env", "staging", "--source", "service_public", "--dsn", "postgresql://gate-test"])

    report = quality_gates_job.run_gates(args, config)

    assert report["status"] == "fail"
    assert report["checks"][0]["id"] == "database.connection"
    assert "OperationalError" in report["checks"][0]["message"]
    assert "password authentication" not in json.dumps(report)
    assert "password authentication" in capsys.readouterr().err


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
