from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_rh_data_engineering.jobs import service_public_ingestion, service_public_medallion


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
            [{"section_id": f"section-{short_id}", "doc_id": f"doc-{short_id}", "heading": "Titre"}],
        )
    if chunks:
        _write_jsonl(
            lake_root / "gold" / "chunks" / f"{short_id}.chunks.jsonl",
            [{"hash_id": f"chunk-{short_id}", "short_id": short_id, "chunk_text": "Texte"}],
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
    assert calls == {"documents": 1, "sections": 1, "chunks": 1}


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
