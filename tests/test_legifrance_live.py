"""Contrat de matérialisation live PISTE pour l'issue #424."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assistant_rh_data_engineering.legifrance import LegifrancePipeline, LegifrancePipelineConfig
from assistant_rh_data_engineering.legifrance.bronze import BronzeRepository, LegifranceBronzeBuilder
from assistant_rh_data_engineering.legifrance.config import BronzeConfig, LakePaths
from assistant_rh_data_engineering.legifrance.live import (
    LegifranceLiveMaterializer,
    bronze_payload_from_response,
)
from assistant_rh_data_engineering.legifrance.piste import CodeArticle

JORFARTI = "JORFARTI000039728025"
VERSION_2026 = "LEGIARTI000054638420"
CHRONIQUE = "LEGIARTI000039728025"


def _get_article_response() -> dict[str, Any]:
    return {
        "article": {
            "id": VERSION_2026,
            "cid": CHRONIQUE,
            "num": "2",
            "etat": "VIGUEUR",
            "dateDebut": "2026-08-08",
            "dateFin": "2999-01-01",
            "texteHtml": (
                "<p>Le montant ne peut pas être inférieur aux montants suivants :</p>"
                "<ul><li>un sixième de mois jusqu'à dix ans ;</li>"
                "<li>un cinquième de mois jusqu'à quinze ans ;</li>"
                "<li>un quart de mois jusqu'à vingt ans ;</li>"
                "<li>un tiers de mois jusqu'à vingt-quatre ans.</li></ul>"
            ),
            "fullSectionsTitre": "Chapitre 1er : Indemnité de rupture conventionnelle",
            "sectionParentCid": "LEGISCTA000039728023",
            "sectionParentTitre": "Chapitre 1er",
            "textTitles": [
                {
                    "id": "LEGITEXT000039728019",
                    "cid": "LEGITEXT000039728019",
                    "titre": "Décret n° 2019-1596",
                    "titreLong": "Décret n° 2019-1596 du 31 décembre 2019 relatif à l'indemnité spécifique de rupture conventionnelle",
                    "nature": "DECRET",
                }
            ],
            "lienModifications": [
                {
                    "articleId": "JORFARTI000054636255",
                    "articleNum": "3",
                    "linkType": "MODIFIE",
                    "textTitle": "Décret n°2026-746 du 6 août 2026 - art. 3",
                    "textCid": "JORFTEXT000054635915",
                }
            ],
        },
        "executionTime": 12,
    }


def _expected() -> CodeArticle:
    return CodeArticle(
        cid=JORFARTI,
        etat="VIGUEUR",
        num="2",
        version_id=VERSION_2026,
        alias_ids=(JORFARTI, VERSION_2026),
    )


def test_getarticle_projection_rekeys_jorfarti_and_preserves_2026_content() -> None:
    canonical, payload = bronze_payload_from_response(_expected(), _get_article_response())

    assert canonical.cid == CHRONIQUE
    assert canonical.version_id == VERSION_2026
    assert {JORFARTI, CHRONIQUE, VERSION_2026} <= set(canonical.alias_ids)
    assert payload["cid"] == CHRONIQUE
    assert payload["article_id"] == VERSION_2026
    assert payload["start_date"] == "2026-08-08"
    assert "un sixième de mois" in payload["text"]
    assert "un tiers de mois" in payload["text"]
    assert payload["lien_modifications"][0]["linkType"] == "MODIFIE"


def test_getarticle_projection_preserves_stable_jorfarti_for_loda_article() -> None:
    response = _get_article_response()
    response["article"]["cid"] = JORFARTI

    canonical, payload = bronze_payload_from_response(_expected(), response)

    assert canonical.cid == JORFARTI
    assert canonical.version_id == VERSION_2026
    assert {JORFARTI, VERSION_2026} <= set(canonical.alias_ids)
    assert payload["cid"] == JORFARTI
    assert payload["article_id"] == VERSION_2026


def test_live_materializer_archives_raw_and_builds_silver_gold(tmp_path: Path) -> None:
    config = LegifrancePipelineConfig(paths=LakePaths(root_dir=tmp_path / "lake"))
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    config.gold.export_parquet = False
    config.gold.export_npy = False
    pipeline = LegifrancePipeline(config)

    sync_calls: list[dict[str, Any]] = []

    class _Syncer:
        def sync_medallion_root(self, root: Path, target_env: str, **kwargs: Any) -> dict[str, str]:
            sync_calls.append({"root": root, "target_env": target_env, **kwargs})
            return {"bronze": "s3://bronze", "silver": "s3://silver", "gold": "s3://gold"}

    materializer = LegifranceLiveMaterializer(pipeline, object_storage=_Syncer(), target_env="staging")
    response = _get_article_response()

    bundle = materializer.materialize(_expected(), response)
    destinations = materializer.sync()

    raw_path = pipeline.bronze_repo.piste_articles_dir / f"{VERSION_2026}.json"
    assert json.loads(raw_path.read_text(encoding="utf-8")) == response
    assert bundle.document["short_id"] == CHRONIQUE
    assert bundle.document["metadata"]["version_id"] == VERSION_2026
    assert "un sixième de mois" in bundle.document["doc_markdown"]
    assert bundle.chunks[0]["cid"] == CHRONIQUE
    assert bundle.chunks[0]["url"].endswith(VERSION_2026)
    assert bundle.chunks[0]["lien_modifications"][0]["linkType"] == "MODIFIE"
    assert destinations == {"bronze": "s3://bronze", "silver": "s3://silver", "gold": "s3://gold"}
    assert sync_calls[0]["delete"] is False
    assert sync_calls[0]["include_layers"] == ("bronze", "silver", "gold")


def test_local_bronze_reload_keeps_live_payload_ahead_of_bulk_xml(tmp_path: Path, monkeypatch: Any) -> None:
    """Un run médaillon ultérieur ne doit jamais restaurer le dump figé."""
    repository = BronzeRepository(tmp_path / "bronze")
    builder = LegifranceBronzeBuilder(BronzeConfig())
    _, live_payload = bronze_payload_from_response(_expected(), _get_article_response())
    builder.persist_article_payload(repository, live_payload)

    old_bulk = {
        **live_payload,
        "article_id": "LEGIARTI000039728025",
        "version_id": "LEGIARTI000039728025",
        "text": "Ancien barème du dump manuel.",
        "origin": "legi_bulk_raw",
    }

    def fake_xml(_repository: BronzeRepository) -> dict[str, dict[str, Any]]:
        normalized = builder._normalize_article_payload(old_bulk)
        _repository.save_article_payload(normalized["short_id"], normalized)
        return {normalized["article_id"]: normalized}

    monkeypatch.setattr(builder, "_load_article_payloads_from_xml", fake_xml)

    selected = builder._load_local_article_payloads(repository)

    assert len(selected) == 1
    assert selected[0]["version_id"] == VERSION_2026
    assert "un sixième" in selected[0]["text"]
    persisted = json.loads((repository.articles_dir / f"{CHRONIQUE}.json").read_text(encoding="utf-8"))
    assert persisted["version_id"] == VERSION_2026
    assert persisted["origin"] == "piste_get_article"
