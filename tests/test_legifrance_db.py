from __future__ import annotations

from typing import Any

import pytest
from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter


class _ScriptedCursor:
    def __init__(self, script: list[dict[str, Any]], calls: list[dict[str, Any]]):
        self._script = script
        self._calls = calls
        self._current: dict[str, Any] = {}

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: object, params: object = None) -> None:
        self._current = self._script.pop(0) if self._script else {}
        self._calls.append({"query": " ".join(repr(query).split()), "params": params})

    def fetchall(self) -> list[tuple]:
        return self._current.get("rows", [])

    def fetchone(self) -> tuple | None:
        rows = self._current.get("rows", [])
        return rows[0] if rows else None

    @property
    def rowcount(self) -> int:
        return int(self._current.get("rowcount", 0))


class _ScriptedConnection:
    def __init__(self, script: list[dict[str, Any]], calls: list[dict[str, Any]], events: list[str]):
        self._script = script
        self._calls = calls
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _ScriptedCursor:
        return _ScriptedCursor(self._script, self._calls)

    def commit(self) -> None:
        self._events.append("commit")


def _writer(script: list[dict[str, Any]]) -> tuple[LegifranceDbWriter, list[dict[str, Any]], list[str]]:
    calls: list[dict[str, Any]] = []
    events: list[str] = []
    writer = LegifranceDbWriter(schema="staging", dsn="postgresql://unused")
    connection = _ScriptedConnection(script, calls, events)
    writer._connect = lambda: connection  # type: ignore[method-assign]
    return writer, calls, events


def test_list_legifrance_corpus_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    writer, calls, events = _writer(
        [
            {"rows": [("staging.rag_documents",)]},
            {"rows": [("D1", "doc-1", "sha-1")]},
            {"rows": [("staging.rag_chunks_dgafp",)]},
            {"rows": [("D1", 2)]},
            {"rows": []},
        ]
    )
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: {"checksum": ("text", None)})
    monkeypatch.setattr(writer, "ensure_legacy_target_table", lambda: pytest.fail("read path attempted legacy DDL"))
    monkeypatch.setattr(writer, "ensure_modern_target_table", lambda: pytest.fail("read path attempted modern DDL"))

    corpus = writer.list_legifrance_corpus()

    assert corpus == {"D1": {"doc_id": "doc-1", "checksum": "sha-1", "nb_chunks": 2}}
    assert events == []
    assert all("CREATE" not in call["query"] and "ALTER" not in call["query"] for call in calls)


@pytest.mark.parametrize(
    ("method_name", "chunks", "message"),
    [
        ("ingest_article_bundle", [{"cid": "LEGIARTI1", "hash_id": "modern", "_targets": ["modern"]}], "chunk legacy"),
        ("ingest_texte_bundle", [{"short_id": "D1", "chunk_id": "legacy", "_targets": ["legacy"]}], "chunk moderne"),
    ],
)
def test_ingest_bundle_rejects_empty_projected_chunks_before_db(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    chunks: list[dict[str, Any]],
    message: str,
) -> None:
    writer, calls, events = _writer([])
    monkeypatch.setattr(writer, "ensure_legacy_target_table", lambda: pytest.fail("invalid bundle attempted legacy DDL"))
    monkeypatch.setattr(writer, "ensure_modern_target_table", lambda: pytest.fail("invalid bundle attempted modern DDL"))

    with pytest.raises(ValueError, match=message):
        getattr(writer, method_name)({"doc_id": "doc", "short_id": "D1"}, [{"doc_id": "doc"}], chunks)

    assert calls == []
    assert events == []


def test_ingest_texte_bundle_realigns_children_after_canonical_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    canonical = "99999999-8888-7777-6666-555555555555"
    generated = "11111111-2222-3333-4444-555555555555"
    writer, calls, events = _writer(
        [
            {"rows": [(canonical,)]},
            {"rowcount": 0},
            {"rowcount": 0},
        ]
    )
    monkeypatch.setattr(writer, "ensure_modern_target_table", lambda: None)
    monkeypatch.setattr(writer, "_upsert_documents", lambda conn, documents: 1)
    monkeypatch.setattr(writer, "_upsert_sections", lambda conn, sections: len(sections))
    upserted_chunks: list[dict[str, Any]] = []

    def fake_upsert(conn: object, table: str, rows: list[dict[str, Any]], conflict: list[str], **kwargs: Any) -> int:
        upserted_chunks.extend(rows)
        return len(rows)

    monkeypatch.setattr(writer, "_upsert", fake_upsert)
    sections = [{"section_id": "section", "doc_id": generated}]
    chunks = [{"hash_id": "chunk", "short_id": "D1", "source_document_id": generated, "_targets": ["modern"]}]

    writer.ingest_texte_bundle(
        {"doc_id": generated, "short_id": "D1", "checksum": "sha"},
        sections,
        chunks,
    )

    assert sections[0]["doc_id"] == canonical
    assert chunks[0]["source_document_id"] == generated  # projection is realigned, not the caller-owned input
    assert upserted_chunks[0]["source_document_id"] == canonical
    assert calls[1]["params"] == (canonical,)
    assert events == ["commit"]


def test_ingest_article_bundle_cascades_twin_in_same_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix swap #311 (P1 atomicité) : la cascade du jumeau version et l'ingest de
    # la chronique partagent UNE seule transaction (un seul commit), la cascade
    # précédant l'upsert de la chronique.
    writer, calls, events = _writer(
        [
            {"rows": [("doc-old",)]},  # cascade: SELECT doc_id du jumeau
            {"rowcount": 1},  # cascade: DELETE chunks legacy par cid
            {"rowcount": 1},  # cascade: DELETE sections
            {"rowcount": 1},  # cascade: DELETE documents
            {"rowcount": 0},  # ingest: DELETE sections du doc chronique
            {"rowcount": 0},  # ingest: DELETE chunks orphelins
        ]
    )
    monkeypatch.setattr(writer, "ensure_legacy_target_table", lambda: None)
    # Purge R2 neutralisée ici (pas de colonne index_variant dans ce scénario) :
    # la 2e passe a son test dédié (test_cascade_articles_purges_r2_rows...).
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: {})
    monkeypatch.setattr(writer, "_upsert_documents", lambda conn, documents: 1)
    monkeypatch.setattr(writer, "_canonical_doc_id", lambda conn, short_id: None)
    monkeypatch.setattr(writer, "_upsert_sections", lambda conn, sections: len(sections))
    monkeypatch.setattr(writer, "_upsert", lambda conn, table, rows, conflict, **kwargs: len(rows))

    counts = writer.ingest_article_bundle(
        {"doc_id": "dn", "short_id": "LEGI_NEW", "checksum": "h"},
        [{"section_id": "s", "doc_id": "dn", "section_index": 0}],
        [{"cid": "LEGI_NEW", "chunk_id": "c", "_targets": ["legacy"]}],
        cascade_cids=["LEGI_OLD"],
    )

    assert counts["migrated"] == {"chunks": 1, "sections": 1, "documents": 1}
    assert events == ["commit"]  # une SEULE transaction pour cascade + ingest
    # La cascade du jumeau (SELECT doc_id puis DELETE par cid) précède l'upsert.
    assert "SELECT doc_id" in calls[0]["query"] and "rag_documents" in calls[0]["query"]
    assert "DELETE" in calls[1]["query"] and "rag_chunks_dgafp" in calls[1]["query"]


def test_cascade_articles_purges_r2_rows_in_fresh_statement(monkeypatch) -> None:
    """Revue #332 round 4 : la 2e passe de purge des lignes R2 couvre AUSSI le
    chemin _cascade_articles_ops (suppressions normales et cascade_cids), pas
    seulement _ingest_bundle_tx."""
    from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter

    executed: list[str] = []

    class _Cur:
        rowcount = 1

        def execute(self, query, params=None):
            executed.append(str(query))

        def fetchall(self):
            return []  # aucun doc_id : seuls les DELETE chunks nous intéressent

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    writer = LegifranceDbWriter(schema="public", dsn="postgresql://fake", legacy_table_name="rag_chunks_dgafp")
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: {"index_variant": "text"})
    counts = writer._cascade_articles_ops(_Conn(), ["C1"], "legifrance")

    purge = [q for q in executed if "index_variant IS NOT NULL" in q]
    assert len(purge) == 1, executed  # 2e passe présente, en statement séparé
    assert counts["chunks"] == 2  # DELETE principal + purge

    # Hors table legacy : aucun probe, aucune purge (le probe est inutile sur
    # la table moderne, revue #332 round 4).
    executed.clear()
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: (_ for _ in ()).throw(AssertionError("probe interdit hors legacy")))
    assert writer._purge_summary_rows_fresh_snapshot(_Conn(), table="rag_chunks_legifrance", join_column="short_id", uids=["X"]) == 0
