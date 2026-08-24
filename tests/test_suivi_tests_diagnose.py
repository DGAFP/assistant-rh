"""Tests du diagnostic par étape (issue #298) — src/suivi_tests/diagnose.py.

Les fixtures reproduisent les patterns d'échec observés sur la campagne
staging suivi-tests-20260708 : doc écarté par le selector (prime SSD, FMD),
perdu au rerank, perdu à l'agrégation, absent du retrieval.
"""

from __future__ import annotations

import json

from src.suivi_tests.campaign import load_expected_docs, resolve_ministry_id
from src.suivi_tests.diagnose import diagnose_run, parse_kept_indices


def _chunk(doc_id: str, title: str, heading: str = "", final_score: float = 0.3, rerank_score: float | None = None) -> dict:
    return {
        "doc_id": doc_id,
        "doc_title": title,
        "section_heading": heading,
        "final_score": final_score,
        "rerank_score": rerank_score,
    }


def _context_item(document_id: str, heading: str, score: float = 0.5) -> dict:
    return {"document_id": document_id, "heading": heading, "score": score}


SP_CHUNK = _chunk("sp-1", "Remboursement des frais de transport domicile-travail (fonction publique)", "FPT")
FMD_CHUNK = _chunk("masa-fmd", "2026-1_prise en charge FMD", "2. Les exclusions au dispositif du FMD", 0.19, 0.849)


def _row(
    raw: list[dict],
    reranked: list[dict],
    context_items: list[dict],
    kept: str,
    retrieved: list[dict],
    as_json: bool = False,
) -> dict:
    row = {
        "v3_chunks_raw": raw,
        "v3_chunks_after_rerank": reranked,
        "v3_context_items_full": context_items,
        "v3_selector_kept_indices": kept,
        "retrieved": retrieved,
    }
    if as_json:
        row = {key: (json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value) for key, value in row.items()}
    return row


class TestDiagnoseRun:
    def test_ecarte_selector_reproduit_cas_fmd(self):
        """Cas masa-63 : le doc FMD est dans les context items mais le selector
        ne garde que la fiche SP → « écarté par le selector »."""
        row = _row(
            raw=[SP_CHUNK, FMD_CHUNK],
            reranked=[SP_CHUNK, FMD_CHUNK],
            context_items=[_context_item("sp-1", "FPT"), _context_item("masa-fmd", "2. Les exclusions au dispositif du FMD")],
            kept="0",
            retrieved=[{"source_name": "Remboursement des frais de transport domicile-travail (fonction publique)"}],
        )
        diagnosis = diagnose_run(row, ["prise en charge FMD"])
        assert diagnosis.overall == "ecarte_selector"
        diag = diagnosis.patterns[0]
        assert diag.retrieval_rank == 2
        assert diag.rerank_rank == 2
        assert diag.context_index == 1
        assert not diag.kept_by_selector

    def test_ok_quand_doc_dans_sources_finales(self):
        row = _row(
            raw=[FMD_CHUNK],
            reranked=[FMD_CHUNK],
            context_items=[_context_item("masa-fmd", "Exclusions")],
            kept="0",
            retrieved=[{"source_name": "2026-1_prise en charge FMD"}],
        )
        diagnosis = diagnose_run(row, ["prise en charge FMD"])
        assert diagnosis.overall == "ok"
        assert diagnosis.patterns[0].in_final_sources

    def test_perdu_rerank(self):
        row = _row(
            raw=[SP_CHUNK, FMD_CHUNK],
            reranked=[SP_CHUNK],
            context_items=[_context_item("sp-1", "FPT")],
            kept="0",
            retrieved=[],
        )
        diagnosis = diagnose_run(row, ["prise en charge FMD"])
        assert diagnosis.overall == "perdu_rerank"

    def test_perdu_agregation(self):
        """Survit au rerank mais absent des ~20 sections proposées au selector."""
        row = _row(
            raw=[FMD_CHUNK],
            reranked=[FMD_CHUNK],
            context_items=[_context_item("sp-1", "FPT")],
            kept="0",
            retrieved=[],
        )
        diagnosis = diagnose_run(row, ["prise en charge FMD"])
        assert diagnosis.overall == "perdu_agregation"

    def test_absent_retrieval(self):
        row = _row(raw=[SP_CHUNK], reranked=[SP_CHUNK], context_items=[], kept="", retrieved=[])
        diagnosis = diagnose_run(row, ["Instruction RDR SD hors IDF"])
        assert diagnosis.overall == "absent_retrieval"

    def test_matching_insensible_accents_et_casse(self):
        chunk = _chunk("mso-1", "2. Présentation DREETS-SGCD de la déconcentration des actes 01012026")
        row = _row(raw=[chunk], reranked=[], context_items=[], kept="", retrieved=[])
        diagnosis = diagnose_run(row, ["presentation dreets-sgcd de la DECONCENTRATION"])
        assert diagnosis.patterns[0].retrieval_rank == 1

    def test_colonnes_json_string_comme_en_base(self):
        """Les colonnes chat_runs arrivent en TEXT JSON : même verdict."""
        row = _row(
            raw=[SP_CHUNK, FMD_CHUNK],
            reranked=[SP_CHUNK, FMD_CHUNK],
            context_items=[_context_item("sp-1", "FPT"), _context_item("masa-fmd", "Exclusions FMD")],
            kept="0",
            retrieved=[{"source_name": "Remboursement des frais de transport"}],
            as_json=True,
        )
        diagnosis = diagnose_run(row, ["prise en charge FMD"])
        assert diagnosis.overall == "ecarte_selector"

    def test_alternatives_ou_dans_un_pattern(self):
        """Cas mso-41 : deux docs porteurs possibles (« A|B »), un seul suffit."""
        liste = _chunk("mso-liste", "Liste des actes déconcentrés pour les agents contractuels", "Congé maternité", 0.4)
        row = _row(
            raw=[liste],
            reranked=[liste],
            context_items=[_context_item("mso-liste", "Congé maternité")],
            kept="0",
            retrieved=[{"source_name": "Liste des actes déconcentrés pour les agents contractuels"}],
        )
        diagnosis = diagnose_run(row, ["Présentation DREETS-SGCD|Liste des actes déconcentrés"])
        assert diagnosis.overall == "ok"

    def test_entrees_de_liste_en_et(self):
        """Cas matte-8 : la fiche ministérielle sort en sources mais le décret
        attendu meurt au rerank → l'ensemble est KO (entrées = ET)."""
        fiche = _chunk("matte-7-1", "Fiche MATTE n° 7-1 - Les conditions particulières", "")
        decret = _chunk("dgafp-86-83", "Décret n° 86-83 du 17 janvier 1986", "")
        row = _row(
            raw=[fiche, decret],
            reranked=[fiche],
            context_items=[_context_item("matte-7-1", "Conditions particulières")],
            kept="0",
            retrieved=[{"source_name": "Fiche MATTE n° 7-1 - Les conditions particulières"}],
        )
        diagnosis = diagnose_run(row, ["Fiche MATTE n° 7-1", "86-83"])
        assert diagnosis.patterns[0].verdict == "ok"
        assert diagnosis.patterns[1].verdict == "perdu_rerank"
        assert diagnosis.overall == "perdu_rerank"

    def test_kept_sur_un_item_ulterieur_signale_perte_context_builder(self):
        """Plusieurs items du même doc : le premier donne l'index, un item
        gardé plus loin prouve le passage du selector, pas l'arrivée finale."""
        doc = _chunk("d1", "Vademecum de gestion", "Le temps partiel")
        row = _row(
            raw=[doc],
            reranked=[doc],
            context_items=[_context_item("d1", "Pour naissance ou adoption"), _context_item("d1", "Le temps partiel")],
            kept="1",
            retrieved=[],
        )
        diagnosis = diagnose_run(row, ["Vademecum"])
        diag = diagnosis.patterns[0]
        assert diag.context_index == 0
        assert diag.kept_by_selector
        assert not diag.in_final_sources
        assert diag.verdict == "perdu_context_builder"
        assert diagnosis.overall == "perdu_context_builder"

    def test_sans_pattern_non_evalue(self):
        assert diagnose_run(_row([], [], [], "", []), []).overall == "non_evalue"


class TestParseKeptIndices:
    def test_csv(self):
        assert parse_kept_indices("1,5") == [1, 5]

    def test_vide_et_none(self):
        assert parse_kept_indices("") == []
        assert parse_kept_indices(None) == []

    def test_liste(self):
        assert parse_kept_indices([0, 2]) == [0, 2]


class TestCampaignHelpers:
    def test_resolve_ministry_id_ministeres_et_fallback(self):
        assert resolve_ministry_id("MSO") == "mso"
        assert resolve_ministry_id("masa") == "masa"
        # Convention resolve_question_scope : non-ministériel → parcours MATTE.
        assert resolve_ministry_id("SP") == "matte"
        assert resolve_ministry_id("") == "matte"
        assert resolve_ministry_id(None) == "matte"

    def test_load_expected_docs_ignore_les_commentaires(self, tmp_path):
        path = tmp_path / "expected.json"
        path.write_text(
            json.dumps({"_comment": "doc", "61": ["prime de fidélisation SSD"], "63": ["prise en charge FMD", ""]}),
            encoding="utf-8",
        )
        mapping = load_expected_docs(path)
        assert mapping == {61: ["prime de fidélisation SSD"], 63: ["prise en charge FMD"]}

    def test_load_expected_docs_fichier_absent(self, tmp_path):
        assert load_expected_docs(tmp_path / "absent.json") == {}
