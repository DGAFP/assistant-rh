"""Tests du médaillon Légifrance en mode --delta (E2.3-c, #20, #289).

Même patron que le médaillon Service-Public (#308) via les helpers partagés
utils/medallion_delta : gold+embeddings reconstruits uniquement pour les articles
nouveaux/modifiés (hash silver), les inchangés réutilisent leur gold. Pipeline
factice — aucun dump DILA réel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_rh_data_engineering.jobs import legifrance_medallion
from assistant_rh_data_engineering.legifrance.config import EmbeddingConfig
from assistant_rh_data_engineering.utils.medallion_delta import gold_reuse_fingerprint, write_gold_fingerprints


def _seed_gold_fingerprint(lake_root: Path, uids: list[str], *, enable_m3: bool = False, enable_bge: bool = False, single_chunk: bool = True) -> None:
    """Sidecar d'empreinte config PAR document (défaut = run --no-embed, single_chunk True côté Legi)."""
    fp = gold_reuse_fingerprint(
        single_chunk_per_article=single_chunk,
        embeddings=EmbeddingConfig(enable_m3=enable_m3, enable_bge_scaleway=enable_bge),
    )
    write_gold_fingerprints(lake_root / "gold", {uid.upper(): fp for uid in uids})


class _Bundle:
    def __init__(self, document: dict[str, Any], chunks: list[dict[str, Any]] | None = None) -> None:
        self.document = document
        self.chunks = chunks or []


class _FakeLegiPipeline:
    last: "_FakeLegiPipeline | None" = None
    checksums: dict[str, str] = {}
    articles: list[str] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.gold_calls: list[list[str]] = []
        self.bronze_repo = object()
        self.bronze_builder = SimpleNamespace(fetch_from_object_storage=lambda repo, syncer, env: self.run_bronze())
        _FakeLegiPipeline.last = self

    def run_bronze(self) -> list[Any]:
        return [SimpleNamespace(short_id=uid) for uid in _FakeLegiPipeline.articles]

    def run_silver(self, batch: list[Any]) -> list[_Bundle]:
        return [
            _Bundle({"short_id": asset.short_id, "doc_id": f"doc-{asset.short_id}", "checksum": self.checksums.get(asset.short_id, "h")})
            for asset in batch
        ]

    def run_gold(self, bundles: list[_Bundle]) -> list[_Bundle]:
        self.gold_calls.append([str(bundle.document["short_id"]) for bundle in bundles])
        out = []
        for bundle in bundles:
            uid = str(bundle.document["short_id"])
            chunks = [{"hash_id": f"chunk-{uid}"}]
            chunks_path = Path(self.config.paths.gold_dir) / "chunks" / f"{uid}.chunks.jsonl"
            chunks_path.parent.mkdir(parents=True, exist_ok=True)
            chunks_path.write_text("\n".join(json.dumps(c) for c in chunks), encoding="utf-8")
            out.append(_Bundle(bundle.document, chunks=chunks))
        return out

    def ingest_from_silver_and_gold(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("ingest ne doit pas être appelé en --delta")


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, articles: list[str], checksums: dict[str, str]) -> None:
    import assistant_rh_data_engineering.legifrance as legi_package

    _FakeLegiPipeline.articles = articles
    _FakeLegiPipeline.checksums = checksums
    _FakeLegiPipeline.last = None
    monkeypatch.setattr(legi_package, "LegifrancePipeline", _FakeLegiPipeline)


def _seed_silver(lake_root: Path, uid: str, checksum: str) -> None:
    path = lake_root / "silver" / "documents" / f"{uid}.document.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"short_id": uid, "checksum": checksum}), encoding="utf-8")


def _seed_gold(lake_root: Path, uid: str, nb_chunks: int = 1) -> None:
    path = lake_root / "gold" / "chunks" / f"{uid}.chunks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"hash_id": f"old-{uid}-{i}"}) for i in range(nb_chunks)), encoding="utf-8")


def test_delta_rebuilds_only_new_and_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake_root = tmp_path / "lake"
    # A1 inchangé (hash identique + gold), A2 modifié, A3 nouveau.
    _seed_silver(lake_root, "A1", "h1")
    _seed_gold(lake_root, "A1", nb_chunks=3)
    _seed_silver(lake_root, "A2", "h2-old")
    _seed_gold(lake_root, "A2")
    _seed_gold_fingerprint(lake_root, ["A1", "A2"])
    _patch_pipeline(monkeypatch, ["A1", "A2", "A3"], {"A1": "h1", "A2": "h2-new", "A3": "h3"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["legi-medallion", "--lake-root", str(lake_root), "--delta", "--no-embed"],
    )

    assert legifrance_medallion.main() == 0

    pipeline = _FakeLegiPipeline.last
    assert pipeline is not None
    assert pipeline.gold_calls == [["A2", "A3"]]  # A1 jamais reconstruit

    payload = json.loads(capsys.readouterr().out)
    assert payload["delta"] is True
    assert payload["gold_skipped_unchanged"] == ["A1"]
    assert payload["gold_chunks_reused"] == 3
    assert payload["hydrated_from_object_storage"] is None  # local, pas d'OS


def test_delta_rebuilds_when_existing_gold_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "A1", "h1")
    corrupt = lake_root / "gold" / "chunks" / "A1.chunks.jsonl"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text('{"ok": 1}\nPAS DU JSON', encoding="utf-8")  # non-vide mais invalide
    _seed_gold_fingerprint(lake_root, ["A1"])  # config inchangée -> le rebuild vient du gold corrompu
    _patch_pipeline(monkeypatch, ["A1"], {"A1": "h1"})
    monkeypatch.setattr(sys, "argv", ["legi-medallion", "--lake-root", str(lake_root), "--delta", "--no-embed"])

    assert legifrance_medallion.main() == 0

    assert _FakeLegiPipeline.last is not None and _FakeLegiPipeline.last.gold_calls == [["A1"]]  # reconstruit
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_skipped_unchanged"] == []


def test_delta_rebuilds_when_gold_config_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Revue #313 P1 : A1 inchangé (silver+gold+hash) MAIS empreinte config
    # différente (embeddings changés) -> reconstruire, jamais réutiliser un gold
    # produit sous une autre config.
    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "A1", "h1")
    _seed_gold(lake_root, "A1", nb_chunks=2)
    _seed_gold_fingerprint(lake_root, ["A1"], enable_m3=True, enable_bge=True)  # config PRÉCÉDENTE différente
    _patch_pipeline(monkeypatch, ["A1"], {"A1": "h1"})
    monkeypatch.setattr(sys, "argv", ["legi-medallion", "--lake-root", str(lake_root), "--delta", "--no-embed"])

    assert legifrance_medallion.main() == 0

    assert _FakeLegiPipeline.last is not None and _FakeLegiPipeline.last.gold_calls == [["A1"]]  # reconstruit malgré silver inchangé
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_skipped_unchanged"] == []


def test_delta_hydrates_silver_gold_with_legifrance_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import assistant_rh_data_engineering.utils.object_storage as object_storage

    class _FakeSyncer:
        calls: list[dict[str, Any]] = []

        def __init__(self, config: Any) -> None:
            pass

        def download_medallion_root(
            self, root: Any, target_env: str, source_name: str = "legifrance", include_layers: tuple[str, ...] = ()
        ) -> dict[str, str]:
            _FakeSyncer.calls.append({"source": source_name, "layers": tuple(include_layers)})
            return {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}

        def sync_medallion_root(self, root: Any, target_env: str, **kwargs: Any) -> dict[str, str]:
            return {"bronze": "s3://x/bronze/", "silver": "s3://x/silver/", "gold": "s3://x/gold/"}

    _FakeSyncer.calls = []
    monkeypatch.setattr(object_storage, "ScalewayObjectStorageSync", _FakeSyncer)
    monkeypatch.setattr(object_storage.ObjectStorageConfig, "from_env", classmethod(lambda cls: SimpleNamespace()))

    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "A1", "h1")
    _seed_gold(lake_root, "A1", nb_chunks=2)
    _seed_gold_fingerprint(lake_root, ["A1"])
    _patch_pipeline(monkeypatch, ["A1"], {"A1": "h1"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["legi-medallion", "--lake-root", str(lake_root), "--delta", "--sync-object-storage", "--no-embed", "--target-env", "staging"],
    )

    assert legifrance_medallion.main() == 0

    # Hydratation silver+gold avec la source legifrance, avant traitement.
    assert _FakeSyncer.calls == [{"source": "legifrance", "layers": ("silver", "gold")}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["hydrated_from_object_storage"] == {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}
    assert payload["gold_skipped_unchanged"] == ["A1"]  # inchangé grâce à l'état hydraté


def test_delta_rejects_ingest_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["legi-medallion", "--delta", "--ingest"])

    with pytest.raises(SystemExit, match="incompatible avec --delta"):
        legifrance_medallion.main()


def test_delta_sync_excludes_bronze_when_reading_from_object_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Fix cron BUG A : le médaillon Legi LIT le bronze depuis l'Object Storage
    # (--from-object-storage) et ne le POSSÈDE pas -> le sync final ne doit PAS
    # toucher la couche bronze (sinon --delete-remote effacerait le bronze distant
    # depuis un bronze local vide, fatal en chaîne delta standalone).
    import assistant_rh_data_engineering.utils.object_storage as object_storage

    sync_calls: list[dict[str, Any]] = []

    class _FakeSyncer:
        def __init__(self, config: Any) -> None:
            pass

        def download_medallion_root(
            self, root: Any, target_env: str, source_name: str = "legifrance", include_layers: tuple[str, ...] = ()
        ) -> dict[str, str]:
            return {}

        def sync_medallion_root(
            self,
            root: Any,
            target_env: str,
            source_name: str = "legifrance",
            *,
            delete: bool = False,
            include_layers: tuple[str, ...] = ("bronze", "silver", "gold"),
        ) -> dict[str, str]:
            sync_calls.append({"delete": delete, "include_layers": tuple(include_layers)})
            return {"silver": "s3://x/silver/", "gold": "s3://x/gold/"}

    monkeypatch.setattr(object_storage, "ScalewayObjectStorageSync", _FakeSyncer)
    monkeypatch.setattr(object_storage.ObjectStorageConfig, "from_env", classmethod(lambda cls: SimpleNamespace()))

    lake_root = tmp_path / "lake"
    _seed_silver(lake_root, "A1", "h1")
    _seed_gold(lake_root, "A1", nb_chunks=1)
    _seed_gold_fingerprint(lake_root, ["A1"])
    _patch_pipeline(monkeypatch, ["A1"], {"A1": "h1"})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "legi-medallion",
            "--lake-root",
            str(lake_root),
            "--delta",
            "--from-object-storage",
            "--sync-object-storage",
            "--delete-remote",
            "--no-embed",
            "--target-env",
            "staging",
        ],
    )

    assert legifrance_medallion.main() == 0

    # Sync final : bronze EXCLU (lu depuis l'OS), delete appliqué à silver+gold seulement.
    assert sync_calls == [{"delete": True, "include_layers": ("silver", "gold")}]
