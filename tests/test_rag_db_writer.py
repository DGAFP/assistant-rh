"""Tests du writer DB générique (utils/db.py) extrait de ServicePublicDbWriter.

Couvre uniquement les méthodes nouvelles (réconciliation, suppression cascade,
trace des runs) : le comportement hérité est déjà verrouillé par
tests/test_service_public_jobs.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter
from assistant_rh_data_engineering.utils.db import RagDbWriter


class ScriptedCursor:
    """Cursor factice: enregistre les execute() et rejoue des résultats scriptés."""

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


class ScriptedConnection:
    def __init__(self, script: list[dict[str, Any]], calls: list[dict[str, Any]], events: list[str]):
        self._script = script
        self._calls = calls
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self._script, self._calls)

    def commit(self) -> None:
        self._events.append("commit")


def make_writer(script: list[dict[str, Any]], chunk_table: str | None = "rag_chunks_mi"):
    calls: list[dict[str, Any]] = []
    events: list[str] = []
    writer = RagDbWriter(schema="staging", dsn="postgresql://unused", chunk_table=chunk_table)
    connection = ScriptedConnection(script, calls, events)
    writer._connect = lambda: connection  # type: ignore[method-assign]
    return writer, calls, events


def test_require_chunk_table_fails_without_default_or_argument() -> None:
    writer = RagDbWriter(schema="staging", dsn="postgresql://unused")
    with pytest.raises(ValueError, match="table de chunks"):
        writer.upsert_chunks([{"hash_id": "x"}])


def test_service_public_writer_keeps_its_chunk_table() -> None:
    writer = ServicePublicDbWriter(schema="staging", dsn="postgresql://unused")
    assert writer.chunk_table == "rag_chunks_service_public"


def test_column_types_raises_explicitly_when_table_is_missing() -> None:
    writer, _, _ = make_writer([{"rows": []}])
    connection = writer._connect()

    with pytest.raises(RuntimeError, match="staging.rag_chunks_absent"):
        writer._column_types(connection, "rag_chunks_absent")


def test_delete_chunks_by_short_ids_normalizes_database_values_too() -> None:
    writer, calls, _ = make_writer([{"rowcount": 3}])
    connection = writer._connect()

    deleted = writer._delete_chunks_by_short_ids(connection, [" mi-0001 ", "MI-0002"])

    assert deleted == 3
    assert calls[0]["params"] == (["MI-0001", "MI-0002"],)
    assert "UPPER(TRIM(short_id))" in calls[0]["query"]
    assert "short_id IS NOT NULL" in calls[0]["query"]


def test_list_short_ids_with_checksum_reads_checksum_when_column_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    writer, calls, _ = make_writer(
        [
            {
                "rows": [
                    # 5e colonne: page_vision_complete (quality_flags->>...): 'true' / 'false' / None.
                    (" mi-0001 ", "doc-uuid-1", "sha-1", 12, "true"),
                    ("MI-0002", "doc-uuid-2", None, 0, "false"),
                ]
            }
        ]
    )
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: {"checksum": ("text", None), "quality_flags": ("jsonb", None)})

    current = writer.list_short_ids_with_checksum("MI")

    assert current == {
        "MI-0001": {"doc_id": "doc-uuid-1", "checksum": "sha-1", "nb_chunks": 12, "page_vision_complete": True},
        "MI-0002": {"doc_id": "doc-uuid-2", "checksum": None, "nb_chunks": 0, "page_vision_complete": False},
    }
    assert calls[0]["params"] == ("mi",)
    assert "rag_chunks_mi" in calls[0]["query"]
    assert "rag_documents" in calls[0]["query"]


def test_list_short_ids_with_checksum_tolerates_missing_checksum_column(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ni checksum ni quality_flags: la 5e colonne vaut NULL -> page_vision_complete True.
    writer, calls, _ = make_writer([{"rows": [("MI-0001", "doc-uuid-1", None, 3, None)]}])
    monkeypatch.setattr(writer, "_column_types", lambda conn, table: {"short_id": ("varchar", 64)})

    current = writer.list_short_ids_with_checksum("mi")

    assert current["MI-0001"]["checksum"] is None
    assert current["MI-0001"]["page_vision_complete"] is True  # absent -> considéré complet
    assert "SQL('NULL')" in calls[0]["query"]


def test_delete_documents_cascade_deletes_chunks_only_for_matched_documents() -> None:
    script = [
        {"rows": [("doc-uuid-1", "MI-0001"), ("doc-uuid-2", "MI-0002")]},  # SELECT docs with source filter
        {"rowcount": 7},  # DELETE chunks by matched short_id
        {"rowcount": 5},  # DELETE rag_sections
        {"rowcount": 2},  # DELETE rag_documents
    ]
    writer, calls, events = make_writer(script)

    counts = writer.delete_documents_cascade(["mi-0001", " MI-0002 "], source="MI")

    assert counts == {"chunks": 7, "sections": 5, "documents": 2}
    assert events == ["commit"]
    assert "rag_documents" in calls[0]["query"]
    assert calls[0]["params"] == [["MI-0001", "MI-0002"], "mi"]
    assert "short_id" in calls[1]["query"]
    assert calls[1]["params"] == (["MI-0001", "MI-0002"],)
    assert "rag_sections" in calls[2]["query"]
    assert "rag_documents" in calls[3]["query"]


def test_delete_documents_cascade_deletes_in_order_and_commits_once(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [
        {"rows": [("doc-uuid-1", "MI-0001"), ("doc-uuid-2", "MI-0002")]},  # SELECT documents
        {"rowcount": 5},  # DELETE rag_sections
        {"rowcount": 2},  # DELETE rag_documents
    ]
    writer, calls, events = make_writer(script)
    monkeypatch.setattr(
        writer,
        "_delete_chunks_by_short_ids",
        lambda conn, short_ids, table=None: 7,
    )

    counts = writer.delete_documents_cascade(["mi-0001", " MI-0002 "], source="MI")

    assert counts == {"chunks": 7, "sections": 5, "documents": 2}
    assert events == ["commit"]
    assert "rag_documents" in calls[0]["query"]
    assert calls[0]["params"] == [["MI-0001", "MI-0002"], "mi"]
    assert "rag_sections" in calls[1]["query"]
    assert calls[1]["params"] == (["doc-uuid-1", "doc-uuid-2"],)
    assert "rag_documents" in calls[2]["query"]


def test_delete_documents_cascade_requires_source() -> None:
    writer, calls, _ = make_writer([])

    with pytest.raises(TypeError):
        writer.delete_documents_cascade(["MI-0001"])  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="source obligatoire"):
        writer.delete_documents_cascade(["MI-0001"], source="  ")
    assert calls == []


def test_delete_documents_cascade_noop_on_empty_short_ids() -> None:
    writer, calls, events = make_writer([])

    counts = writer.delete_documents_cascade(["  ", ""], source="mi")

    assert counts == {"chunks": 0, "sections": 0, "documents": 0}
    assert calls == []
    assert events == []


def test_delete_documents_cascade_skips_section_and_doc_deletes_without_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    writer, calls, events = make_writer([{"rows": []}])
    monkeypatch.setattr(writer, "_delete_chunks_by_short_ids", lambda conn, short_ids, table=None: 0)

    counts = writer.delete_documents_cascade(["MI-0009"], source="mi")

    assert counts == {"chunks": 0, "sections": 0, "documents": 0}
    assert len(calls) == 1  # seul le SELECT doc_id a tourné
    assert events == ["commit"]


def test_insert_ingestion_run_upserts_on_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    writer, _, events = make_writer([])
    upserts: list[tuple] = []

    def fake_upsert(conn: object, table: str, rows: list[dict[str, Any]], conflict_cols: list[str]) -> int:
        upserts.append((table, rows, conflict_cols))
        return len(rows)

    monkeypatch.setattr(writer, "_upsert", fake_upsert)

    run = {"run_id": "run-1", "ministere": "mi", "ingested": 3}
    assert writer.insert_ingestion_run(run) == 1
    assert upserts == [("rag_ingestion_runs", [run], ["run_id"])]
    assert events == ["commit"]


def test_ingest_document_bundle_single_transaction_and_section_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Revue #246: doc + sections + chunks doivent partir dans UNE transaction
    # (sinon un échec après l'upsert du document fige un checksum à jour avec
    # des chunks périmés => ignore_inchange au run suivant), et les sections
    # de l'ancienne version sont purgées (delete-puis-insert par doc_id).
    script = [
        {"rows": [("11111111-2222-3333-4444-555555555555",)]},  # SELECT doc_id canonique (identique: pas de remap)
        {"rowcount": 5},  # DELETE rag_sections
        {"rowcount": 7},  # DELETE chunks par short_id
    ]
    writer, calls, events = make_writer(script)
    order: list[str] = []
    monkeypatch.setattr(writer, "_upsert_documents", lambda conn, docs: order.append("documents") or 1)
    monkeypatch.setattr(writer, "_upsert_sections", lambda conn, sections: order.append("sections") or len(sections))
    monkeypatch.setattr(
        writer,
        "_upsert",
        lambda conn, table, rows, conflict, **kwargs: order.append(f"chunks:{table}") or len(rows),
    )

    result = writer.ingest_document_bundle(
        {"doc_id": "11111111-2222-3333-4444-555555555555", "short_id": "mi-0001", "checksum": "sha"},
        [{"section_id": "s1"}, {"section_id": "s2"}],
        [{"hash_id": "c1"}, {"hash_id": "c2"}, {"hash_id": "c3"}],
    )

    assert events == ["commit"]  # une seule transaction
    assert order == ["documents", "sections", "chunks:rag_chunks_mi"]
    assert "rag_documents" in calls[0]["query"]  # lecture du doc_id canonique
    assert calls[0]["params"] == ("MI-0001",)
    assert "DELETE FROM" in calls[1]["query"] and "rag_sections" in calls[1]["query"]
    assert calls[1]["params"] == ("11111111-2222-3333-4444-555555555555",)
    assert "UPPER(TRIM(short_id))" in calls[2]["query"]  # purge des chunks par short_id
    assert result == {"documents": 1, "sections": 2, "chunks_deleted": 7, "chunks": 3}


def test_ingest_document_bundle_remaps_bundle_on_preexisting_doc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # L'upsert documents préserve le doc_id existant sur conflit short_id
    # (update_exclude_cols): un document homonyme préexistant doit réaligner
    # sections.doc_id et chunks.source_document_id sur le doc_id canonique,
    # et la purge des sections doit viser le doc_id canonique.
    canonical = "99999999-8888-7777-6666-555555555555"
    generated = "11111111-2222-3333-4444-555555555555"
    script = [
        {"rows": [(canonical,)]},  # SELECT doc_id canonique (diverge)
        {"rowcount": 0},  # DELETE rag_sections (doc_id canonique)
        {"rowcount": 0},  # DELETE chunks par short_id
    ]
    writer, calls, events = make_writer(script)
    monkeypatch.setattr(writer, "_upsert_documents", lambda conn, docs: 1)
    monkeypatch.setattr(writer, "_upsert_sections", lambda conn, sections: len(sections))
    monkeypatch.setattr(writer, "_upsert", lambda conn, table, rows, conflict, **kwargs: len(rows))

    sections = [{"section_id": "s1", "doc_id": generated}]
    chunks = [{"hash_id": "c1", "source_document_id": generated}]
    writer.ingest_document_bundle(
        {"doc_id": generated, "short_id": "mi-0001", "checksum": "sha"},
        sections,
        chunks,
    )

    assert sections[0]["doc_id"] == canonical
    assert chunks[0]["source_document_id"] == canonical
    assert calls[1]["params"] == (canonical,)  # purge des sections sous l'id canonique
    assert events == ["commit"]


def test_ingest_document_bundle_keeps_doc_id_when_short_id_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    # Plusieurs lignes pour un même short_id (schéma sans index unique):
    # on ne devine pas, le bundle garde son doc_id généré.
    generated = "11111111-2222-3333-4444-555555555555"
    script = [
        {"rows": [("a-1",), ("a-2",)]},  # SELECT doc_id canonique: ambigu
        {"rowcount": 0},
        {"rowcount": 0},
    ]
    writer, calls, _ = make_writer(script)
    monkeypatch.setattr(writer, "_upsert_documents", lambda conn, docs: 1)
    monkeypatch.setattr(writer, "_upsert_sections", lambda conn, sections: len(sections))
    monkeypatch.setattr(writer, "_upsert", lambda conn, table, rows, conflict, **kwargs: len(rows))

    sections = [{"section_id": "s1", "doc_id": generated}]
    writer.ingest_document_bundle({"doc_id": generated, "short_id": "mi-0001", "checksum": "sha"}, sections, [])

    assert sections[0]["doc_id"] == generated
    assert calls[1]["params"] == (generated,)


def test_delete_chunks_not_in_short_ids_sweeps_null_and_unknown() -> None:
    writer, calls, events = make_writer([{"rowcount": 42}])

    deleted = writer.delete_chunks_not_in_short_ids([" matte-0001 ", "MATTE-0002"])

    assert deleted == 42
    assert calls[0]["params"] == (["MATTE-0001", "MATTE-0002"],)
    assert "short_id IS NULL" in calls[0]["query"]
    assert "<> ALL" in calls[0]["query"]
    assert events == ["commit"]


def test_delete_chunks_not_in_short_ids_refuses_empty_keep_list() -> None:
    # Garde anti-wipe: une liste vide balayerait la table entière.
    writer, calls, _ = make_writer([])

    with pytest.raises(ValueError, match="anti-wipe"):
        writer.delete_chunks_not_in_short_ids(["  ", ""])
    assert calls == []
