"""Identité stable des articles Légifrance (revue #307, P1 n°2).

Le XML du dump DILA ne porte PAS le cid chronique : le parseur retombe sur
l'ID de version. Le mapping alias→chronique (cache follow-live PISTE, clé
``articles``) est appliqué en BRONZE — avant ``short_id`` et ``source_url`` —
pour que toute la chaîne (silver ``doc_id`` compris) soit keyée chronique.

Depuis #350 : l'identité reste chronique, mais l'URL d'affichage des chunks
gold est construite sur l'id de VERSION (seule forme acceptée par la route
Légifrance ``article_lc``).
"""

from __future__ import annotations

from assistant_rh_data_engineering.legifrance.bronze import LegifranceBronzeBuilder
from assistant_rh_data_engineering.legifrance.config import BronzeConfig, EmbeddingConfig, GoldConfig
from assistant_rh_data_engineering.legifrance.gold import LegifranceGoldBuilder

VERSION_ID = "LEGIARTI000045662481"
CHRONIQUE = "LEGIARTI000006486629"


def _payload(**overrides):
    base = {
        "article_id": VERSION_ID,
        "cid": VERSION_ID,  # le parseur retombe sur l'ID de version (pas de CID XML)
        "num_article": "50",
        "text": "Contenu de l'article 50.",
        "title": "Décret n°86-83",
        "category": "DECRET",
        "status": "VIGUEUR",
    }
    base.update(overrides)
    return base


def test_bronze_mapping_restores_chronical_identity() -> None:
    config = BronzeConfig(article_cid_mapping={VERSION_ID: CHRONIQUE})
    builder = LegifranceBronzeBuilder(config)

    normalized = builder._normalize_article_payload(_payload())

    assert normalized["cid"] == CHRONIQUE
    assert normalized["short_id"] == CHRONIQUE  # identité corpus stable
    assert normalized["version_id"] == VERSION_ID
    assert CHRONIQUE in normalized["source_url"]  # URL stable à travers les versions


def test_bronze_without_mapping_keeps_version_identity() -> None:
    # Ancien format de cache (sans clé articles) : comportement historique,
    # signalé par le médaillon en [warn] — jamais un crash.
    builder = LegifranceBronzeBuilder(BronzeConfig())

    normalized = builder._normalize_article_payload(_payload())

    assert normalized["cid"] == VERSION_ID
    assert normalized["short_id"] == VERSION_ID


def _gold_builder() -> LegifranceGoldBuilder:
    return LegifranceGoldBuilder(
        EmbeddingConfig(enable_m3=False, enable_bge_scaleway=False),
        GoldConfig(export_parquet=False, export_npy=False),
    )


def _gold_document(metadata: dict) -> dict:
    return {
        "doc_id": "doc-350",
        "short_id": CHRONIQUE,
        "title": "Décret n°86-83",
        "full_title": "Décret n°86-83",
        "source_url": f"https://www.legifrance.gouv.fr/loda/article_lc/{CHRONIQUE}",
        "metadata": metadata,
    }


def _gold_sections() -> list[dict]:
    return [
        {
            "section_id": "s0",
            "section_markdown": "Contenu de l'article 50.",
            "heading_path": "Article 50",
            "section_type": "article",
        }
    ]


def test_gold_url_uses_version_id_while_identity_stays_chronique() -> None:
    # #350 : la route article_lc n'accepte que des ids de VERSION — l'URL
    # d'affichage est construite sur metadata.article_id, l'identité (chunk_id,
    # cid) reste keyée chronique.
    metadata = {
        "article_id": VERSION_ID,
        "cid": CHRONIQUE,
        "num_article": "50",
        "category": "DECRET",
        "status": "VIGUEUR",
    }

    chunks = _gold_builder().build_chunks(_gold_document(metadata), _gold_sections())

    assert chunks[0]["chunk_id"] == f"{CHRONIQUE}_0"
    assert chunks[0]["cid"] == CHRONIQUE
    # Route loda respectée (category != CODE), id de version dans l'URL.
    assert chunks[0]["url"] == f"https://www.legifrance.gouv.fr/loda/article_lc/{VERSION_ID}"


def test_gold_url_falls_back_to_source_url_without_article_id() -> None:
    metadata = {
        "cid": CHRONIQUE,
        "num_article": "50",
        "category": "DECRET",
        "status": "VIGUEUR",
    }

    chunks = _gold_builder().build_chunks(_gold_document(metadata), _gold_sections())

    assert chunks[0]["url"] == f"https://www.legifrance.gouv.fr/loda/article_lc/{CHRONIQUE}"
