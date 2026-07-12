"""Tests de la réconciliation delta Légifrance (E2.3-b v2, #289).

Modèle « texte suivi » unifié : chaque ligne Grist Légifrance = un texte suivi
en live via PISTE (``legifrance_code`` → LEGITEXT, ``legifrance_texte`` →
JORFTEXT), dont les articles sont dérivés de sa TOC. Trois niveaux :
- fonctions pures (``select_legifrance_rows`` / ``build_legifrance_plan`` /
  ``plan_summary``) — aucun I/O ;
- job (``ingest_delta``) avec Grist, PISTE et writer factices en mémoire ;
- ``main()`` end-to-end (argparse → clients → ingest_delta).

Un test croise les statuts actifs avec ``service_public/reconcile.py`` pour
garantir que les deux réconciliations ne divergent pas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import requests
from assistant_rh_data_engineering.jobs import legifrance_ingestion
from assistant_rh_data_engineering.legifrance import reconcile
from assistant_rh_data_engineering.legifrance.piste import CodeArticle, PisteError
from assistant_rh_data_engineering.reconciliation import Confidence
from assistant_rh_data_engineering.service_public import reconcile as sp_reconcile

LEGITEXT = "LEGITEXT000044416551"
LEGITEXT2 = "LEGITEXT000006068842"
JORF_D1 = "JORFTEXT000000000101"
JORF_D2 = "JORFTEXT000000000201"


def _rec(record_id: int, **fields: Any) -> dict[str, Any]:
    return {"id": record_id, "fields": fields}


def _legi(**fields: Any) -> dict[str, Any]:
    base = {"source_corpus": "Interministériel/Légifrance"}
    base.update(fields)
    return base


def _code(statut: str = "ingere", **fields: Any) -> dict[str, Any]:
    return _legi(type_id="legifrance_code", id_extraction=LEGITEXT, titre_document="CGFP", statut=statut, **fields)


def _texte(jorftext: str, statut: str = "ingere", **fields: Any) -> dict[str, Any]:
    return _legi(type_id="legifrance_texte", jorftext=jorftext, statut=statut, **fields)


def _corpus(**entries: tuple[str, int]) -> dict[str, dict[str, Any]]:
    return {uid: {"doc_id": f"d-{uid}", "checksum": checksum, "nb_chunks": nb} for uid, (checksum, nb) in entries.items()}


def _arts(*specs: tuple[str, ...]) -> list[CodeArticle]:
    """Specs ``(cid, etat)`` ou ``(cid, etat, version_id)`` -> CodeArticle."""
    return [CodeArticle(cid=spec[0], etat=spec[1], version_id=spec[2] if len(spec) > 2 else "") for spec in specs]


# --- select_legifrance_rows -----------------------------------------------------


def test_select_classifies_code_texte_out_of_scope_and_pending_mapping(capsys: Any) -> None:
    records = [
        _rec(1, **_code()),
        _rec(2, **_texte(JORF_D1, statut="a_ingerer")),
        _rec(3, **_texte("", statut="a_ingerer")),  # matcher pas passé -> pending_mapping
        _rec(4, **_legi(titre_document="Circulaire hors périmètre", document_ids="CIRC1", statut="a_ingerer")),  # sans type_id
        _rec(5, **_legi(type_id="legifrance_texte", url_legifrance=f"https://www.legifrance.gouv.fr/loda/id/{JORF_D2}", statut="a_supprimer")),
        _rec(6, source_corpus="service-public", id_extraction="F1", statut="ingere"),  # autre corpus
    ]

    selection = reconcile.select_legifrance_rows(records)

    assert [(r.kind, r.uid) for r in selection.rows] == [
        ("texte", JORF_D1),
        ("texte", JORF_D2),
        ("code", LEGITEXT),
    ]
    code = selection.code_rows[0]
    assert code.active and code.record_id == 1
    assert selection.texte_rows[1].abrogated  # résolu via url_legifrance
    assert selection.out_of_scope == (4,)
    assert selection.out_of_scope_uids == ("CIRC1",)
    assert selection.pending_mapping == (3,)
    assert "sans JORFTEXT résoluble" in capsys.readouterr().out


def test_extract_jorftext_from_column_id_extraction_and_url() -> None:
    assert reconcile.extract_jorftext({"jorftext": "jorftext000000509867"}) == "JORFTEXT000000509867"
    assert reconcile.extract_jorftext({"id_extraction": "JORFTEXT000000509867"}) == "JORFTEXT000000509867"
    assert reconcile.extract_jorftext({"url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000509867/"}) == "JORFTEXT000000509867"
    # Priorité : colonne dédiée avant id_extraction.
    assert reconcile.extract_jorftext({"jorftext": JORF_D1, "id_extraction": JORF_D2}) == JORF_D1
    # Un LEGITEXT ou une URL sans JORFTEXT ne résolvent rien.
    assert reconcile.extract_jorftext({"id_extraction": LEGITEXT, "url_legifrance": "https://www.legifrance.gouv.fr/"}) is None


def test_select_texte_without_jorftext_is_pending_mapping_never_blocking(capsys: Any) -> None:
    records = [
        _rec(3, **_texte("", statut="a_ingerer")),
        _rec(4, **_texte("", statut="a_supprimer")),
        _rec(5, **_texte("", statut="en_attente")),  # limbo -> ignorée silencieusement
    ]

    selection = reconcile.select_legifrance_rows(records)  # ne lève jamais

    assert selection.rows == ()
    assert selection.pending_mapping == (3, 4)
    assert "sans JORFTEXT résoluble" in capsys.readouterr().out


def test_select_active_code_without_legitext_raises() -> None:
    records = [_rec(7, **_legi(type_id="legifrance_code", statut="ingere"))]

    with pytest.raises(reconcile.GristContractError, match="Grist 7"):
        reconcile.select_legifrance_rows(records)


def test_select_limbo_code_without_legitext_is_skipped(capsys: Any) -> None:
    records = [_rec(8, **_legi(type_id="legifrance_code", statut="en_attente"))]

    assert reconcile.select_legifrance_rows(records).rows == ()
    assert "ignorée" in capsys.readouterr().out


def test_select_dedup_juridical_abrogation_beats_active() -> None:
    records = [
        _rec(1, **_texte(JORF_D1, statut="", abroge="oui")),
        _rec(2, **_texte(JORF_D1, statut="ingere")),
    ]

    rows = reconcile.select_legifrance_rows(records).rows

    assert len(rows) == 1
    assert rows[0].abrogated and rows[0].record_id == 1


def test_active_statuts_match_service_public() -> None:
    # Anti-drift : le cycle de vie unifié #289 est le même pour tous les corpus.
    assert reconcile.ACTIVE_STATUTS == sp_reconcile.ACTIVE_STATUTS
    assert reconcile.REMOVAL_STATUTS == sp_reconcile.REMOVAL_STATUTS


# --- build_legifrance_plan ------------------------------------------------------


def _selection(records: list[dict[str, Any]]) -> reconcile.LegifranceSelection:
    return reconcile.select_legifrance_rows(records)


def test_plan_articles_new_changed_unchanged_abrogated_stale() -> None:
    selection = _selection(
        [
            _rec(1, **_code()),
            _rec(2, **_texte(JORF_D1, statut="ingere")),
        ]
    )
    toc = {
        LEGITEXT: _arts(
            ("LEGIARTI001", "VIGUEUR"),  # unchanged
            ("LEGIARTI002", "VIGUEUR"),  # new
            ("LEGIARTI003", "VIGUEUR"),  # changed
            ("LEGIARTI004", "ABROGE"),  # abrogé à la source, présent au corpus
            ("LEGIARTI005", "VIGUEUR", "LEGIARTI005V2"),  # corpus keyé version -> migration
        ),
        JORF_D1: _arts(("LEGIARTI101", "VIGUEUR")),  # unchanged
    }
    silver = {"LEGIARTI001": "h1", "LEGIARTI002": "h2", "LEGIARTI003": "h3-new", "LEGIARTI005": "h5", "LEGIARTI101": "h101"}
    corpus = _corpus(
        LEGIARTI001=("h1", 1),
        LEGIARTI003=("h3-old", 1),
        LEGIARTI004=("h4", 1),
        LEGIARTI005V2=("h5-old", 1),  # ancien alias d'identité -> stale autoritaire
        LEGIARTI101=("h101", 1),
        LEGIARTI999=("h9", 1),  # hors de toutes les TOCs -> flagged, jamais auto
    )

    lf_plan = reconcile.build_legifrance_plan(selection, toc, silver, corpus)
    plan = lf_plan.plan

    assert plan.new == ("LEGIARTI002", "LEGIARTI005")
    assert plan.changed == ("LEGIARTI003",)
    assert set(plan.unchanged) == {"LEGIARTI001", "LEGIARTI101"}
    assert set(plan.auto_removals) == {"LEGIARTI004", "LEGIARTI005V2"}
    assert plan.flagged_removals == ("LEGIARTI999",)
    assert lf_plan.followed_record_ids == {LEGITEXT: 1, JORF_D1: 2}
    assert {"LEGIARTI002", "LEGIARTI005", "LEGIARTI005V2"} <= set(lf_plan.followed_articles[LEGITEXT])
    assert lf_plan.followed_articles[JORF_D1] == frozenset({"LEGIARTI101"})


def test_plan_version_keyed_corpus_doc_migrates_identity_to_cid() -> None:
    # Corpus historique keyé version (bug parseur corrigé) : un doc == version_id
    # d'un article VIGUEUR suivi est un ancien alias -> stale AUTORITAIRE, son
    # cid chronique arrive en `new` (swap version -> chronique).
    selection = _selection([_rec(1, **_code())])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR", "LEGIARTI001V2"))}
    corpus = _corpus(LEGIARTI001V2=("old", 2))

    plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus).plan

    assert plan.new == ("LEGIARTI001",)
    assert [(r.uid, r.reason, r.confidence) for r in plan.removals] == [("LEGIARTI001V2", "stale", Confidence.AUTHORITATIVE)]


def test_plan_unattributable_corpus_article_is_flagged_never_auto() -> None:
    # « Les 244 » : un article du corpus hors de toutes les TOCs suivies (ni
    # cid ni version_id) est inattribuable -> revue opérateur, jamais cascadé.
    selection = _selection([_rec(1, **_code())])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI244=("h244", 1))

    plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus).plan

    assert plan.auto_removals == ()
    assert plan.flagged_removals == ("LEGIARTI244",)


def test_plan_extra_attribution_makes_old_version_authoritative_stale() -> None:
    # Revue #307 ter : une ANCIENNE version hors TOC n'est cascadable que via
    # une ownership VÉRIFIÉE (extra_attributions, résolue par PISTE getArticle
    # dans le job). Owner suivi -> stale autoritaire scopé ; owner inconnu des
    # textes suivis ou absent -> flagged (fail-closed).
    selection = _selection([_rec(1, **_code())])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI_L652_OLD=("h-old", 1), LEGIARTI244=("h244", 1))

    lf_plan = reconcile.build_legifrance_plan(
        selection,
        toc,
        {"LEGIARTI001": "h1"},
        corpus,
        extra_attributions={
            "LEGIARTI_L652_OLD": LEGITEXT,  # ownership vérifiée -> cascade scopée
            "LEGIARTI244": "JORFTEXT000099999999",  # owner NON suivi -> ignoré, flagged
        },
    )

    assert lf_plan.plan.auto_removals == ("LEGIARTI_L652_OLD",)
    assert lf_plan.plan.flagged_removals == ("LEGIARTI244",)
    assert "LEGIARTI_L652_OLD" in lf_plan.followed_articles[LEGITEXT]  # rattachée (deferral/agrégat)


def test_plan_non_legiarti_corpus_docs_protected_as_legacy_text_docs() -> None:
    # Documents texte-level (table moderne) encore en base : protégés + surfacés
    # jusqu'à la décommission, jamais gérés (ni cascadés) par ce delta.
    selection = _selection([_rec(1, **_code())])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), DECRET_86_83_AB=("hd", 4), CGFP_TABLE=("hm", 2))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus)

    assert lf_plan.legacy_text_docs == ("CGFP_TABLE", "DECRET_86_83_AB")
    assert set(lf_plan.legacy_text_docs) <= set(lf_plan.protected)
    assert lf_plan.plan.removals == ()
    assert reconcile.plan_summary(lf_plan)["legacy_text_docs"] == {"count": 2, "sample": ["CGFP_TABLE", "DECRET_86_83_AB"]}


def test_plan_missing_toc_for_active_followed_text_raises() -> None:
    with pytest.raises(reconcile.GristContractError, match="TOC PISTE"):
        reconcile.build_legifrance_plan(_selection([_rec(1, **_code())]), {}, {}, _corpus(LEGIARTI001=("h1", 1)))

    with pytest.raises(reconcile.GristContractError, match=JORF_D1):
        reconcile.build_legifrance_plan(_selection([_rec(2, **_texte(JORF_D1, statut="a_ingerer"))]), {}, {}, {})


def test_plan_abrogated_text_cascades_its_toc_articles() -> None:
    # Un texte retiré cascade SES ARTICLES (attribués par sa TOC) : présents au
    # corpus -> cascade ; absents -> acquittés. Jamais les docs texte-level.
    selection = _selection([_rec(1, **_code(statut="a_supprimer"))])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"), ("LEGIARTI002", "VIGUEUR"), ("LEGIARTI003", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI002=("h2", 1), LEGIARTI888=("h8", 1), D1=("hd", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {}, corpus)

    assert set(lf_plan.plan.auto_removals) == {"LEGIARTI001", "LEGIARTI002"}
    assert lf_plan.plan.acknowledged == ("LEGIARTI003",)
    assert lf_plan.plan.flagged_removals == ("LEGIARTI888",)  # hors TOC, inattribuable
    assert "D1" in lf_plan.legacy_text_docs  # texte-level protégé, jamais cascadé


def test_plan_abrogated_text_without_toc_leaves_articles_flagged() -> None:
    # TOC absente pour un texte abrogé : ses articles corpus restent
    # inattribuables -> flagged, jamais cascadés à l'aveugle.
    selection = _selection([_rec(1, **_code(statut="a_supprimer"))])
    corpus = _corpus(LEGIARTI001=("h1", 1))

    plan = reconcile.build_legifrance_plan(selection, {}, {}, corpus).plan

    assert plan.auto_removals == ()
    assert plan.flagged_removals == ("LEGIARTI001",)


def test_plan_limbo_text_with_toc_protects_corpus_articles() -> None:
    selection = _selection([_rec(1, **_code(statut="en_attente"))])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR", "LEGIARTI001V9"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI001V9=("old", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {}, corpus)

    assert lf_plan.plan.auto_removals == ()
    assert lf_plan.plan.flagged_removals == ()
    assert {"LEGIARTI001", "LEGIARTI001V9"} <= set(lf_plan.protected)


def test_plan_multiple_followed_texts_cohabit_with_toc_attribution() -> None:
    # Le garde multi-code v1 a disparu : plusieurs codes/textes cohabitent,
    # l'attribution (stale compris) passe par les TOCs.
    selection = _selection(
        [
            _rec(1, **_code()),
            _rec(2, **_legi(type_id="legifrance_code", id_extraction=LEGITEXT2, statut="ingere")),
        ]
    )
    toc = {
        LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR")),
        LEGITEXT2: _arts(("LEGIARTI201", "VIGUEUR", "LEGIARTI201V2")),
    }
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI201V2=("old", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1", "LEGIARTI201": "h201"}, corpus)

    assert lf_plan.followed_record_ids == {LEGITEXT: 1, LEGITEXT2: 2}
    assert lf_plan.plan.new == ("LEGIARTI201",)
    assert lf_plan.plan.auto_removals == ("LEGIARTI201V2",)  # attribué au 2e code
    assert lf_plan.followed_articles[LEGITEXT2] == frozenset({"LEGIARTI201", "LEGIARTI201V2"})


def test_plan_protects_mapped_out_of_scope_documents() -> None:
    selection = _selection(
        [
            _rec(1, **_code()),
            _rec(2, **_legi(document_ids="CIRC1", statut="a_ingerer")),
        ]
    )
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), CIRC1=("hc", 2))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus)

    assert "CIRC1" in lf_plan.protected
    assert "CIRC1" not in lf_plan.plan.auto_removals
    assert "CIRC1" not in lf_plan.legacy_text_docs  # déjà mappé hors périmètre, pas un résidu


def test_plan_full_run_article_absent_from_lake_is_pending() -> None:
    selection = _selection([_rec(1, **_code())])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"), ("LEGIARTI002", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus)

    assert lf_plan.plan.unchanged == ("LEGIARTI001",)
    assert lf_plan.pending == ("LEGIARTI002",)
    assert "LEGIARTI002" not in lf_plan.plan.to_ingest


def test_plan_requested_subset_spares_rest_of_corpus() -> None:
    selection = _selection([_rec(1, **_code()), _rec(2, **_texte(JORF_D2, statut="a_supprimer"))])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR")), JORF_D2: _arts(("LEGIARTI201", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI201=("h201", 1), LEGIARTI999=("h9", 1))

    plan = reconcile.build_legifrance_plan(selection, toc, {}, corpus, requested={"LEGIARTI201"}).plan

    assert plan.auto_removals == ("LEGIARTI201",)
    assert "LEGIARTI999" not in [r.uid for r in plan.removals]  # hors sous-ensemble, épargné


def test_plan_empty_selection_never_purges_corpus() -> None:
    # Aucune ligne Legi en Grist mais un corpus non vide : jamais de purge
    # silencieuse — articles flagged, docs texte-level protégés.
    lf_plan = reconcile.build_legifrance_plan(_selection([]), {}, {}, _corpus(LEGIARTI999=("h9", 1), D1=("hd", 1)))

    assert lf_plan.plan.auto_removals == ()
    assert lf_plan.plan.flagged_removals == ("LEGIARTI999",)
    assert lf_plan.legacy_text_docs == ("D1",)


def test_plan_mass_stale_guard_downgrades_bulk_removals(capsys: Any) -> None:
    # Migration : le corpus staging réel contient ~1600 documents keyed version.
    # Un volume de stale > max_auto_stale ne doit JAMAIS être cascadé
    # automatiquement — revue opérateur d'abord. L'abrogation Grist, elle,
    # reste autoritaire quel que soit le volume.
    selection = _selection([_rec(1, **_code()), _rec(2, **_texte(JORF_D2, statut="a_supprimer"))])
    toc = {
        LEGITEXT: [CodeArticle(cid=f"LEGIARTI1{i:03d}", etat="VIGUEUR", version_id=f"LEGIARTI2{i:03d}") for i in range(60)],
        JORF_D2: _arts(("LEGIARTI901", "VIGUEUR")),
    }
    corpus = {f"LEGIARTI2{i:03d}": {"doc_id": f"d{i}", "checksum": "h", "nb_chunks": 1} for i in range(60)}
    corpus["LEGIARTI901"] = {"doc_id": "da", "checksum": "ha", "nb_chunks": 1}

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {}, corpus, max_auto_stale=50)

    assert lf_plan.mass_stale_guard is True
    assert lf_plan.plan.auto_removals == ("LEGIARTI901",)  # l'abrogation opérateur reste appliquée
    assert len(lf_plan.plan.flagged_removals) == 60
    assert "max_auto_stale" in capsys.readouterr().out
    assert reconcile.plan_summary(lf_plan)["mass_stale_guard"] is True

    # Garde désactivé explicitement (migration délibérée).
    lf_plan_off = reconcile.build_legifrance_plan(selection, toc, {}, corpus, max_auto_stale=None)
    assert lf_plan_off.mass_stale_guard is False
    assert len(lf_plan_off.plan.auto_removals) == 61


def test_plan_summary_reports_buckets(capsys: Any) -> None:
    selection = _selection(
        [
            _rec(1, **_code()),
            _rec(3, **_texte("", statut="a_ingerer")),  # pending_mapping
            _rec(4, **_legi(titre_document="hors périmètre", statut="a_ingerer")),  # out_of_scope
        ]
    )
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}

    summary = reconcile.plan_summary(reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, _corpus(D1=("hd", 2))))

    assert summary["new"] == {"count": 1, "sample": ["LEGIARTI001"]}
    assert summary["out_of_scope_rows"] == {"count": 1}
    assert summary["pending_mapping_rows"] == {"count": 1}
    assert summary["legacy_text_docs"] == {"count": 1, "sample": ["D1"]}
    assert summary["to_ingest"]["count"] == 1


# --- ingest_delta (job) ---------------------------------------------------------


class _RecordingGrist:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []
        self.writebacks: list[tuple[int, dict[str, Any]]] = []
        self.update_calls = 0

    def list_records(self, table_id: str | None = None) -> list[dict[str, Any]]:
        return self._records

    def update_records(self, records: list[dict[str, Any]], table_id: str | None = None) -> None:
        self.update_calls += 1
        self.writebacks.extend((int(record["id"]), dict(record["fields"])) for record in records)


class _FakePiste:
    """TOC follow-live factice : articles configurés par uid de texte suivi."""

    def __init__(
        self,
        toc: dict[str, list[CodeArticle]] | None = None,
        *,
        fail: bool = False,
        fail_uids: set[str] | None = None,
    ) -> None:
        self._toc = toc or {}
        self._fail = fail
        self._fail_uids = fail_uids or set()
        self.calls: list[tuple[str, int, str]] = []
        # Ownership getArticle : uid -> texte parent ; uid absent = API en échec.
        self.article_parents: dict[str, str] = {}
        self.article_calls: list[str] = []

    def text_articles(self, text_uid: str, date_millis: int, *, kind: str = "code") -> list[CodeArticle]:
        self.calls.append((text_uid, date_millis, kind))
        if self._fail or text_uid in self._fail_uids:
            raise requests.ConnectionError("piste down")
        return list(self._toc.get(text_uid, []))

    def get_article(self, article_id: str) -> dict[str, Any]:
        self.article_calls.append(article_id)
        parent = self.article_parents.get(article_id)
        if parent is None:
            raise requests.ConnectionError("getArticle down")
        return {"article": {"cid": article_id, "textTitles": [{"cid": parent}]}}


class _DeltaWriter:
    def __init__(self, corpus: dict[str, dict[str, Any]]) -> None:
        self._corpus = corpus
        self.article_bundles: list[str] = []
        self.article_cascades: list[list[str]] = []

    def list_legifrance_corpus(self, source: str = "legifrance") -> dict[str, dict[str, Any]]:
        return self._corpus

    def ingest_article_bundle(self, document: dict[str, Any], sections: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, int]:
        self.article_bundles.append(str(document.get("short_id")).upper())
        return {"documents": 1, "sections": len(sections), "chunks_deleted": 0, "chunks": len(chunks)}

    def delete_articles_cascade(self, cids: list[str], *, source: str = "legifrance") -> dict[str, int]:
        self.article_cascades.append(list(cids))
        return {"chunks": len(cids), "sections": len(cids), "documents": len(cids)}


def _artifacts() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents = [
        {"short_id": "LEGIARTI002", "doc_id": "da2", "checksum": "h2"},
        {"short_id": "LEGIARTI101", "doc_id": "da101", "checksum": "h101-new"},
    ]
    sections = [
        {"doc_id": "da2", "section_index": 0, "section_id": "sa2"},
        {"doc_id": "da101", "section_index": 0, "section_id": "sa101"},
    ]
    chunks = [
        {"cid": "LEGIARTI002", "chunk_id": "LEGIARTI002_0", "_targets": ["legacy"]},
        {"cid": "LEGIARTI101", "chunk_id": "LEGIARTI101_0", "_targets": ["legacy", "modern"]},
    ]
    return documents, sections, chunks


def _delta_records() -> list[dict[str, Any]]:
    return [
        _rec(1, **_code()),
        _rec(2, **_texte(JORF_D1, statut="ingere")),  # texte suivi, article changed
        _rec(3, **_texte(JORF_D2, statut="a_supprimer")),  # texte retiré -> cascade de ses articles
    ]


def _delta_toc(code: list[CodeArticle]) -> dict[str, list[CodeArticle]]:
    return {
        LEGITEXT: code,
        JORF_D1: _arts(("LEGIARTI101", "VIGUEUR")),
        JORF_D2: _arts(("LEGIARTI201", "VIGUEUR")),
    }


def test_ingest_delta_dry_run_computes_plan_without_writes() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(_delta_records())
    piste = _FakePiste(_delta_toc(_arts(("LEGIARTI001", "VIGUEUR"), ("LEGIARTI002", "VIGUEUR"))))
    writer = _DeltaWriter(_corpus(LEGIARTI101=("h101-old", 3), LEGIARTI201=("h201", 2)))

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, dry_run=True, toc_date_millis=1000)

    assert summary["dry_run"] is True
    assert summary["plan"]["new"]["sample"] == ["LEGIARTI002"]
    assert summary["plan"]["changed"]["sample"] == ["LEGIARTI101"]
    assert summary["plan"]["abrogated"]["sample"] == ["LEGIARTI201"]
    assert summary["plan"]["pending_artifact"]["sample"] == ["LEGIARTI001"]  # actif, hors lake
    assert writer.article_bundles == [] and writer.article_cascades == []
    assert grist.writebacks == []
    # Une TOC par texte suivi (lignes triées par uid), avec le bon endpoint.
    assert piste.calls == [(JORF_D1, 1000, "texte"), (JORF_D2, 1000, "texte"), (LEGITEXT, 1000, "code")]


def test_ingest_delta_apply_single_route_and_aggregated_writeback_per_text() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(_delta_records())
    piste = _FakePiste(
        _delta_toc(
            _arts(
                ("LEGIARTI002", "VIGUEUR"),  # new -> ingest bundle article (route unique)
                ("LEGIARTI004", "ABROGE"),  # au corpus -> cascade articles
            )
        )
    )
    writer = _DeltaWriter(_corpus(LEGIARTI004=("h4", 1), LEGIARTI101=("h101-old", 3), LEGIARTI201=("h201", 2), LEGIARTI999=("h9", 1)))

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, toc_date_millis=1000)

    # Route UNIQUE article-level : les articles du code ET des textes passent
    # par ingest_article_bundle / delete_articles_cascade.
    assert writer.article_bundles == ["LEGIARTI002", "LEGIARTI101"]
    assert writer.article_cascades == [["LEGIARTI004", "LEGIARTI201"]]
    assert summary["applied"] == {"ingested": 2, "skipped": 0, "deleted": 2, "identity_migrations": 0, "failed": 0}
    assert summary["plan"]["flagged"]["sample"] == ["LEGIARTI999"]  # inattribuable, jamais cascadé

    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    # Writeback AGRÉGÉ par texte suivi (1 ligne <-> les articles de sa TOC).
    assert by_record[1]["statut"] == "ingere"
    assert by_record[1]["ingere_prod"] is True
    assert by_record[1]["nb_chunks"] == 1  # le chunk de LEGIARTI002 (004 cascadé)
    assert by_record[2]["statut"] == "ingere" and by_record[2]["ingere_prod"] is True
    assert by_record[3]["statut"] == "supprime" and by_record[3]["ingere_prod"] is False
    assert grist.update_calls == 1  # un seul lot


def test_ingest_delta_article_failure_marks_followed_row_erreur() -> None:
    # Article VIGUEUR demandé explicitement mais absent du lake -> échec tracé,
    # agrégé en `erreur` sur la ligne du texte suivi (sans toucher l'agrégat).
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI002", "VIGUEUR"))})
    writer = _DeltaWriter({})

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, [], [], [], requested={"LEGIARTI002"}, toc_date_millis=1000)

    assert summary["status"] == "partial"
    assert "LEGIARTI002" in summary["failed"]
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[1]["statut"] == "erreur"
    assert "LEGIARTI002" in by_record[1]["erreur_ingestion"]
    assert "ingere_prod" not in by_record[1]
    assert "statut_ingestion_reelle" not in by_record[1]
    assert "nb_chunks" not in by_record[1]


def test_ingest_delta_deferral_is_scoped_per_followed_text() -> None:
    # Revue v2 : l'instabilité du texte A (article pending, hors lake) ne doit
    # PAS geler indéfiniment la migration d'identité du texte B, sain.
    grist = _RecordingGrist([_rec(1, **_texte(JORF_D1)), _rec(2, **_texte(JORF_D2))])
    piste = _FakePiste(
        {
            JORF_D1: _arts(("LEGIARTI_A1", "VIGUEUR", "LEGIARTI_A1_OLD")),  # remplaçant hors lake -> pending
            JORF_D2: _arts(("LEGIARTI_B1", "VIGUEUR", "LEGIARTI_B1_OLD")),  # remplaçant dispo
        }
    )
    writer = _DeltaWriter(_corpus(LEGIARTI_A1_OLD=("ha", 1), LEGIARTI_B1_OLD=("hb", 1)))
    documents = [{"short_id": "LEGIARTI_B1", "doc_id": "db1", "checksum": "hb1"}]
    sections = [{"doc_id": "db1", "section_index": 0, "section_id": "sb1"}]
    chunks = [{"cid": "LEGIARTI_B1", "chunk_id": "LEGIARTI_B1_0", "_targets": ["legacy"]}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, toc_date_millis=1000)

    assert writer.article_bundles == ["LEGIARTI_B1"]
    assert writer.article_cascades == [["LEGIARTI_B1_OLD"]]  # le texte sain migre
    assert summary["deferred_removals"] == ["LEGIARTI_A1_OLD"]  # le texte instable attend
    assert summary["plan"]["pending_artifact"]["sample"] == ["LEGIARTI_A1"]


def test_ingest_delta_targeted_alias_swaps_atomically() -> None:
    # --uid sur un ancien alias de version doit embarquer son cid chronique :
    # jamais de cascade sans ingestion du remplaçant dans le même run.
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI_NEW", "VIGUEUR", "LEGIARTI_OLD"))})
    writer = _DeltaWriter(_corpus(LEGIARTI_OLD=("h-old", 1)))
    documents = [{"short_id": "LEGIARTI_NEW", "doc_id": "dn", "checksum": "hn"}]
    sections = [{"doc_id": "dn", "section_index": 0, "section_id": "sn"}]
    chunks = [{"cid": "LEGIARTI_NEW", "chunk_id": "LEGIARTI_NEW_0", "_targets": ["legacy"]}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, requested={"LEGIARTI_OLD"}, toc_date_millis=1000)

    assert writer.article_bundles == ["LEGIARTI_NEW"]  # le cid jumeau est embarqué
    assert writer.article_cascades == [["LEGIARTI_OLD"]]
    assert summary["applied"] == {"ingested": 1, "skipped": 0, "deleted": 1, "identity_migrations": 0, "failed": 0}


def test_ingest_delta_identity_migration_deletes_checksum_twin_before_ingest() -> None:
    # Migration d'identité version->chronique (fix swap #307) : l'article
    # recodifié arrive sous son cid CHRONIQUE (new) avec un contenu identique à
    # son ancienne version encore en base (stale, même checksum).
    # uq_rag_documents_source_checksum bloquerait l'INSERT tant que le jumeau
    # version occupe (source, checksum) -> on cascade le jumeau AVANT d'ingérer
    # la chronique (out-avant-in), dans le même run.
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI_NEW", "VIGUEUR", "LEGIARTI_OLD"))})
    # Le jumeau OLD est en base avec le MÊME checksum que le bundle chronique.
    writer = _DeltaWriter(_corpus(LEGIARTI_OLD=("same-hash", 1)))
    documents = [{"short_id": "LEGIARTI_NEW", "doc_id": "dn", "checksum": "same-hash"}]
    sections = [{"doc_id": "dn", "section_index": 0, "section_id": "sn"}]
    chunks = [{"cid": "LEGIARTI_NEW", "chunk_id": "LEGIARTI_NEW_0", "_targets": ["legacy"]}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, toc_date_millis=1000)

    # Le jumeau version est cascadé (une seule fois) AVANT l'ingest de la chronique.
    assert writer.article_cascades == [["LEGIARTI_OLD"]]
    assert writer.article_bundles == ["LEGIARTI_NEW"]
    assert summary["identity_migrations"] == ["LEGIARTI_OLD"]
    assert summary["applied"] == {"ingested": 1, "skipped": 0, "deleted": 1, "identity_migrations": 1, "failed": 0}
    # Pas de double suppression : le jumeau migré n'est pas re-cascadé au sweep.
    assert summary["deleted"] == []


def test_ingest_delta_distinct_checksum_is_not_treated_as_identity_migration() -> None:
    # Garde-fou : un stale au checksum DIFFÉRENT du bundle n'est pas un jumeau
    # d'identité -> pas de suppression anticipée, comportement inchangé.
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI_NEW", "VIGUEUR", "LEGIARTI_OLD"))})
    writer = _DeltaWriter(_corpus(LEGIARTI_OLD=("other-hash", 1)))
    documents = [{"short_id": "LEGIARTI_NEW", "doc_id": "dn", "checksum": "new-hash"}]
    sections = [{"doc_id": "dn", "section_index": 0, "section_id": "sn"}]
    chunks = [{"cid": "LEGIARTI_NEW", "chunk_id": "LEGIARTI_NEW_0", "_targets": ["legacy"]}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, toc_date_millis=1000)

    assert summary["applied"]["identity_migrations"] == 0
    assert summary["identity_migrations"] == []


def test_ingest_delta_resolves_out_of_toc_ownership_via_getarticle() -> None:
    # Revue #307 ter : les articles hors TOC sont résolus via getArticle.
    # Parent suivi -> stale attribué cascadé ; API en échec -> flagged (fail-closed).
    grist = _RecordingGrist([_rec(1, **_texte(JORF_D1))])
    piste = _FakePiste({JORF_D1: _arts(("LEGIARTI_A1", "VIGUEUR"))})
    piste.article_parents["LEGIARTI_OLD_OWNED"] = JORF_D1  # ancienne version du texte suivi
    # LEGIARTI_UNKNOWN absent de article_parents -> getArticle échoue -> flagged
    writer = _DeltaWriter(_corpus(LEGIARTI_A1=("h1", 1), LEGIARTI_OLD_OWNED=("h-old", 1), LEGIARTI_UNKNOWN=("h-x", 1)))
    documents = [{"short_id": "LEGIARTI_A1", "doc_id": "da1", "checksum": "h1"}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, [], [], toc_date_millis=1000)

    assert sorted(piste.article_calls) == ["LEGIARTI_OLD_OWNED", "LEGIARTI_UNKNOWN"]
    assert writer.article_cascades == [["LEGIARTI_OLD_OWNED"]]  # ownership vérifiée -> cascadé
    assert summary["plan"]["stale"]["sample"] == ["LEGIARTI_OLD_OWNED"]
    assert summary["plan"]["flagged"]["sample"] == ["LEGIARTI_UNKNOWN"]  # fail-closed


def test_ingest_delta_getarticle_parent_not_followed_stays_flagged() -> None:
    # Reproduction de la revue : un article d'un AUTRE texte (même s'il partage
    # un titre) ne doit JAMAIS être cascadé — son parent résolu n'est pas suivi.
    grist = _RecordingGrist([_rec(1, **_texte(JORF_D1))])
    piste = _FakePiste({JORF_D1: _arts(("LEGIARTI_FOLLOWED", "VIGUEUR"))})
    piste.article_parents["LEGIARTI_UNRELATED"] = "JORFTEXT000099999999"  # texte NON suivi
    writer = _DeltaWriter(_corpus(LEGIARTI_FOLLOWED=("h1", 1), LEGIARTI_UNRELATED=("h2", 1)))
    documents = [{"short_id": "LEGIARTI_FOLLOWED", "doc_id": "df", "checksum": "h1"}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, [], [], toc_date_millis=1000)

    assert writer.article_cascades == []  # jamais de suppression autoritaire
    assert summary["plan"]["flagged"]["sample"] == ["LEGIARTI_UNRELATED"]


def test_ingest_delta_incomplete_bundle_defers_stale_cascade() -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    # LEGIARTI_OLD = ancien alias version du nouvel article suivi -> stale
    # autoritaire attribuable, mais le swap doit rester in-avant-out.
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI_NEW", "VIGUEUR", "LEGIARTI_OLD"))})
    writer = _DeltaWriter(_corpus(LEGIARTI_OLD=("old", 4)))
    documents = [{"short_id": "LEGIARTI_NEW", "doc_id": "new-doc", "checksum": "new"}]
    sections = [{"doc_id": "new-doc", "section_index": 0, "section_id": "new-section"}]

    summary = legifrance_ingestion.ingest_delta(
        writer,
        grist,
        piste,
        documents,
        sections,
        [],
        toc_date_millis=1000,
        max_auto_stale=0,
    )

    assert "chunks" in summary["failed"]["LEGIARTI_NEW"]
    assert summary["deleted"] == []
    assert summary["deferred_removals"] == ["LEGIARTI_OLD"]
    assert writer.article_bundles == []
    assert writer.article_cascades == []
    code_fields = dict(grist.writebacks)[1]
    assert code_fields["statut"] == "erreur"
    assert code_fields["ingere_prod"] is True
    assert code_fields["nb_chunks"] == 4


def test_ingest_delta_rejects_article_chunks_for_wrong_target() -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI_NEW", "VIGUEUR", "LEGIARTI_OLD"))})
    writer = _DeltaWriter(_corpus(LEGIARTI_OLD=("old", 2)))
    documents = [{"short_id": "LEGIARTI_NEW", "doc_id": "new-doc", "checksum": "new"}]
    sections = [{"doc_id": "new-doc", "section_index": 0, "section_id": "new-section"}]
    chunks = [{"cid": "LEGIARTI_NEW", "hash_id": "modern-only", "_targets": ["modern"]}]

    summary = legifrance_ingestion.ingest_delta(
        writer,
        grist,
        piste,
        documents,
        sections,
        chunks,
        toc_date_millis=1000,
        max_auto_stale=0,
    )

    assert "chunks legacy" in summary["failed"]["LEGIARTI_NEW"]
    assert summary["deferred_removals"] == ["LEGIARTI_OLD"]
    assert writer.article_bundles == []
    assert writer.article_cascades == []


def test_ingest_delta_failure_keeps_stale_but_applies_explicit_abrogation(monkeypatch: pytest.MonkeyPatch) -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI_NEW", "VIGUEUR", "LEGIARTI_OLD"), ("LEGIARTI_ABROGE", "ABROGE"))})
    writer = _DeltaWriter(_corpus(LEGIARTI_OLD=("old", 1), LEGIARTI_ABROGE=("ha", 1)))
    documents = [{"short_id": "LEGIARTI_NEW", "doc_id": "new-doc", "checksum": "new"}]
    sections = [{"doc_id": "new-doc", "section_index": 0, "section_id": "new-section"}]
    chunks = [{"cid": "LEGIARTI_NEW", "chunk_id": "new-chunk", "_targets": ["legacy"]}]
    monkeypatch.setattr(writer, "ingest_article_bundle", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db failure")))

    summary = legifrance_ingestion.ingest_delta(
        writer,
        grist,
        piste,
        documents,
        sections,
        chunks,
        toc_date_millis=1000,
        max_auto_stale=0,
    )

    assert summary["deleted"] == ["LEGIARTI_ABROGE"]
    assert summary["deferred_removals"] == ["LEGIARTI_OLD"]
    assert writer.article_cascades == [["LEGIARTI_ABROGE"]]


def test_ingest_delta_targeted_run_does_not_overwrite_aggregates() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist([_rec(1, **_code()), _rec(2, **_texte(JORF_D1, statut="ingere"))])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR")), JORF_D1: _arts(("LEGIARTI101", "VIGUEUR"))})
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 7), LEGIARTI101=("old", 2)))

    summary = legifrance_ingestion.ingest_delta(
        writer,
        grist,
        piste,
        documents,
        sections,
        chunks,
        requested={"LEGIARTI101"},
        toc_date_millis=1000,
    )

    assert writer.article_bundles == ["LEGIARTI101"]
    assert summary["applied"]["failed"] == 0
    # Un plan --uid ne voit qu'un sous-ensemble de chaque texte : jamais de
    # réécriture des agrégats (statut/nb_chunks) — seule une erreur canonique
    # serait remontée.
    assert grist.writebacks == []


def test_ingest_delta_limbo_text_never_fetched_never_cascaded_no_writeback() -> None:
    grist = _RecordingGrist([_rec(1, **_code(statut="en_attente"))])
    piste = _FakePiste({})
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 3)))

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, [], [], [], toc_date_millis=1000)

    assert piste.calls == []  # pas de TOC pour un texte en limbo
    # Sans TOC ses articles sont inattribuables : flagged (revue), jamais auto.
    assert summary["plan"]["flagged"]["sample"] == ["LEGIARTI001"]
    assert summary["applied"]["deleted"] == 0
    assert grist.writebacks == []
    assert writer.article_cascades == []


def test_ingest_delta_abrogated_text_toc_failure_warns_and_continues(capsys: Any) -> None:
    # La TOC d'un texte abrogé ne répond plus : warn, articles flagged, le run
    # continue (seul un texte ACTIF en échec est bloquant).
    grist = _RecordingGrist([_rec(1, **_code()), _rec(2, **_texte(JORF_D2, statut="a_supprimer"))])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}, fail_uids={JORF_D2})
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 1), LEGIARTI201=("h201", 2)))
    documents = [{"short_id": "LEGIARTI001", "doc_id": "da1", "checksum": "h1"}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, [], [], toc_date_millis=1000)

    assert "texte abrogé" in capsys.readouterr().out
    assert summary["status"] == "ok"
    assert summary["deleted"] == []
    assert summary["plan"]["flagged"]["sample"] == ["LEGIARTI201"]
    assert writer.article_cascades == []
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[1]["statut"] == "ingere"
    assert by_record[2]["statut"] == "supprime"  # intention acquittée, corpus intact


def test_ingest_delta_abrogated_already_supprime_and_untouched_skips_writeback() -> None:
    # Idempotence : acquittement déjà fait, rien n'a bougé, rien présent -> pas
    # de writeback (ne pas bumper derniere_ingestion chaque nuit).
    grist = _RecordingGrist([_rec(2, **_texte(JORF_D2, statut="supprime"))])
    piste = _FakePiste({JORF_D2: _arts(("LEGIARTI201", "VIGUEUR"))})
    writer = _DeltaWriter({})

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, [], [], [], toc_date_millis=1000)

    assert summary["plan"]["acknowledged"]["sample"] == ["LEGIARTI201"]
    assert grist.writebacks == []
    assert writer.article_cascades == []


def test_ingest_delta_never_cascades_legacy_text_docs() -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))})
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 1), D1=("hd", 4), CGFP_TABLE=("hm", 2)))
    documents = [{"short_id": "LEGIARTI001", "doc_id": "da1", "checksum": "h1"}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, [], [], toc_date_millis=1000)

    assert summary["plan"]["legacy_text_docs"] == {"count": 2, "sample": ["CGFP_TABLE", "D1"]}
    assert summary["deleted"] == []
    assert writer.article_cascades == []


def test_ingest_delta_staging_only_touches_toggles() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(_delta_records())
    piste = _FakePiste(_delta_toc(_arts(("LEGIARTI002", "VIGUEUR"))))
    writer = _DeltaWriter(_corpus(LEGIARTI101=("h101-old", 3), LEGIARTI201=("h201", 2)))

    legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, target_env="staging", toc_date_millis=1000)

    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[1] == {"ingere_staging": True}  # code agrégé
    assert by_record[2] == {"ingere_staging": True}  # texte dont l'article est ingéré
    assert by_record[3] == {"ingere_staging": False}  # texte dont l'article est cascadé
    assert all("statut" not in fields for fields in by_record.values())


def test_ingest_delta_mass_stale_guard_blocks_cascade() -> None:
    # Bout-en-bout job : au-delà de --max-auto-stale, aucune cascade des stale
    # (ici des alias version attribuables, le cas migration réel).
    grist = _RecordingGrist([_rec(1, **_code())])
    toc = [CodeArticle(cid=f"LEGIARTI1{i:03d}", etat="VIGUEUR", version_id=f"LEGIARTI2{i:03d}") for i in range(10)]
    piste = _FakePiste({LEGITEXT: toc})
    corpus = {f"LEGIARTI2{i:03d}": {"doc_id": f"d{i}", "checksum": "h", "nb_chunks": 1} for i in range(10)}
    writer = _DeltaWriter(corpus)

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, [], [], [], toc_date_millis=1000, max_auto_stale=5)

    assert summary["plan"]["mass_stale_guard"] is True
    assert summary["plan"]["flagged"]["count"] == 10
    assert summary["applied"]["deleted"] == 0
    assert writer.article_cascades == []


def test_ingest_delta_piste_http_error_on_active_text_raises_piste_error() -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste(fail=True)
    writer = _DeltaWriter({})

    with pytest.raises(PisteError, match="TOC PISTE"):
        legifrance_ingestion.ingest_delta(writer, grist, piste, [], [], [], toc_date_millis=1000)


# --- main() end-to-end ----------------------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _seed_article(lake_root: Path, cid: str, checksum: str) -> None:
    _write_json(
        lake_root / "silver" / "documents" / f"{cid}.document.json",
        {"doc_id": f"doc-{cid}", "short_id": cid, "checksum": checksum},
    )
    _write_jsonl(
        lake_root / "silver" / "sections" / f"{cid}.sections.jsonl",
        [{"section_id": f"section-{cid}", "doc_id": f"doc-{cid}", "section_index": 0}],
    )
    _write_jsonl(
        lake_root / "gold" / "chunks" / f"{cid}.chunks.jsonl",
        [{"chunk_id": f"{cid}_0", "cid": cid, "_targets": ["legacy"]}],
    )


def test_main_delta_apply_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import assistant_rh_data_engineering.legifrance.db as legi_db
    import assistant_rh_data_engineering.legifrance.piste as piste_module
    import assistant_rh_data_engineering.utils.grist as grist_module

    lake_root = tmp_path / "lake"
    _seed_article(lake_root, "LEGIARTI002", "h2")
    config_path = tmp_path / "legifrance_articles.json"
    _write_json(config_path, {"article_numbers": []})

    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste({LEGITEXT: _arts(("LEGIARTI002", "VIGUEUR"))})
    writer = _DeltaWriter({})
    monkeypatch.setattr(grist_module, "GristClient", lambda *a, **k: grist)
    monkeypatch.setattr(piste_module, "PisteClient", lambda *a, **k: piste)
    monkeypatch.setattr(legi_db, "LegifranceDbWriter", lambda *a, **k: writer)
    monkeypatch.setattr(
        sys,
        "argv",
        ["legi-ingest", "--lake-root", str(lake_root), "--article-config", str(config_path), "--dsn", "postgresql://unused", "--delta"],
    )

    assert legifrance_ingestion.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == {"ingested": 1, "skipped": 0, "deleted": 0, "identity_migrations": 0, "failed": 0}
    assert writer.article_bundles == ["LEGIARTI002"]
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[1]["statut"] == "ingere"


def test_main_delta_piste_failure_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import assistant_rh_data_engineering.legifrance.db as legi_db
    import assistant_rh_data_engineering.legifrance.piste as piste_module
    import assistant_rh_data_engineering.utils.grist as grist_module

    lake_root = tmp_path / "lake"
    _seed_article(lake_root, "LEGIARTI002", "h2")
    config_path = tmp_path / "legifrance_articles.json"
    _write_json(config_path, {"article_numbers": []})

    monkeypatch.setattr(grist_module, "GristClient", lambda *a, **k: _RecordingGrist([_rec(1, **_code())]))
    monkeypatch.setattr(piste_module, "PisteClient", lambda *a, **k: _FakePiste(fail=True))
    monkeypatch.setattr(legi_db, "LegifranceDbWriter", lambda *a, **k: _DeltaWriter({}))
    monkeypatch.setattr(
        sys,
        "argv",
        ["legi-ingest", "--lake-root", str(lake_root), "--article-config", str(config_path), "--dsn", "postgresql://unused", "--delta"],
    )

    with pytest.raises(SystemExit, match="Échec Grist/PISTE en mode --delta"):
        legifrance_ingestion.main()


def test_main_delta_rejects_skip_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["legi-ingest", "--delta", "--skip-chunks"])

    with pytest.raises(SystemExit, match="skip"):
        legifrance_ingestion.main()
