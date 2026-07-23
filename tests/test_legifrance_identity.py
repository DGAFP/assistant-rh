"""Identité stable des articles Légifrance (revue #307, P1 n°2).

Le XML du dump DILA ne porte PAS le cid chronique : le parseur retombe sur
l'ID de version. Le mapping alias→chronique (cache follow-live PISTE, clé
``articles``) est appliqué en BRONZE — avant ``short_id`` et ``source_url`` —
pour que toute la chaîne (silver ``doc_id`` compris) soit keyée chronique.
"""

from __future__ import annotations

from assistant_rh_data_engineering.legifrance.bronze import LegifranceBronzeBuilder
from assistant_rh_data_engineering.legifrance.config import BronzeConfig

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
