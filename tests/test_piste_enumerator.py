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
