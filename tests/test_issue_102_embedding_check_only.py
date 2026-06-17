from __future__ import annotations

import json
from types import SimpleNamespace

from assistant_rh_data_engineering.jobs import embeddings_backfill


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn

    def execute(self, query, params=None):
        self.conn.queries.append(query)
        self.conn.params.append(params)

    def fetchone(self):
        return self.conn.fetchone_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class FakeConnection:
    def __init__(self, fetchone_results):
        self.fetchone_results = list(fetchone_results)
        self.queries = []
        self.params = []
        self.committed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


def test_check_only_reports_coverage_without_writes() -> None:
    conn = FakeConnection([(1,), (10, 7, 2, 1)])
    report = embeddings_backfill.audit_embedding_coverage(
        conn,
        "public",
        [
            {
                "table": "rag_chunks_dgafp",
                "id_column": "chunk_id",
                "text_column": "chunk_text",
                "embeddings": [{"column": "embedding_m3", "algorithm": "m3"}],
            }
        ],
    )

    stats = report["tables"]["rag_chunks_dgafp"]["embedding_m3"]
    assert stats == {"total": 10, "non_null": 7, "missing_with_text": 2, "empty_text": 1, "coverage_pct": 70.0}
    assert conn.committed is False
    assert all("UPDATE" not in str(query).upper() for query in conn.queries)


def test_coverage_threshold_returns_non_zero_for_gaps() -> None:
    report = {
        "missing_tables": [],
        "tables": {"rag_chunks_dgafp": {"embedding_m3": {"coverage_pct": 70.0}}},
    }

    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=95)

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
                {"column": "embedding_bge_scw", "algorithm": "bge"},
            ],
        }
    ]

    filtered = embeddings_backfill.filter_table_specs(specs, only_table="rag_chunks_dgafp", only_column="embedding_m3")

    assert len(filtered) == 1
    assert filtered[0]["embeddings"] == [{"column": "embedding_m3", "algorithm": "m3"}]


def test_check_only_main_does_not_call_embedding_backfill(monkeypatch, tmp_path, capsys) -> None:
    config = tmp_path / "tables.json"
    config.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "table": "rag_chunks_dgafp",
                        "id_column": "chunk_id",
                        "text_column": "chunk_text",
                        "embeddings": [{"column": "embedding_m3", "algorithm": "m3"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    conn = FakeConnection([(1,), (10, 10, 0, 0)])
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(embeddings_backfill, "psycopg", SimpleNamespace(connect=lambda dsn: conn))
    monkeypatch.setattr(embeddings_backfill, "backfill_m3", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(embeddings_backfill, "backfill_bge_scaleway", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(
        "sys.argv",
        ["embeddings", "--config", str(config), "--check-only", "--coverage-min-pct", "100"],
    )

    assert embeddings_backfill.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_only"] is True
    assert payload["exit_code"] == 0
