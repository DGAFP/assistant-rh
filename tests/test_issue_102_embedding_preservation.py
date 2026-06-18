from __future__ import annotations

from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn

    def execute(self, query, params=None):
        self.conn.executed_queries.append(query)

    def executemany(self, query, rows):
        self.conn.executed_queries.append(query)
        self.conn.rows = list(rows)

    def fetchall(self):
        return [(col, udt, length) for col, (udt, length) in self.conn.column_types.items()]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class FakeConnection:
    def __init__(self, column_types: dict[str, tuple[str, int | None]]):
        self.column_types = column_types
        self.executed_queries = []
        self.rows = []
        self.committed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


def render(query) -> str:
    return query.as_string({}).replace('"', "").replace("\n", " ")


def test_upsert_preserves_selected_columns_when_incoming_value_is_null() -> None:
    writer = ServicePublicDbWriter(schema="public")
    conn = FakeConnection(
        {
            "chunk_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
        }
    )

    writer._upsert(
        conn,
        "rag_chunks_dgafp",
        [
            {
                "chunk_id": "LEGIARTI_0",
                "chunk_text": "Texte",
                "embedding_m3": None,
                "embedding_bge_scw": None,
            }
        ],
        ["chunk_id"],
        preserve_on_null_cols=["embedding_m3", "embedding_bge_scw"],
    )

    sql = render(conn.executed_queries[-1])
    assert "COALESCE(EXCLUDED.embedding_m3, rag_chunks_dgafp.embedding_m3)" in sql
    assert "COALESCE(EXCLUDED.embedding_bge_scw, rag_chunks_dgafp.embedding_bge_scw)" in sql
    assert "chunk_text = EXCLUDED.chunk_text" in sql


def test_upsert_keeps_default_plain_excluded_behavior() -> None:
    writer = ServicePublicDbWriter(schema="public")
    conn = FakeConnection({"chunk_id": ("varchar", 64), "embedding_m3": ("vector", None)})

    writer._upsert(conn, "rag_chunks_dgafp", [{"chunk_id": "id", "embedding_m3": None}], ["chunk_id"])

    sql = render(conn.executed_queries[-1])
    assert "embedding_m3 = EXCLUDED.embedding_m3" in sql
    assert "COALESCE" not in sql


def test_upsert_ignores_preserve_columns_that_are_not_updated() -> None:
    writer = ServicePublicDbWriter(schema="public")
    conn = FakeConnection({"chunk_id": ("varchar", 64), "embedding_m3": ("vector", None)})

    writer._upsert(
        conn,
        "rag_chunks_dgafp",
        [{"chunk_id": "id", "embedding_m3": None}],
        ["chunk_id"],
        preserve_on_null_cols=["chunk_id", "embedding_does_not_exist"],
    )

    sql = render(conn.executed_queries[-1])
    assert "embedding_does_not_exist" not in sql
    assert "COALESCE" not in sql


def test_legifrance_legacy_writer_guards_existing_embedding_columns(monkeypatch) -> None:
    from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter

    captured = {}
    conn = FakeConnection(
        {
            "chunk_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
            "embedding_qwen3": ("vector", None),
        }
    )

    monkeypatch.setattr(LegifranceDbWriter, "_connect", lambda self: conn)
    monkeypatch.setattr(LegifranceDbWriter, "ensure_legacy_target_table", lambda self: None)

    def fake_upsert(self, conn, table, rows, conflict_cols, **kwargs):
        captured.update(table=table, conflict_cols=conflict_cols, preserve=kwargs["preserve_on_null_cols"])
        return len(rows)

    monkeypatch.setattr(LegifranceDbWriter, "_upsert", fake_upsert)

    count = LegifranceDbWriter().upsert_legacy_chunks([{"chunk_id": "LEGIARTI_0", "chunk_text": "Texte", "_targets": ["legacy"]}])

    assert count == 1
    assert captured["table"] == "rag_chunks_dgafp"
    assert captured["conflict_cols"] == ["chunk_id"]
    assert captured["preserve"] == ["embedding_m3", "embedding_bge_scw", "embedding_qwen3"]


def test_legifrance_modern_writer_guards_existing_embedding_columns(monkeypatch) -> None:
    from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter

    captured = {}
    conn = FakeConnection(
        {
            "hash_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
        }
    )

    monkeypatch.setattr(LegifranceDbWriter, "_connect", lambda self: conn)
    monkeypatch.setattr(LegifranceDbWriter, "ensure_modern_target_table", lambda self: None)

    def fake_upsert(self, conn, table, rows, conflict_cols, **kwargs):
        captured.update(table=table, conflict_cols=conflict_cols, preserve=kwargs["preserve_on_null_cols"])
        return len(rows)

    monkeypatch.setattr(LegifranceDbWriter, "_upsert", fake_upsert)

    count = LegifranceDbWriter().upsert_modern_chunks([{"hash_id": "hash", "chunk_text": "Texte", "_targets": ["modern"]}])

    assert count == 1
    assert captured["table"] == "rag_chunks_legifrance"
    assert captured["conflict_cols"] == ["hash_id"]
    assert captured["preserve"] == ["embedding_m3", "embedding_bge_scw"]
