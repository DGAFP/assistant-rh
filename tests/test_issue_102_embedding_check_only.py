from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from assistant_rh_data_engineering.jobs import embeddings_backfill


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self._next_kind: str | None = None

    def execute(self, query, params=None):
        text = str(query)
        self.conn.queries.append(text)
        self.conn.params.append(params)
        if "information_schema.tables" in text:
            self._next_kind = "table_exists"
        elif "FROM" in text.upper():
            self._next_kind = "coverage"
        else:
            self._next_kind = None

    def fetchone(self):
        if self._next_kind == "table_exists":
            return self.conn.table_exists_results.pop(0)
        if self._next_kind == "coverage":
            return self.conn.coverage_results.pop(0)
        raise AssertionError(f"FakeCursor.fetchone(): unexpected query kind {self._next_kind!r}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        table_exists_results: list[tuple | None] | None = None,
        coverage_results: list[tuple] | None = None,
    ) -> None:
        self.table_exists_results = list(table_exists_results or [])
        self.coverage_results = list(coverage_results or [])
        self.queries: list[str] = []
        self.params: list[object] = []
        self.committed = False
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


SPEC_ONE_COLUMN = [
    {
        "table": "rag_chunks_dgafp",
        "id_column": "chunk_id",
        "text_column": "chunk_text",
        "embeddings": [{"column": "embedding_m3", "algorithm": "m3"}],
    }
]


def test_check_only_reports_coverage_without_writes() -> None:
    conn = FakeConnection(
        table_exists_results=[(1,)],
        coverage_results=[(10, 7, 2, 1)],
    )

    report = embeddings_backfill.audit_embedding_coverage(conn, "public", SPEC_ONE_COLUMN)

    stats = report["tables"]["rag_chunks_dgafp"]["embedding_m3"]
    assert stats == {
        "total": 10,
        "non_null": 7,
        "missing_with_text": 2,
        "empty_text": 1,
        "is_empty": False,
        "coverage_pct": 70.0,
    }
    assert conn.committed is False
    assert all("UPDATE" not in query.upper() for query in conn.queries)


def test_empty_table_is_flagged_as_problem() -> None:
    conn = FakeConnection(
        table_exists_results=[(1,)],
        coverage_results=[(0, 0, 0, 0)],
    )

    report = embeddings_backfill.audit_embedding_coverage(conn, "public", SPEC_ONE_COLUMN)
    stats = report["tables"]["rag_chunks_dgafp"]["embedding_m3"]

    assert stats["is_empty"] is True
    assert stats["coverage_pct"] == 0.0

    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=100)
    assert exit_code == 1
    assert any("table vide" in problem for problem in problems)


def test_missing_table_is_flagged_as_problem() -> None:
    conn = FakeConnection(
        table_exists_results=[None],
        coverage_results=[],
    )

    report = embeddings_backfill.audit_embedding_coverage(conn, "public", SPEC_ONE_COLUMN)
    assert report["missing_tables"] == ["rag_chunks_dgafp"]
    assert report["tables"] == {}

    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=100)
    assert exit_code == 1
    assert any("Table absente" in problem for problem in problems)


def test_coverage_threshold_returns_non_zero_for_gaps() -> None:
    report = {
        "missing_tables": [],
        "tables": {"rag_chunks_dgafp": {"embedding_m3": {"total": 10, "non_null": 7, "coverage_pct": 70.0}}},
    }

    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=95)

    assert exit_code == 1
    assert "rag_chunks_dgafp.embedding_m3" in problems[0]


def test_threshold_uses_raw_coverage_not_rounded_display_value() -> None:
    # Raw 99.9851% rounds to 99.99 for display; threshold 99.99 must still flag it.
    report = {
        "missing_tables": [],
        "tables": {
            "rag_chunks_dgafp": {
                "embedding_m3": {
                    "total": 1_000_000,
                    "non_null": 999_851,
                    "coverage_pct": 99.99,
                }
            }
        },
    }

    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=99.99)

    assert exit_code == 1
    assert "rag_chunks_dgafp.embedding_m3" in problems[0]


def test_filter_table_specs_applies_table_and_column_filters() -> None:
    specs = [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [
                {"column": "embedding_m3", "algorithm": "m3"},
                {"column": "embedding_bge_scw", "algorithm": "bge_scaleway"},
            ],
        }
    ]

    filtered = embeddings_backfill.filter_table_specs(specs, only_table="rag_chunks_dgafp", only_column="embedding_m3")

    assert len(filtered) == 1
    assert filtered[0]["embeddings"] == [{"column": "embedding_m3", "algorithm": "m3"}]


def test_filter_table_specs_drops_embeddings_without_algorithm(caplog: pytest.LogCaptureFixture) -> None:
    specs = [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [
                {"column": "embedding_orphan"},
                {"column": "embedding_m3", "algorithm": "m3"},
            ],
        }
    ]

    with caplog.at_level("WARNING"):
        filtered = embeddings_backfill.filter_table_specs(specs, only_table=None, only_column=None)

    assert filtered == [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [{"column": "embedding_m3", "algorithm": "m3"}],
        }
    ]
    assert "Skipping embedding" in caplog.text


def test_only_column_no_match_raises_system_exit(monkeypatch, tmp_path) -> None:
    config = tmp_path / "tables.json"
    config.write_text(json.dumps({"tables": SPEC_ONE_COLUMN}), encoding="utf-8")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(
        "sys.argv",
        [
            "embeddings",
            "--config",
            str(config),
            "--check-only",
            "--only-column",
            "embedding_does_not_exist",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        embeddings_backfill.main()

    assert "embedding_does_not_exist" in str(exc_info.value)


def test_audit_handles_multiple_embedding_columns_per_table() -> None:
    specs = [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [
                {"column": "embedding_m3", "algorithm": "m3"},
                {"column": "embedding_bge_scw", "algorithm": "bge_scaleway"},
            ],
        }
    ]
    conn = FakeConnection(
        table_exists_results=[(1,)],
        coverage_results=[(10, 10, 0, 0), (10, 4, 6, 0)],
    )

    report = embeddings_backfill.audit_embedding_coverage(conn, "public", specs)

    columns = report["tables"]["rag_chunks_dgafp"]
    assert set(columns) == {"embedding_m3", "embedding_bge_scw"}
    assert columns["embedding_m3"]["coverage_pct"] == 100.0
    assert columns["embedding_bge_scw"]["coverage_pct"] == 40.0


def test_check_only_main_uses_autocommit_and_does_not_call_embedding_backfill(monkeypatch, tmp_path, capsys) -> None:
    config = tmp_path / "tables.json"
    config.write_text(json.dumps({"tables": SPEC_ONE_COLUMN}), encoding="utf-8")
    conn = FakeConnection(
        table_exists_results=[(1,)],
        coverage_results=[(10, 10, 0, 0)],
    )
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(embeddings_backfill, "psycopg", SimpleNamespace(connect=lambda dsn: conn))
    monkeypatch.setattr(
        embeddings_backfill,
        "backfill_m3",
        lambda *args, **kwargs: pytest.fail("backfill_m3 should not run in check-only mode"),
    )
    monkeypatch.setattr(
        embeddings_backfill,
        "backfill_bge_scaleway",
        lambda *args, **kwargs: pytest.fail("backfill_bge_scaleway should not run in check-only mode"),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["embeddings", "--config", str(config), "--check-only", "--coverage-min-pct", "100"],
    )

    assert embeddings_backfill.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_only"] is True
    assert payload["exit_code"] == 0
    assert payload["coverage"]["tables"]["rag_chunks_dgafp"]["embedding_m3"]["coverage_pct"] == 100.0
    assert conn.autocommit is True
    assert conn.committed is False


def test_dry_run_alias_triggers_check_only_path(monkeypatch, tmp_path, capsys) -> None:
    config = tmp_path / "tables.json"
    config.write_text(json.dumps({"tables": SPEC_ONE_COLUMN}), encoding="utf-8")
    conn = FakeConnection(
        table_exists_results=[(1,)],
        coverage_results=[(10, 10, 0, 0)],
    )
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(embeddings_backfill, "psycopg", SimpleNamespace(connect=lambda dsn: conn))
    monkeypatch.setattr(
        embeddings_backfill,
        "backfill_m3",
        lambda *args, **kwargs: pytest.fail("backfill_m3 should not run in --dry-run mode"),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["embeddings", "--config", str(config), "--dry-run"],
    )

    assert embeddings_backfill.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_only"] is True
    assert conn.autocommit is True
