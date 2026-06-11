from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_rh_data_engineering.jobs import service_public_ingestion, service_public_medallion
from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter
from assistant_rh_data_engineering.utils.helpers import stable_section_uuid


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _write_service_public_artifacts(lake_root: Path, short_id: str, *, sections: bool = True, chunks: bool = True) -> None:
    _write_json(
        lake_root / "silver" / "documents" / f"{short_id}.document.json",
        {"doc_id": f"doc-{short_id}", "short_id": short_id, "title": short_id},
    )
    if sections:
        _write_jsonl(
            lake_root / "silver" / "sections" / f"{short_id}.sections.jsonl",
            [
                {
                    "section_id": f"section-{short_id}",
                    "doc_id": f"doc-{short_id}",
                    "heading": "Titre",
                    "section_index": 0,
                }
            ],
        )
    if chunks:
        _write_jsonl(
            lake_root / "gold" / "chunks" / f"{short_id}.chunks.jsonl",
            [
                {
                    "hash_id": f"chunk-{short_id}",
                    "short_id": short_id,
                    "chunk_text": "Texte",
                    "source_document_id": f"doc-{short_id}",
                    "section_id": f"section-{short_id}",
                }
            ],
        )


def test_load_artifacts_reads_documents_sections_and_chunks(tmp_path: Path) -> None:
    _write_service_public_artifacts(tmp_path, "F32513")

    documents, sections, chunks, per_fiche = service_public_ingestion.load_artifacts(tmp_path, ["F32513"])

    assert [document["short_id"] for document in documents] == ["F32513"]
    assert [section["section_id"] for section in sections] == ["section-F32513"]
    assert [chunk["hash_id"] for chunk in chunks] == ["chunk-F32513"]
    assert per_fiche == {"F32513": {"documents": 1, "sections": 1, "chunks": 1}}


def test_load_artifacts_fails_on_missing_sections_file(tmp_path: Path) -> None:
    _write_service_public_artifacts(tmp_path, "F32513", sections=False)

    with pytest.raises(RuntimeError, match="sections silver"):
        service_public_ingestion.load_artifacts(tmp_path, ["F32513"])


def test_load_artifacts_fails_on_empty_chunks_file(tmp_path: Path) -> None:
    _write_service_public_artifacts(tmp_path, "F32513", chunks=False)
    _write_jsonl(tmp_path / "gold" / "chunks" / "F32513.chunks.jsonl", [])

    with pytest.raises(RuntimeError, match="chunks gold"):
        service_public_ingestion.load_artifacts(tmp_path, ["F32513"])


def test_load_artifacts_collects_corrupted_files_across_corpus(tmp_path: Path) -> None:
    corrupted_chunks = tmp_path / "gold" / "chunks" / "F32513.chunks.jsonl"
    _write_service_public_artifacts(tmp_path, "F32513")
    corrupted_chunks.write_text('{"hash_id": "chunk-F32513", "tronqué', encoding="utf-8")
    _write_service_public_artifacts(tmp_path, "F12163", sections=False)

    with pytest.raises(RuntimeError) as excinfo:
        service_public_ingestion.load_artifacts(tmp_path, ["F32513", "F12163"])

    message = str(excinfo.value)
    assert "F32513: chunks gold illisibles" in message
    assert "F12163: sections silver" in message


def test_load_artifacts_allows_missing_chunks_when_skipped(tmp_path: Path) -> None:
    _write_service_public_artifacts(tmp_path, "F32513", chunks=False)

    documents, sections, chunks, per_fiche = service_public_ingestion.load_artifacts(tmp_path, ["F32513"], skip_chunks=True)

    assert len(documents) == 1
    assert len(sections) == 1
    assert chunks == []
    assert per_fiche == {"F32513": {"documents": 1, "sections": 1, "chunks": 0}}


def test_remap_existing_document_ids_preserves_foreign_keys() -> None:
    documents = [{"doc_id": "new-doc", "short_id": "F32513", "title": "Titre"}]
    sections = [
        {
            "section_id": "new-section-parent",
            "doc_id": "new-doc",
            "section_index": 0,
            "parent_section_id": None,
        },
        {
            "section_id": "new-section-child",
            "doc_id": "new-doc",
            "section_index": 1,
            "parent_section_id": "new-section-parent",
        },
    ]
    chunks = [
        {
            "hash_id": "chunk-F32513",
            "short_id": "F32513",
            "source_document_id": "new-doc",
            "section_id": "new-section-child",
        }
    ]

    remapped = service_public_ingestion.remap_existing_document_ids(
        documents,
        sections,
        chunks,
        {"F32513": "existing-doc"},
    )

    parent_section_id = stable_section_uuid("existing-doc", 0)
    child_section_id = stable_section_uuid("existing-doc", 1)
    assert remapped == {"documents": 1, "sections": 2, "chunks": 1}
    assert documents[0]["doc_id"] == "existing-doc"
    assert sections == [
        {
            "section_id": parent_section_id,
            "doc_id": "existing-doc",
            "section_index": 0,
            "parent_section_id": None,
        },
        {
            "section_id": child_section_id,
            "doc_id": "existing-doc",
            "section_index": 1,
            "parent_section_id": parent_section_id,
        },
    ]
    assert chunks[0]["source_document_id"] == "existing-doc"
    assert chunks[0]["section_id"] == child_section_id


def test_remap_existing_document_ids_reuses_existing_section_ids() -> None:
    documents = [{"doc_id": "new-doc", "short_id": "F32513"}]
    sections = [
        {
            "section_id": "new-section",
            "doc_id": "new-doc",
            "section_index": 0,
            "parent_section_id": None,
        }
    ]
    chunks = [
        {
            "hash_id": "chunk-F32513",
            "source_document_id": "new-doc",
            "section_id": "new-section",
        }
    ]

    service_public_ingestion.remap_existing_document_ids(
        documents,
        sections,
        chunks,
        {"F32513": "existing-doc"},
        {("existing-doc", 0): "existing-section"},
    )

    assert sections[0]["section_id"] == "existing-section"
    assert chunks[0]["section_id"] == "existing-section"


def test_ingestion_main_upserts_documents_sections_and_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake_root = tmp_path / "lake"
    _write_service_public_artifacts(lake_root, "F32513")
    config_path = tmp_path / "service_public_fiches.json"
    _write_json(config_path, {"fiche_ids": ["F32513"]})
    calls: dict[str, int] = {}

    class DummyWriter:
        def __init__(self, schema: str = "public", dsn: str | None = None):
            self.schema = schema
            self.dsn = dsn

        def list_document_ids_by_short_id(self, short_ids: list[str]) -> dict[str, str]:
            calls["listed_documents"] = len(short_ids)
            return {}

        def list_section_ids_by_doc_id_and_index(self, doc_ids: list[str]) -> dict[tuple[str, int], str]:
            calls["listed_sections"] = len(doc_ids)
            return {}

        def upsert_documents(self, rows: list[dict[str, Any]]) -> int:
            calls["documents"] = calls.get("documents", 0) + len(rows)
            return len(rows)

        def upsert_sections(self, rows: list[dict[str, Any]]) -> int:
            calls["sections"] = calls.get("sections", 0) + len(rows)
            return len(rows)

        def upsert_chunks(self, rows: list[dict[str, Any]]) -> int:
            calls["chunks"] = calls.get("chunks", 0) + len(rows)
            return len(rows)

    import assistant_rh_data_engineering.service_public.db as service_public_db

    monkeypatch.setattr(service_public_db, "ServicePublicDbWriter", DummyWriter)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "service-public-ingestion",
            "--lake-root",
            str(lake_root),
            "--fiche-config",
            str(config_path),
            "--dsn",
            "postgresql://unused",
        ],
    )

    assert service_public_ingestion.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["loaded"] == {"documents": 1, "sections": 1, "chunks": 1}
    assert payload["ingested"] == {"documents": 1, "sections": 1, "chunks": 1}
    assert payload["per_fiche"] == {"F32513": {"documents": 1, "sections": 1, "chunks": 1}}
    assert payload["remapped_existing_ids"] == {"documents": 0, "sections": 0, "chunks": 0}
    assert calls == {"listed_documents": 1, "listed_sections": 0, "documents": 1, "sections": 1, "chunks": 1}


def test_db_writer_upserts_documents_on_short_id_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = ServicePublicDbWriter(dsn="postgresql://unused")
    calls: dict[str, Any] = {}

    class DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def commit(self) -> None:
            calls["committed"] = True

    monkeypatch.setattr(writer, "_connect", lambda: DummyConnection())
    monkeypatch.setattr(writer, "_index_predicate", lambda conn, index_name: "short_id IS NOT NULL")

    def fake_upsert(
        conn: object,
        table: str,
        rows: list[dict[str, Any]],
        conflict_cols: list[str],
        conflict_where: str | None = None,
        update_exclude_cols: list[str] | None = None,
    ) -> int:
        calls["table"] = table
        calls["rows"] = rows
        calls["conflict_cols"] = conflict_cols
        calls["conflict_where"] = conflict_where
        calls["update_exclude_cols"] = update_exclude_cols
        return len(rows)

    monkeypatch.setattr(writer, "_upsert", fake_upsert)

    assert writer.upsert_documents([{"doc_id": "new-doc", "short_id": "F12386", "title": "Titre"}]) == 1
    assert calls["table"] == "rag_documents"
    assert calls["conflict_cols"] == ["short_id"]
    assert calls["conflict_where"] == "short_id IS NOT NULL"
    assert calls["update_exclude_cols"] == ["doc_id"]
    assert calls["committed"] is True


def test_db_writer_upserts_sections_on_doc_index_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = ServicePublicDbWriter(dsn="postgresql://unused")
    calls: dict[str, Any] = {}

    class DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def commit(self) -> None:
            calls["committed"] = True

    monkeypatch.setattr(writer, "_connect", lambda: DummyConnection())
    monkeypatch.setattr(writer, "_index_predicate", lambda conn, index_name: "")

    def fake_upsert(
        conn: object,
        table: str,
        rows: list[dict[str, Any]],
        conflict_cols: list[str],
        conflict_where: str | None = None,
        update_exclude_cols: list[str] | None = None,
    ) -> int:
        calls["table"] = table
        calls["rows"] = rows
        calls["conflict_cols"] = conflict_cols
        calls["conflict_where"] = conflict_where
        calls["update_exclude_cols"] = update_exclude_cols
        return len(rows)

    monkeypatch.setattr(writer, "_upsert", fake_upsert)

    assert writer.upsert_sections([{"section_id": "section", "doc_id": "doc", "section_index": 0}]) == 1
    assert calls["table"] == "rag_sections"
    assert calls["conflict_cols"] == ["doc_id", "section_index"]
    assert calls["conflict_where"] is None
    assert calls["update_exclude_cols"] == ["section_id"]
    assert calls["committed"] is True


def test_summarize_pipeline_outputs_returns_per_fiche_counts() -> None:
    summary = service_public_medallion.summarize_pipeline_outputs(
        ["F32513"],
        [SimpleNamespace(fiche_id="F32513")],
        [SimpleNamespace(document={"short_id": "F32513"}, sections=[{"section_id": "section-F32513"}])],
        [SimpleNamespace(document={"short_id": "F32513"}, chunks=[{"hash_id": "chunk-F32513"}])],
    )

    assert summary == {
        "F32513": {
            "bronze": 1,
            "documents": 1,
            "sections": 1,
            "gold_documents": 1,
            "chunks": 1,
        }
    }


def test_summarize_pipeline_outputs_fails_on_missing_chunks() -> None:
    with pytest.raises(RuntimeError, match="gold chunks"):
        service_public_medallion.summarize_pipeline_outputs(
            ["F32513"],
            [SimpleNamespace(fiche_id="F32513")],
            [SimpleNamespace(document={"short_id": "F32513"}, sections=[{"section_id": "section-F32513"}])],
            [SimpleNamespace(document={"short_id": "F32513"}, chunks=[])],
        )
