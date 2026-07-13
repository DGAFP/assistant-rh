"""Tests du médaillon Service-Public piloté par Grist + delta (E2.3-c, #289).

``--from-grist`` : la sélection vient du référentiel (lignes SP actives), la
config committée devient un cache. ``--delta`` : gold+embeddings reconstruits
uniquement pour les fiches nouvelles/modifiées (hash silver), les inchangées
réutilisent leur artefact gold. Pipeline factice — aucun fetch DILA réel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_rh_data_engineering.jobs import service_public_medallion
from assistant_rh_data_engineering.service_public.config import EmbeddingConfig
from assistant_rh_data_engineering.utils.medallion_delta import gold_reuse_fingerprint, write_gold_fingerprint


def _seed_gold_fingerprint(lake_root: Path, *, enable_m3: bool = False, enable_bge: bool = False) -> None:
    """Sidecar d'empreinte config : sans lui, la réutilisation delta est gatée off.

    Défaut = empreinte d'un run --no-embed (m3/bge désactivés). Passer des flags
    différents simule un changement de config gold/embeddings (invalide la réutilisation).
    """
    fp = gold_reuse_fingerprint(
        single_chunk_per_article=False,  # SP GoldConfig n'a pas ce champ (getattr -> False)
        embeddings=EmbeddingConfig(enable_m3=enable_m3, enable_bge_scaleway=enable_bge),
    )
    write_gold_fingerprint(lake_root / "gold", fp)


class _Bundle:
    def __init__(self, document: dict[str, Any], sections: list[dict[str, Any]] | None = None, chunks: list[dict[str, Any]] | None = None) -> None:
        self.document = document
        self.sections = sections if sections is not None else [{"doc_id": document.get("doc_id"), "section_index": 0}]
        self.chunks = chunks or []


class _FakePipeline:
    """Pipeline SP factice : bronze/silver déterministes, gold enregistré."""

    last: "_FakePipeline | None" = None
    checksums: dict[str, str] = {}

    def __init__(self, config: Any) -> None:
        self.config = config
        self.gold_calls: list[list[str]] = []
        _FakePipeline.last = self

    def run_bronze(self, fiche_ids: Any = None) -> list[Any]:
        return [SimpleNamespace(fiche_id=fiche_id) for fiche_id in (fiche_ids or self.config.fiche_ids or [])]

    def run_silver(self, batch: list[Any]) -> list[_Bundle]:
        return [
            _Bundle({"short_id": asset.fiche_id, "doc_id": f"doc-{asset.fiche_id}", "checksum": self.checksums.get(asset.fiche_id, "h")})
            for asset in batch
        ]

    def run_gold(self, bundles: list[_Bundle]) -> list[_Bundle]:
        self.gold_calls.append([str(bundle.document["short_id"]) for bundle in bundles])
        out = []
        for bundle in bundles:
            uid = str(bundle.document["short_id"])
            chunks = [{"hash_id": f"chunk-{uid}", "short_id": uid}]
            chunks_path = Path(self.config.paths.gold_dir) / "chunks" / f"{uid}.chunks.jsonl"
            chunks_path.parent.mkdir(parents=True, exist_ok=True)
            chunks_path.write_text("\n".join(json.dumps(c) for c in chunks), encoding="utf-8")
            out.append(_Bundle(bundle.document, sections=bundle.sections, chunks=chunks))
        return out


class _FakeGrist:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def list_records(self, table_id: str | None = None) -> list[dict[str, Any]]:
        return self._records


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, checksums: dict[str, str]) -> None:
    import assistant_rh_data_engineering.service_public as sp_package

    _FakePipeline.checksums = checksums
    _FakePipeline.last = None
    monkeypatch.setattr(sp_package, "ServicePublicPipeline", _FakePipeline)


def _seed_silver(lake_root: Path, fiche_id: str, checksum: str) -> None:
    path = lake_root / "silver" / "documents" / f"{fiche_id}.document.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"short_id": fiche_id, "checksum": checksum}), encoding="utf-8")


def _seed_gold(lake_root: Path, fiche_id: str, nb_chunks: int = 1) -> None:
    path = lake_root / "gold" / "chunks" / f"{fiche_id}.chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"hash_id": f"old-{fiche_id}-{i}"}) for i in range(nb_chunks)), encoding="utf-8")


def _write_config(tmp_path: Path, fiche_ids: list[str]) -> Path:
    config_path = tmp_path / "service_public_fiches.json"
    config_path.write_text(json.dumps({"fiche_ids": fiche_ids, "situation": "FPE"}), encoding="utf-8")
    return config_path


def test_delta_rebuilds_only_new_and_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake_root = tmp_path / "lake"
    # F1 inchangée (hash identique + gold existant), F2 modifiée, F3 nouvelle.
    _seed_silver(lake_root, "F1", "h1")
    _seed_gold(lake_root, "F1", nb_chunks=3)
    _seed_silver(lake_root, "F2", "h2-old")
    _seed_gold(lake_root, "F2")
    _seed_gold_fingerprint(lake_root)
    config_path = _write_config(tmp_path, ["F1", "F2", "F3"])
    _patch_pipeline(monkeypatch, {"F1": "h1", "F2": "h2-new", "F3": "h3"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-medallion", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--delta", "--no-embed"],
    )

    assert service_public_medallion.main() == 0

    pipeline = _FakePipeline.last
    assert pipeline is not None
    assert pipeline.gold_calls == [["F2", "F3"]]  # F1 jamais reconstruite

    payload = json.loads(capsys.readouterr().out)
    assert payload["delta"] is True
    assert payload["gold_skipped_unchanged"] == ["F1"]
    assert payload["gold_chunks_reused"] == 3
    assert payload["per_fiche"]["F1"]["chunks"] == 3  # gold réutilisé, pas « manquant »
    assert payload["per_fiche"]["F2"]["chunks"] == 1


def test_delta_rebuilds_when_existing_gold_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Hash inchangé MAIS gold existant vide (write interrompu, sync partiel) :
    # reconstruire, jamais skipper — sinon le run delta échouerait en boucle
    # sans s'auto-réparer (leçon retry_zero_chunk).
    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "F1", "h1")
    _seed_gold(lake_root, "F1", nb_chunks=0)  # fichier présent mais vide
    _seed_gold_fingerprint(lake_root)  # config inchangée -> le rebuild vient du gold vide
    config_path = _write_config(tmp_path, ["F1"])
    _patch_pipeline(monkeypatch, {"F1": "h1"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-medallion", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--delta", "--no-embed"],
    )

    assert service_public_medallion.main() == 0  # pas de RuntimeError « gold chunks »

    pipeline = _FakePipeline.last
    assert pipeline is not None
    assert pipeline.gold_calls == [["F1"]]  # reconstruite malgré le hash inchangé

    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_skipped_unchanged"] == []
    assert payload["per_fiche"]["F1"]["chunks"] == 1


def test_from_grist_selects_active_rows_and_ignores_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import assistant_rh_data_engineering.utils.grist as grist_module

    lake_root = tmp_path / "lake"
    records = [
        {"id": 1, "fields": {"source_corpus": "service-public", "id_extraction": "F2", "statut": "a_ingerer"}},
        {"id": 2, "fields": {"source_corpus": "service-public", "id_extraction": "F1", "statut": "ingere"}},
        {"id": 3, "fields": {"source_corpus": "service-public", "id_extraction": "F9", "statut": "ingere", "abroge": "oui"}},
        {"id": 4, "fields": {"source_corpus": "service-public", "id_extraction": "F8", "statut": "en_attente"}},
    ]
    monkeypatch.setattr(grist_module, "GristClient", lambda *a, **k: _FakeGrist(records))
    _patch_pipeline(monkeypatch, {"F1": "h1", "F2": "h2"})
    monkeypatch.setattr(
        sys,
        "argv",
        # --fiche-config volontairement inexistant : il ne doit PAS être lu.
        ["sp-medallion", "--lake-root", str(lake_root), "--fiche-config", str(tmp_path / "absent.json"), "--from-grist", "--no-embed"],
    )

    assert service_public_medallion.main() == 0

    pipeline = _FakePipeline.last
    assert pipeline is not None
    assert list(pipeline.config.fiche_ids) == ["F1", "F2"]  # actives seulement (tri par uid), abrogée/limbo exclues

    payload = json.loads(capsys.readouterr().out)
    assert payload["from_grist"] is True
    assert payload["fiche_config_path"] is None
    assert payload["situation"] == "FPE"  # même défaut que le générateur E2.1


def test_from_grist_failure_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import assistant_rh_data_engineering.utils.grist as grist_module

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise grist_module.GristError("GRIST_API_KEY manquant")

    monkeypatch.setattr(grist_module, "GristClient", _boom)
    monkeypatch.setattr(sys, "argv", ["sp-medallion", "--from-grist"])

    with pytest.raises(SystemExit, match="Échec Grist en mode --from-grist"):
        service_public_medallion.main()


def test_from_grist_without_active_rows_exits_with_clear_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import assistant_rh_data_engineering.utils.grist as grist_module

    records = [{"id": 1, "fields": {"source_corpus": "service-public", "id_extraction": "F9", "statut": "ingere", "abroge": "oui"}}]
    monkeypatch.setattr(grist_module, "GristClient", lambda *a, **k: _FakeGrist(records))
    monkeypatch.setattr(sys, "argv", ["sp-medallion", "--lake-root", str(tmp_path / "lake"), "--from-grist"])

    # Le message doit pointer les statuts Grist, pas la config committée.
    with pytest.raises(SystemExit, match="ligne Service-Public active"):
        service_public_medallion.main()


def test_delta_rejects_ingest_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sp-medallion", "--delta", "--ingest"])

    with pytest.raises(SystemExit, match="incompatible avec --delta"):
        service_public_medallion.main()


def test_delta_with_sync_hydrates_silver_and_gold_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Revue #308 P1 : job stateless (Scaleway) -> l'état précédent vit dans
    # l'Object Storage. --delta --sync-object-storage doit HYDRATER silver+gold
    # AVANT de lire previous_checksums, sinon chaque cron reconstruit tout.
    import assistant_rh_data_engineering.utils.object_storage as object_storage

    class _FakeSyncer:
        calls: list[dict[str, Any]] = []

        def __init__(self, config: Any) -> None:
            pass

        def download_medallion_root(
            self, root: Any, target_env: str, source_name: str = "service_public", include_layers: tuple[str, ...] = ("bronze", "silver", "gold")
        ) -> dict[str, str]:
            _FakeSyncer.calls.append({"root": str(root), "source_name": source_name, "layers": tuple(include_layers)})
            return {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}

        def sync_medallion_root(self, root: Any, target_env: str, **kwargs: Any) -> dict[str, str]:
            return {"bronze": "s3://x/bronze/", "silver": "s3://x/silver/", "gold": "s3://x/gold/"}

    _FakeSyncer.calls = []
    monkeypatch.setattr(object_storage, "ScalewayObjectStorageSync", _FakeSyncer)
    monkeypatch.setattr(object_storage.ObjectStorageConfig, "from_env", classmethod(lambda cls: SimpleNamespace()))

    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "F1", "h1")
    _seed_gold(lake_root, "F1", nb_chunks=2)
    _seed_gold_fingerprint(lake_root)
    config_path = _write_config(tmp_path, ["F1"])
    _patch_pipeline(monkeypatch, {"F1": "h1"})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sp-medallion",
            "--lake-root",
            str(lake_root),
            "--fiche-config",
            str(config_path),
            "--delta",
            "--sync-object-storage",
            "--no-embed",
            "--target-env",
            "staging",
        ],
    )

    assert service_public_medallion.main() == 0

    # Hydratation silver+gold AVANT traitement (une seule fois, source+layers exacts).
    assert _FakeSyncer.calls == [{"root": str(lake_root), "source_name": "service_public", "layers": ("silver", "gold")}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["hydrated_from_object_storage"] == {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}
    # État hydraté exploité : F1 inchangée -> réutilisée, pas reconstruite.
    assert payload["gold_skipped_unchanged"] == ["F1"]
    assert _FakePipeline.last is not None and _FakePipeline.last.gold_calls == []


def test_delta_rebuilds_when_existing_gold_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Revue #308 P2 : hash inchangé MAIS gold non-vide et illisible (JSONL
    # corrompu, sync partiel) -> reconstruire, jamais réutiliser un artefact invalide.
    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "F1", "h1")
    corrupt = lake_root / "gold" / "chunks" / "F1.chunks.jsonl"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text('{"ok": 1}\nCECI N EST PAS DU JSON', encoding="utf-8")
    _seed_gold_fingerprint(lake_root)  # config inchangée -> le rebuild vient du gold corrompu
    config_path = _write_config(tmp_path, ["F1"])
    _patch_pipeline(monkeypatch, {"F1": "h1"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-medallion", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--delta", "--no-embed"],
    )

    assert service_public_medallion.main() == 0

    assert _FakePipeline.last is not None and _FakePipeline.last.gold_calls == [["F1"]]  # reconstruite
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_skipped_unchanged"] == []


def test_delta_rebuilds_when_gold_config_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Revue #313 P1 : F1 inchangée (silver+gold+hash) MAIS empreinte config
    # différente (embeddings changés depuis le run précédent) -> reconstruire,
    # jamais réutiliser un gold produit sous une autre config.
    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "F1", "h1")
    _seed_gold(lake_root, "F1", nb_chunks=2)
    _seed_gold_fingerprint(lake_root, enable_m3=True, enable_bge=True)  # config PRÉCÉDENTE différente du run --no-embed
    config_path = _write_config(tmp_path, ["F1"])
    _patch_pipeline(monkeypatch, {"F1": "h1"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-medallion", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--delta", "--no-embed"],
    )

    assert service_public_medallion.main() == 0

    assert _FakePipeline.last is not None and _FakePipeline.last.gold_calls == [["F1"]]  # reconstruite malgré silver inchangé
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_config_unchanged"] is False
    assert payload["gold_skipped_unchanged"] == []


def test_count_valid_gold_chunks_handles_valid_empty_corrupt_and_unreadable(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    assert service_public_medallion.count_valid_gold_chunks(good) == 2

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert service_public_medallion.count_valid_gold_chunks(empty) == 0

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text('{"ok": 1}\nnope', encoding="utf-8")
    assert service_public_medallion.count_valid_gold_chunks(corrupt) == 0

    unreadable = tmp_path / "adir.jsonl"
    unreadable.mkdir()  # lire un répertoire -> OSError
    assert service_public_medallion.count_valid_gold_chunks(unreadable) == 0
