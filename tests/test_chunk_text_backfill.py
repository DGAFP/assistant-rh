"""Offline unit tests for the chunk_text backfill job (issue #177).

No DB connection: query builders are rendered to SQL strings and the column
guard is exercised with a fake connection that records what was executed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from assistant_rh_data_engineering.jobs import chunk_text_backfill as job

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_MAIN_PATH = REPO_ROOT / "apps" / "data-ingestion-cli" / "src" / "assistant_rh_data_ingestion_cli" / "main.py"


def _load_cli_main():
    if "data_ingestion_cli_main" in sys.modules:
        return sys.modules["data_ingestion_cli_main"]
    spec = importlib.util.spec_from_file_location("data_ingestion_cli_main", CLI_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["data_ingestion_cli_main"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# --- Fake connection (no DB) -------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._result: tuple | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: object, params: tuple | None = None) -> None:
        if isinstance(query, str):
            if "information_schema.tables" in query:
                _schema, table = params  # type: ignore[misc]
                self._result = (1,) if table in self._conn.existing_tables else None
            elif "information_schema.columns" in query:
                _schema, _table, column = params  # type: ignore[misc]
                self._result = (1,) if column in self._conn.existing_columns else None
            else:  # pragma: no cover - defensive
                self._result = None
        else:
            # The only composed query reaching the cursor is the coverage aggregate.
            self._conn.coverage_executed = True
            self._result = (10, 4, 4)  # total, empty_target, backfillable

    def fetchone(self) -> tuple | None:
        return self._result


class _FakeConn:
    def __init__(self, existing_tables: set[str], existing_columns: set[str]) -> None:
        self.existing_tables = set(existing_tables)
        self.existing_columns = set(existing_columns)
        self.coverage_executed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


# --- Parser & defaults -------------------------------------------------------


def test_parser_defaults_target_matte_chunk_text() -> None:
    args = job.build_parser().parse_args([])
    assert args.dsn_env == "SCW_POSTGRES_DSN"
    assert args.schema == "public"
    assert args.source_col == "text"
    assert args.target_col == "chunk_text"
    assert args.tables is None  # resolved to DEFAULT_TABLES in main()
    assert args.check_only is False
    assert args.coverage_min_pct is None


def test_dry_run_is_alias_for_check_only() -> None:
    assert job.build_parser().parse_args(["--dry-run"]).check_only is True


def test_default_table_is_matte() -> None:
    assert job.DEFAULT_TABLES == ("rag_chunks_matte",)


# --- Coverage query is SELECT-only ------------------------------------------


def test_coverage_query_is_select_only() -> None:
    rendered = job.build_coverage_query("public", "rag_chunks_matte", "text", "chunk_text").as_string(None)
    upper = rendered.upper()
    assert upper.lstrip().startswith("SELECT")
    for forbidden in ("UPDATE", "INSERT", "DELETE", "DROP", "ALTER", "TRUNCATE"):
        assert forbidden not in upper, f"coverage query must stay read-only, found {forbidden}: {rendered}"
    assert 'FROM "public"."rag_chunks_matte"' in rendered


# --- Update query is idempotent & guarded -----------------------------------


def test_update_query_only_fills_empty_target_from_present_source() -> None:
    rendered = job.build_update_query("public", "rag_chunks_matte", "text", "chunk_text", with_updated_at=False, limit=None).as_string(None)
    assert rendered.startswith('UPDATE "public"."rag_chunks_matte" SET "chunk_text" = "text"')
    # Idempotence: only rows whose target is empty AND whose source is present are touched.
    assert "LENGTH(TRIM(COALESCE(\"chunk_text\", ''))) = 0" in rendered
    assert "LENGTH(TRIM(COALESCE(\"text\", ''))) > 0" in rendered
    assert "updated_at" not in rendered


def test_update_query_sets_updated_at_when_present() -> None:
    rendered = job.build_update_query("public", "rag_chunks_matte", "text", "chunk_text", with_updated_at=True, limit=None).as_string(None)
    assert "updated_at = CURRENT_TIMESTAMP" in rendered


def test_update_query_bounds_with_ctid_when_limited() -> None:
    rendered = job.build_update_query("public", "rag_chunks_matte", "text", "chunk_text", with_updated_at=False, limit=5).as_string(None)
    assert "ctid IN (SELECT ctid FROM" in rendered
    assert "LIMIT 5" in rendered


# --- Column guard ------------------------------------------------------------


def test_audit_skips_table_when_target_column_missing() -> None:
    conn = _FakeConn(existing_tables={"rag_chunks_matte"}, existing_columns={"text"})  # chunk_text absent
    report = job.audit_text_coverage(conn, "public", ["rag_chunks_matte"], "text", "chunk_text")

    assert conn.coverage_executed is False  # no aggregate run on a table missing the column
    assert report["missing_columns"] == {"rag_chunks_matte": ["chunk_text"]}
    assert "rag_chunks_matte" not in report["tables"]


def test_audit_reports_coverage_when_columns_present() -> None:
    conn = _FakeConn(existing_tables={"rag_chunks_matte"}, existing_columns={"text", "chunk_text"})
    report = job.audit_text_coverage(conn, "public", ["rag_chunks_matte"], "text", "chunk_text")

    assert conn.coverage_executed is True
    stats = report["tables"]["rag_chunks_matte"]
    assert stats == {
        "total": 10,
        "empty_target": 4,
        "non_empty_target": 6,
        "backfillable": 4,
        "is_empty": False,
        "coverage_pct": 60.0,
    }


def test_audit_records_missing_table() -> None:
    conn = _FakeConn(existing_tables=set(), existing_columns=set())
    report = job.audit_text_coverage(conn, "public", ["rag_chunks_matte"], "text", "chunk_text")
    assert report["missing_tables"] == ["rag_chunks_matte"]


# --- evaluate_coverage_report ------------------------------------------------


def test_evaluate_passes_on_full_coverage() -> None:
    report = {
        "target_col": "chunk_text",
        "tables": {"rag_chunks_matte": {"total": 959, "non_empty_target": 959, "backfillable": 0, "is_empty": False}},
        "missing_tables": [],
        "missing_columns": {},
    }
    exit_code, problems = job.evaluate_coverage_report(report, coverage_min_pct=None)
    assert exit_code == 0
    assert problems == []


def test_evaluate_fails_below_threshold() -> None:
    report = {
        "target_col": "chunk_text",
        "tables": {"rag_chunks_matte": {"total": 959, "non_empty_target": 137, "backfillable": 822, "is_empty": False}},
        "missing_tables": [],
        "missing_columns": {},
    }
    exit_code, problems = job.evaluate_coverage_report(report, coverage_min_pct=None)
    assert exit_code == 1
    assert any("rag_chunks_matte.chunk_text" in p and "822" in p for p in problems)


def test_evaluate_flags_missing_table_and_columns() -> None:
    report = {
        "target_col": "chunk_text",
        "tables": {},
        "missing_tables": ["rag_chunks_ghost"],
        "missing_columns": {"rag_chunks_matte": ["chunk_text"]},
    }
    exit_code, problems = job.evaluate_coverage_report(report, coverage_min_pct=None)
    assert exit_code == 1
    assert any("rag_chunks_ghost" in p for p in problems)
    assert any("rag_chunks_matte" in p and "chunk_text" in p for p in problems)


def test_evaluate_flags_empty_table() -> None:
    report = {
        "target_col": "chunk_text",
        "tables": {"rag_chunks_matte": {"total": 0, "non_empty_target": 0, "backfillable": 0, "is_empty": True}},
        "missing_tables": [],
        "missing_columns": {},
    }
    exit_code, problems = job.evaluate_coverage_report(report, coverage_min_pct=None)
    assert exit_code == 1
    assert any("table vide" in p for p in problems)


# --- CLI wiring --------------------------------------------------------------


def test_cli_resolves_chunks_backfill_text_to_job_module() -> None:
    cli = _load_cli_main()
    resolved = cli._resolve_command(["chunks", "backfill-text"])

    assert resolved is not None
    spec, job_args = resolved
    assert spec.module == "assistant_rh_data_engineering.jobs.chunk_text_backfill"
    assert job_args == []


def test_cli_chunks_backfill_text_passthrough_preserves_user_args() -> None:
    cli = _load_cli_main()
    resolved = cli._resolve_command(["chunks", "backfill-text", "--dsn-env", "SCW_POSTGRES_DSN_STAGING", "--check-only"])

    assert resolved is not None
    _, job_args = resolved
    assert job_args == ["--dsn-env", "SCW_POSTGRES_DSN_STAGING", "--check-only"]
