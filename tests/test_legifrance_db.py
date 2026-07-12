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
            {"rows": [("D1", "doc-1", "sha-1", "Décret n°1 de test")]},
            {"rows": [("staging.rag_chunks_dgafp",)]},
            {"rows": [("D1", 2)]},
            {"rows": []},
        ]
    )
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: {"checksum": ("text", None)})
    monkeypatch.setattr(writer, "ensure_legacy_target_table", lambda: pytest.fail("read path attempted legacy DDL"))
    monkeypatch.setattr(writer, "ensure_modern_target_table", lambda: pytest.fail("read path attempted modern DDL"))

    corpus = writer.list_legifrance_corpus()

    assert corpus == {"D1": {"doc_id": "doc-1", "checksum": "sha-1", "nb_chunks": 2, "title": "Décret n°1 de test"}}
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
