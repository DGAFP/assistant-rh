"""Contrat de matérialisation live PISTE pour l'issue #424."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_rh_data_engineering.legifrance import LegifrancePipeline, LegifrancePipelineConfig
from assistant_rh_data_engineering.legifrance.bronze import BronzeRepository, LegifranceBronzeBuilder
from assistant_rh_data_engineering.legifrance.config import BronzeConfig, LakePaths
from assistant_rh_data_engineering.legifrance.live import (
    LegifranceLiveMaterializer,
    bronze_payload_from_response,
    canonicalize_toc_from_silver,
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


@pytest.mark.parametrize("as_of", ["2026-09-12", "2028-12-31"])
def test_deferred_repeal_keeps_current_article_until_effective_date(as_of: str) -> None:
    response = _get_article_response()
    response["article"].update(etat="ABROGE_DIFF", dateDebut="2022-03-01", dateFin="2029-01-01")
    timestamp = int(datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp() * 1000)

    canonical, payload = bronze_payload_from_response(_expected(), response, as_of_millis=timestamp)

    assert canonical.etat == payload["status"] == "VIGUEUR"
    assert payload["end_date"] == "2029-01-01"
    assert response["article"]["etat"] == "ABROGE_DIFF"  # raw response stays auditable


@pytest.mark.parametrize("as_of", ["2022-02-28", "2029-01-01"])
def test_deferred_repeal_with_inconsistent_current_toc_fails_closed(as_of: str) -> None:
    response = _get_article_response()
    response["article"].update(etat="ABROGE_DIFF", dateDebut="2022-03-01", dateFin="2029-01-01")
    timestamp = int(datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp() * 1000)

    with pytest.raises(RuntimeError, match="abrogation différée incompatible"):
        bronze_payload_from_response(_expected(), response, as_of_millis=timestamp)


def test_deferred_repeal_compares_numeric_instants_without_truncating_utc_dates() -> None:
    response = _get_article_response()
    end = int(datetime.fromisoformat("2029-01-01T00:00:00+01:00").timestamp() * 1000)
    as_of = int(datetime.fromisoformat("2028-12-31T12:00:00+01:00").timestamp() * 1000)
    response["article"].update(etat="ABROGE_DIFF", dateDebut=1646092800000, dateFin=end)

    canonical, _ = bronze_payload_from_response(_expected(), response, as_of_millis=as_of)
    assert canonical.etat == "VIGUEUR"
    with pytest.raises(RuntimeError, match="abrogation différée incompatible"):
        bronze_payload_from_response(_expected(), response, as_of_millis=end)


@pytest.mark.parametrize("origin", [None, "legi_bulk_raw"])
def test_legacy_silver_version_key_cannot_override_official_toc_cid(origin: str | None) -> None:
    document = {
        "short_id": VERSION_2026,
        "metadata": {"cid": VERSION_2026, "article_id": VERSION_2026, "origin": origin},
    }

    assert canonicalize_toc_from_silver({"text": [_expected()]}, [document]) == {"text": [_expected()]}


@pytest.mark.parametrize("cid", [CHRONIQUE, JORFARTI])
@pytest.mark.parametrize("reverse", [False, True])
def test_verified_getarticle_mapping_wins_over_legacy_silver_regardless_of_order(cid: str, reverse: bool) -> None:
    documents = [
        {"short_id": VERSION_2026, "metadata": {"cid": VERSION_2026, "article_id": VERSION_2026, "origin": "legi_bulk_raw"}},
        {"short_id": cid, "metadata": {"cid": cid, "article_id": VERSION_2026, "origin": "piste_get_article"}},
    ]
    article = CodeArticle(VERSION_2026, "VIGUEUR", "2", VERSION_2026, (VERSION_2026,))
    if reverse:
        documents.reverse()

    canonical = canonicalize_toc_from_silver({"text": [article]}, documents)["text"][0]

    assert canonical.cid == cid
    assert canonical.version_id == VERSION_2026
    assert {cid, VERSION_2026} <= set(canonical.alias_ids)


def test_conflicting_verified_silver_identities_fail_closed() -> None:
    documents = [
        {"short_id": cid, "metadata": {"cid": cid, "article_id": VERSION_2026, "origin": "piste_get_article"}} for cid in (CHRONIQUE, JORFARTI)
    ]

    with pytest.raises(RuntimeError, match="CID.*contradictoires"):
        canonicalize_toc_from_silver({"text": [_expected()]}, documents)


@pytest.mark.parametrize("remote", [False, True])
def test_bronze_replay_keeps_live_cid_when_old_version_file_sorts_after_it(tmp_path: Path, remote: bool) -> None:
    repository = BronzeRepository(tmp_path / "bronze")
    builder = LegifranceBronzeBuilder(BronzeConfig())
    response = _get_article_response()
    response["article"]["cid"] = JORFARTI
    _, live = bronze_payload_from_response(_expected(), response)
    legacy = {**live, "cid": VERSION_2026, "short_id": VERSION_2026, "origin": "legi_bulk_raw"}
    builder.persist_article_payload(repository, live)
    builder.persist_article_payload(repository, legacy)

    if remote:
        objects = [SimpleNamespace(key=f"{uid}.json", payload=payload) for uid, payload in [(JORFARTI, live), (VERSION_2026, legacy)]]
        storage = SimpleNamespace(
            list_medallion_objects=lambda *args: objects,
            read_text_object=lambda obj: json.dumps(obj.payload),
        )
        payloads = builder._load_article_payloads_from_remote_json(storage, "prod")
    else:
        payloads = builder._load_article_payloads_from_json(repository)

    assert payloads[VERSION_2026]["cid"] == JORFARTI
    assert payloads[VERSION_2026]["origin"] == "piste_get_article"


def test_bronze_live_identity_overrides_conflicting_cached_toc_mapping(tmp_path: Path) -> None:
    builder = LegifranceBronzeBuilder(BronzeConfig(article_cid_mapping={VERSION_2026: VERSION_2026, JORFARTI: VERSION_2026}))
    response = _get_article_response()
    response["article"]["cid"] = JORFARTI
    _, payload = bronze_payload_from_response(_expected(), response)

    asset = builder.persist_article_payload(BronzeRepository(tmp_path / "bronze"), payload)

    assert asset.short_id == JORFARTI
    assert json.loads(asset.payload_path.read_text())["cid"] == JORFARTI


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
