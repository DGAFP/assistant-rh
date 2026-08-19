"""Tests des primitives partagées du médaillon delta (utils/medallion_delta, #289)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from assistant_rh_data_engineering.utils import medallion_delta


def test_count_valid_gold_chunks_valid_empty_corrupt_unreadable(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    assert medallion_delta.count_valid_gold_chunks(good) == 2

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert medallion_delta.count_valid_gold_chunks(empty) == 0

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text('{"ok": 1}\nnope', encoding="utf-8")
    assert medallion_delta.count_valid_gold_chunks(corrupt) == 0

    unreadable = tmp_path / "adir.jsonl"
    unreadable.mkdir()  # lire un répertoire -> OSError
    assert medallion_delta.count_valid_gold_chunks(unreadable) == 0


def test_capture_previous_checksums_reads_documents_and_skips_corrupt(tmp_path: Path) -> None:
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "F1.document.json").write_text(json.dumps({"short_id": "f1", "checksum": "h1"}), encoding="utf-8")
    (docs / "F2.document.json").write_text(json.dumps({"short_id": "F2", "checksum": "h2"}), encoding="utf-8")
    (docs / "bad.document.json").write_text("{pas du json", encoding="utf-8")  # ignoré

    result = medallion_delta.capture_previous_checksums(docs)

    assert result == {"F1": "h1", "F2": "h2"}  # short_id normalisé en MAJ


def test_reusable_gold_chunk_count_only_when_unchanged_and_valid(tmp_path: Path) -> None:
    gold = tmp_path / "chunks"
    gold.mkdir()
    (gold / "F1.chunks.jsonl").write_text('{"hash_id": "a"}\n{"hash_id": "b"}\n', encoding="utf-8")
    previous = {"F1": "h1"}

    # inchangé + gold valide -> réutilisable
    assert medallion_delta.reusable_gold_chunk_count(gold, "F1", "h1", previous) == 2
    # checksum différent (modifié) -> 0 (rebuild)
    assert medallion_delta.reusable_gold_chunk_count(gold, "F1", "h2", previous) == 0
    # absent du previous (nouveau) -> 0
    assert medallion_delta.reusable_gold_chunk_count(gold, "F9", "h9", previous) == 0
    # inchangé mais gold absent -> 0
    assert medallion_delta.reusable_gold_chunk_count(gold, "F1", "h1", {"F1": "h1", "F2": "h2"}) == 2
    assert medallion_delta.reusable_gold_chunk_count(gold, "F2", "h2", {"F2": "h2"}) == 0


def test_count_valid_gold_chunks_rejects_bad_utf8_and_null_rows(tmp_path: Path) -> None:
    # UTF-8 invalide -> 0 (pas de UnicodeDecodeError qui remonte).
    bad_utf8 = tmp_path / "bad_utf8.jsonl"
    bad_utf8.write_bytes(b'{"a": 1}\n\xff\xfe not utf8')
    assert medallion_delta.count_valid_gold_chunks(bad_utf8) == 0

    # Ligne JSON `null` (valide JSON mais pas un objet) -> 0, jamais réutilisé.
    null_row = tmp_path / "null.jsonl"
    null_row.write_text('{"ok": 1}\nnull\n', encoding="utf-8")
    assert medallion_delta.count_valid_gold_chunks(null_row) == 0

    # Objet vide / liste / scalaire -> 0.
    empty_obj = tmp_path / "emptyobj.jsonl"
    empty_obj.write_text("{}\n", encoding="utf-8")
    assert medallion_delta.count_valid_gold_chunks(empty_obj) == 0


def test_capture_previous_checksums_skips_non_object_payloads(tmp_path: Path) -> None:
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "A.document.json").write_text(json.dumps({"short_id": "A", "checksum": "h"}), encoding="utf-8")
    (docs / "null.document.json").write_text("null", encoding="utf-8")  # JSON null -> pas d'AttributeError
    (docs / "list.document.json").write_text("[1, 2]", encoding="utf-8")  # non-objet -> ignoré

    assert medallion_delta.capture_previous_checksums(docs) == {"A": "h"}


def test_gold_reuse_fingerprint_depends_on_config() -> None:
    from types import SimpleNamespace

    base = SimpleNamespace(enable_m3=False, enable_bge_scaleway=False, m3_model_name="BAAI/bge-m3", scaleway_model_name="bge", normalize=True)
    fp = medallion_delta.gold_reuse_fingerprint(source_name="service_public", single_chunk_per_article=True, embeddings=base)

    # Déterministe et stable.
    assert fp == medallion_delta.gold_reuse_fingerprint(source_name="service_public", single_chunk_per_article=True, embeddings=base)
    # Une évolution Légifrance n'invalide pas l'empreinte Service-Public.
    legifrance_fp = medallion_delta.gold_reuse_fingerprint(source_name="legifrance", single_chunk_per_article=True, embeddings=base)
    assert legifrance_fp != fp
    # Chunking différent -> empreinte différente.
    assert fp != medallion_delta.gold_reuse_fingerprint(source_name="service_public", single_chunk_per_article=False, embeddings=base)
    # Embeddings activés -> empreinte différente.
    changed = SimpleNamespace(enable_m3=True, enable_bge_scaleway=False, m3_model_name="BAAI/bge-m3", scaleway_model_name="bge", normalize=True)
    assert fp != medallion_delta.gold_reuse_fingerprint(source_name="service_public", single_chunk_per_article=True, embeddings=changed)
    # Modèle m3 différent -> empreinte différente.
    other_model = SimpleNamespace(enable_m3=False, enable_bge_scaleway=False, m3_model_name="autre", scaleway_model_name="bge", normalize=True)
    assert fp != medallion_delta.gold_reuse_fingerprint(source_name="service_public", single_chunk_per_article=True, embeddings=other_model)


def test_gold_reuse_fingerprint_rejects_unknown_source() -> None:
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="Source médaillon inconnue"):
        medallion_delta.gold_reuse_fingerprint(
            source_name="unknown",
            single_chunk_per_article=True,
            embeddings=SimpleNamespace(),
        )


def test_gold_fingerprints_roundtrip_missing_and_corrupt(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    assert medallion_delta.read_gold_fingerprints(gold) == {}  # absent -> {}

    medallion_delta.write_gold_fingerprints(gold, {"F1": "abc", "F2": "def"})
    assert medallion_delta.read_gold_fingerprints(gold) == {"F1": "abc", "F2": "def"}

    (gold / medallion_delta.GOLD_STATE_FILENAME).write_text("{pas du json", encoding="utf-8")
    assert medallion_delta.read_gold_fingerprints(gold) == {}  # corrompu -> {}


def test_hydrate_silver_gold_downloads_only_silver_and_gold(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class _Syncer:
        def download_medallion_root(self, root: Any, target_env: str, source_name: str = "x", include_layers: tuple[str, ...] = ()) -> dict[str, str]:
            calls.append({"root": str(root), "env": target_env, "source": source_name, "layers": tuple(include_layers)})
            return {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}

    out = medallion_delta.hydrate_silver_gold(_Syncer(), tmp_path, "staging", "legifrance")

    assert out == {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}
    assert calls == [{"root": str(tmp_path), "env": "staging", "source": "legifrance", "layers": ("silver", "gold")}]
