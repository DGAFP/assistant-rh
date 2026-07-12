"""Tests de l'énumérateur PISTE (tableMatieres → articles) — #289 E2.2.

Fonctions pures : aucun appel réseau. On vérifie le parcours récursif de la
structure et le filtre VIGUEUR sur un fixture représentatif.
"""

from __future__ import annotations

from assistant_rh_data_engineering.legifrance.piste import (
    articles_en_vigueur,
    walk_table_matieres,
)

# tableMatieres imbriquée : sections dans sections, articles à plusieurs niveaux,
# un noeud LEGITEXT (racine) qui ne doit PAS être compté comme article.
FIXTURE = {
    "cid": "LEGITEXT000044416551",  # racine, ignorée (pas LEGIARTI)
    "title": "CGFP",
    "sections": [
        {
            "title": "Livre Ier",
            "articles": [
                {"cid": "LEGIARTI000000000001", "etat": "VIGUEUR", "num": "L1"},
                {"cid": "LEGIARTI000000000002", "etat": "ABROGE", "num": "L2"},
            ],
            "sections": [
                {
                    "title": "Titre Ier",
                    "articles": [
                        {"id": "LEGIARTI000000000003", "etat": "VIGUEUR", "num": "L3"},
                    ],
                }
            ],
        }
    ],
}


def test_walk_table_matieres_finds_all_articles_at_any_depth() -> None:
    articles = walk_table_matieres(FIXTURE)
    assert {a.cid for a in articles} == {
        "LEGIARTI000000000001",
        "LEGIARTI000000000002",
        "LEGIARTI000000000003",
    }
    # le noeud LEGITEXT racine n'est pas un article
    assert "LEGITEXT000044416551" not in {a.cid for a in articles}
    etats = {a.cid: a.etat for a in articles}
    assert etats["LEGIARTI000000000002"] == "ABROGE"


def test_articles_en_vigueur_excludes_abroge_and_sorts() -> None:
    assert articles_en_vigueur(FIXTURE) == [
        "LEGIARTI000000000001",
        "LEGIARTI000000000003",
    ]


def test_empty_payload_yields_no_articles() -> None:
    assert walk_table_matieres({}) == []
    assert articles_en_vigueur({}) == []


def test_walk_aggregates_versions_per_article_vigueur_wins() -> None:
    # Revue #307 (P1) : lawDecree émet UN NŒUD PAR VERSION. Reproduit le décret
    # 86-83 art. 50 : version courante VIGUEUR + version d'origine ABROGE pour
    # le même cid — l'article doit rester VIGUEUR, jamais dernier-gagne.
    payload = {
        "articles": [
            {"id": "LEGIARTI000045662481", "cid": "LEGIARTI000006486629", "etat": "VIGUEUR", "num": "50"},
            {"id": "LEGIARTI000006486629", "cid": "LEGIARTI000006486629", "etat": "ABROGE", "num": "50"},
        ]
    }

    articles = walk_table_matieres(payload)

    assert len(articles) == 1
    article = articles[0]
    assert article.cid == "LEGIARTI000006486629"  # identité chronique
    assert article.etat == "VIGUEUR"  # la version en vigueur gagne
    assert article.version_id == "LEGIARTI000045662481"
    assert set(article.alias_ids) == {"LEGIARTI000045662481", "LEGIARTI000006486629"}


def test_walk_handles_jorfarti_cids_with_legiarti_version() -> None:
    # Revue #307 (P1 + P1 bis) : les arrêtés LODA portent cid=JORFARTI +
    # id=LEGIARTI. L'article ne doit pas être jeté, et l'identité stable est
    # le cid JORFARTI (stable à travers les versions) — PAS l'id LEGIARTI de
    # la version courante, qui churnerait à chaque modification.
    payload = {
        "articles": [
            {"id": "LEGIARTI000024082428", "cid": "JORFARTI000024080293", "etat": "VIGUEUR", "num": "10"},
        ]
    }

    articles = walk_table_matieres(payload)

    assert len(articles) == 1
    article = articles[0]
    assert article.cid == "JORFARTI000024080293"  # identité stable = cid API
    assert article.etat == "VIGUEUR"
    assert article.version_id == "LEGIARTI000024082428"
    assert set(article.alias_ids) == {"JORFARTI000024080293", "LEGIARTI000024082428"}


def test_walk_jorfarti_identity_is_stable_across_versions() -> None:
    # Évolution du même article entre deux dates : nouvelle version LEGI,
    # même cid JORFARTI. L'identité ne bouge pas ; l'ancienne version reste
    # attribuable via les alias.
    payload = {
        "articles": [
            {"id": "LEGIARTI000024082428", "cid": "JORFARTI000024080293", "etat": "ABROGE", "num": "10"},
            {"id": "LEGIARTI000050000001", "cid": "JORFARTI000024080293", "etat": "VIGUEUR", "num": "10"},
        ]
    }

    articles = walk_table_matieres(payload)

    assert len(articles) == 1
    article = articles[0]
    assert article.cid == "JORFARTI000024080293"  # identité inchangée entre versions
    assert article.etat == "VIGUEUR"
    assert article.version_id == "LEGIARTI000050000001"
    assert {"LEGIARTI000024082428", "LEGIARTI000050000001"} <= set(article.alias_ids)
