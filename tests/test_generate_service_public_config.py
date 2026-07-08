"""Tests du générateur config Service-Public depuis Grist (E2.1, #289).

Fonctions pures : aucun appel Grist. On vérifie le filtrage (corpus/statut/abroge)
et l'extraction du F-code.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_service_public_config as gen  # noqa: E402


def _rec(**fields) -> dict:
    return {"id": 1, "fields": fields}


def test_selected_fiche_ids_filters_by_corpus_statut_abroge() -> None:
    records = [
        _rec(source_corpus="Service-public", statut="ingere", id_extraction="F12386"),
        _rec(source_corpus="Service-public", statut="a_ingerer", id_extraction="F465"),
        _rec(source_corpus="Service-public", statut="a_supprimer", id_extraction="F999"),  # exclu (intention)
        _rec(source_corpus="Service-public", statut="ingere", abroge="oui", id_extraction="F888"),  # exclu (abrogé)
        _rec(source_corpus="MATTE", statut="ingere", id_extraction="F777"),  # exclu (autre corpus)
        _rec(source_corpus="Service-public", statut="ingere", titre_document="Sans code"),  # exclu (pas de F-code)
        _rec(source_corpus="Service-public", statut="ingere", id_extraction="F12386"),  # doublon
    ]

    # tri lexical (déterministe) : "F12386" < "F465"
    assert gen.selected_fiche_ids(records) == ["F12386", "F465"]


def test_extract_fiche_id_prefers_id_extraction_then_title_then_uid() -> None:
    assert gen.extract_fiche_id({"id_extraction": "F1906", "titre_document": "F9999"}) == "F1906"
    assert gen.extract_fiche_id({"titre_document": "Congé (F1906) du contractuel"}) == "F1906"
    assert gen.extract_fiche_id({"uid": "f1906"}) == "F1906"  # normalisé en majuscule
    assert gen.extract_fiche_id({"titre_document": "aucun code"}) is None


def test_is_service_public_case_insensitive() -> None:
    assert gen.is_service_public({"source_corpus": "Service-public"}) is True
    assert gen.is_service_public({"source_corpus": "service-PUBLIC"}) is True
    assert gen.is_service_public({"source_corpus": "MATTE"}) is False
    assert gen.is_service_public({}) is False


def test_render_config_shape() -> None:
    config = gen.render_config(["F1", "F2"])
    assert config["source"] == "service_public"
    assert config["situation"] == "FPE"
    assert config["fiche_ids"] == ["F1", "F2"]
    assert "Grist" in config["description"]
