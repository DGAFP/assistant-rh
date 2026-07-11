"""Tests de la réconciliation delta Légifrance (E2.3-b, #289).

Trois niveaux :
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
from assistant_rh_data_engineering.service_public import reconcile as sp_reconcile

LEGITEXT = "LEGITEXT000044416551"


def _rec(record_id: int, **fields: Any) -> dict[str, Any]:
    return {"id": record_id, "fields": fields}


def _legi(**fields: Any) -> dict[str, Any]:
    base = {"source_corpus": "Interministériel/Légifrance"}
    base.update(fields)
    return base


def _code(statut: str = "ingere", **fields: Any) -> dict[str, Any]:
    return _legi(type_id="legifrance_code", id_extraction=LEGITEXT, titre_document="CGFP", statut=statut, **fields)


def _texte(document_ids: str, statut: str = "ingere", **fields: Any) -> dict[str, Any]:
    return _legi(type_id="legifrance_texte", document_ids=document_ids, statut=statut, **fields)


def _corpus(**entries: tuple[str, int]) -> dict[str, dict[str, Any]]:
    return {uid: {"doc_id": f"d-{uid}", "checksum": checksum, "nb_chunks": nb} for uid, (checksum, nb) in entries.items()}


def _arts(*specs: tuple[str, str]) -> list[CodeArticle]:
    return [CodeArticle(cid=cid, etat=etat) for cid, etat in specs]


# --- select_legifrance_rows -----------------------------------------------------


def test_select_classifies_code_texte_out_of_scope_and_pending_mapping(capsys: Any) -> None:
    records = [
        _rec(1, **_code()),
        _rec(2, **_texte("DECRET_86_83_AB", statut="a_ingerer")),
        _rec(3, **_texte("", statut="a_ingerer")),  # matcher pas passé -> pending_mapping
        _rec(4, **_legi(titre_document="Circulaire hors périmètre", document_ids="CIRC1", statut="a_ingerer")),  # sans type_id
        _rec(5, **_texte("VIEUX_DECRET", statut="a_supprimer")),
        _rec(6, source_corpus="service-public", id_extraction="F1", statut="ingere"),  # autre corpus
    ]

    selection = reconcile.select_legifrance_rows(records)

    assert [(r.kind, r.uid) for r in selection.rows] == [
        ("texte", "DECRET_86_83_AB"),
        ("code", LEGITEXT),
        ("texte", "VIEUX_DECRET"),
    ]
    code = selection.code_rows[0]
    assert code.active and code.record_id == 1
    assert selection.texte_rows[1].abrogated
    assert selection.out_of_scope == (4,)
    assert selection.out_of_scope_uids == ("CIRC1",)
    assert selection.pending_mapping == (3,)
    assert "matcher pas passé" in capsys.readouterr().out


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
        _rec(1, **_texte("D1", statut="", abroge="oui")),
        _rec(2, **_texte("D1", statut="ingere")),
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
            _rec(2, **_texte("D1", statut="ingere")),  # unchanged
        ]
    )
    toc = {
        LEGITEXT: _arts(
            ("LEGIARTI001", "VIGUEUR"),  # unchanged
            ("LEGIARTI002", "VIGUEUR"),  # new
            ("LEGIARTI003", "VIGUEUR"),  # changed
            ("LEGIARTI004", "ABROGE"),  # abrogé à la source, présent au corpus
        )
    }
    silver = {"LEGIARTI001": "h1", "LEGIARTI002": "h2", "LEGIARTI003": "h3-new", "D1": "hd1"}
    corpus = _corpus(
        LEGIARTI001=("h1", 1),
        LEGIARTI003=("h3-old", 1),
        LEGIARTI004=("h4", 1),
        LEGIARTI999=("h9", 1),  # disparu de la TOC (recodification) -> stale
        D1=("hd1", 4),
    )

    lf_plan = reconcile.build_legifrance_plan(selection, toc, silver, corpus)
    plan = lf_plan.plan

    assert plan.new == ("LEGIARTI002",)
    assert plan.changed == ("LEGIARTI003",)
    assert set(plan.unchanged) == {"LEGIARTI001", "D1"}
    assert set(plan.auto_removals) == {"LEGIARTI004", "LEGIARTI999"}
    assert plan.flagged_removals == ()
    assert lf_plan.code_record_ids == {LEGITEXT: 1}
    assert lf_plan.texte_record_ids == {"D1": 2}
    assert "LEGIARTI002" in lf_plan.code_articles[LEGITEXT]
    assert "LEGIARTI999" in lf_plan.code_articles[LEGITEXT]  # corpus rattaché au code


def test_plan_missing_toc_for_active_code_raises() -> None:
    selection = _selection([_rec(1, **_code())])

    with pytest.raises(reconcile.GristContractError, match="TOC PISTE"):
        reconcile.build_legifrance_plan(selection, {}, {}, _corpus(LEGIARTI001=("h1", 1)))


def test_plan_abrogated_code_cascades_all_corpus_articles() -> None:
    selection = _selection([_rec(1, **_code(statut="a_supprimer"))])
    corpus = _corpus(LEGIARTI001=("h1", 1), LEGIARTI002=("h2", 1), D1=("hd", 1))

    plan = reconcile.build_legifrance_plan(selection, {}, {}, corpus).plan

    assert set(plan.auto_removals) >= {"LEGIARTI001", "LEGIARTI002"}
    # D1 (texte sans ligne Grist) est stale, indépendamment du code.
    assert "D1" in plan.auto_removals


def test_plan_limbo_code_protects_corpus_articles() -> None:
    selection = _selection([_rec(1, **_code(statut="en_attente"))])
    corpus = _corpus(LEGIARTI001=("h1", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, {}, {}, corpus)

    assert lf_plan.plan.auto_removals == ()
    assert "LEGIARTI001" in lf_plan.protected


def test_plan_no_code_row_protects_corpus_articles_from_purge() -> None:
    # Aucune ligne code en Grist mais des articles au corpus : jamais de purge
    # silencieuse de ~2500 articles.
    selection = _selection([_rec(2, **_texte("D1", statut="ingere"))])
    corpus = _corpus(LEGIARTI001=("h1", 1), D1=("hd", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, {}, {"D1": "hd"}, corpus)

    assert "LEGIARTI001" in lf_plan.protected
    assert lf_plan.plan.auto_removals == ()


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


def test_plan_rejects_multiple_code_rows_without_article_ownership() -> None:
    selection = _selection(
        [
            _rec(1, **_code()),
            _rec(
                2,
                **_legi(
                    type_id="legifrance_code",
                    id_extraction="LEGITEXT000000000002",
                    statut="ingere",
                ),
            ),
        ]
    )

    with pytest.raises(reconcile.GristContractError, match="plusieurs codes"):
        reconcile.build_legifrance_plan(selection, {}, {}, {})


def test_plan_full_run_article_absent_from_lake_is_pending() -> None:
    selection = _selection([_rec(1, **_code())])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"), ("LEGIARTI002", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1))

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus)

    assert lf_plan.plan.unchanged == ("LEGIARTI001",)
    assert lf_plan.pending == ("LEGIARTI002",)
    assert "LEGIARTI002" not in lf_plan.plan.to_ingest


def test_plan_requested_subset_spares_rest_of_corpus() -> None:
    selection = _selection([_rec(1, **_code()), _rec(2, **_texte("D1", statut="a_supprimer"))])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), D1=("hd", 1), LEGIARTI999=("h9", 1))

    plan = reconcile.build_legifrance_plan(selection, toc, {}, corpus, requested={"D1"}).plan

    assert plan.auto_removals == ("D1",)
    assert "LEGIARTI999" not in plan.auto_removals  # hors sous-ensemble, épargné


def test_plan_empty_selection_guard_downgrades_stale_to_flagged() -> None:
    plan = reconcile.build_legifrance_plan(_selection([]), {}, {}, _corpus(D1=("hd", 1))).plan

    assert plan.auto_removals == ()
    assert plan.flagged_removals == ("D1",)


def test_plan_mass_stale_guard_downgrades_bulk_removals(capsys: Any) -> None:
    # Migration : le corpus staging réel contient ~1600 documents hérités hors
    # Grist. Un volume de stale > max_auto_stale ne doit JAMAIS être cascadé
    # automatiquement — revue opérateur d'abord. L'abrogation Grist, elle,
    # reste autoritaire quel que soit le volume.
    selection = _selection([_rec(1, **_code()), _rec(2, **_texte("D_ABROGE", statut="a_supprimer"))])
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}
    corpus = _corpus(LEGIARTI001=("h1", 1), D_ABROGE=("ha", 1))
    corpus.update({f"VIEUX_DOC_{i:04d}": {"doc_id": f"d{i}", "checksum": "h", "nb_chunks": 1} for i in range(60)})

    lf_plan = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus, max_auto_stale=50)

    assert lf_plan.mass_stale_guard is True
    assert lf_plan.plan.auto_removals == ("D_ABROGE",)  # l'abrogation opérateur reste appliquée
    assert len(lf_plan.plan.flagged_removals) == 60
    assert "max_auto_stale" in capsys.readouterr().out
    assert reconcile.plan_summary(lf_plan)["mass_stale_guard"] is True

    # Garde désactivé explicitement (nettoyage de migration délibéré).
    lf_plan_off = reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, corpus, max_auto_stale=None)
    assert lf_plan_off.mass_stale_guard is False
    assert len(lf_plan_off.plan.auto_removals) == 61


def test_plan_summary_reports_buckets() -> None:
    selection = _selection(
        [
            _rec(1, **_code()),
            _rec(4, **_legi(titre_document="hors périmètre", statut="a_ingerer")),
        ]
    )
    toc = {LEGITEXT: _arts(("LEGIARTI001", "VIGUEUR"))}

    summary = reconcile.plan_summary(reconcile.build_legifrance_plan(selection, toc, {"LEGIARTI001": "h1"}, {}))

    assert summary["new"] == {"count": 1, "sample": ["LEGIARTI001"]}
    assert summary["out_of_scope_rows"] == {"count": 1}
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
    def __init__(self, articles: list[CodeArticle] | None = None, *, fail: bool = False) -> None:
        self._articles = articles or []
        self._fail = fail
        self.calls: list[tuple[str, int]] = []

    def table_matieres(self, legitext: str, date_millis: int, *, nature: str = "CODE") -> dict[str, Any]:
        self.calls.append((legitext, date_millis))
        if self._fail:
            raise requests.ConnectionError("piste down")
        return {"sections": [{"articles": [{"cid": a.cid, "etat": a.etat, "num": a.num} for a in self._articles]}]}


class _DeltaWriter:
    def __init__(self, corpus: dict[str, dict[str, Any]]) -> None:
        self._corpus = corpus
        self.article_bundles: list[str] = []
        self.texte_bundles: list[str] = []
        self.article_cascades: list[list[str]] = []
        self.texte_cascades: list[list[str]] = []

    def list_legifrance_corpus(self, source: str = "legifrance") -> dict[str, dict[str, Any]]:
        return self._corpus

    def ingest_article_bundle(self, document: dict[str, Any], sections: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, int]:
        self.article_bundles.append(str(document.get("short_id")).upper())
        return {"documents": 1, "sections": len(sections), "chunks_deleted": 0, "chunks": len(chunks)}

    def ingest_texte_bundle(self, document: dict[str, Any], sections: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, int]:
        self.texte_bundles.append(str(document.get("short_id")).upper())
        return {"documents": 1, "sections": len(sections), "chunks_deleted": 0, "chunks": len(chunks)}

    def delete_articles_cascade(self, cids: list[str], *, source: str = "legifrance") -> dict[str, int]:
        self.article_cascades.append(list(cids))
        return {"chunks": len(cids), "sections": len(cids), "documents": len(cids)}

    def delete_textes_cascade(self, short_ids: list[str], *, source: str = "legifrance") -> dict[str, int]:
        self.texte_cascades.append(list(short_ids))
        return {"chunks": 2, "sections": 1, "documents": len(short_ids)}


def _artifacts() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents = [
        {"short_id": "LEGIARTI002", "doc_id": "da2", "checksum": "h2"},
        {"short_id": "D1", "doc_id": "dt1", "checksum": "hd1-new"},
    ]
    sections = [
        {"doc_id": "da2", "section_index": 0, "section_id": "sa2"},
        {"doc_id": "dt1", "section_index": 0, "section_id": "st1"},
    ]
    chunks = [
        {"cid": "LEGIARTI002", "chunk_id": "LEGIARTI002_0", "_targets": ["legacy"]},
        {"short_id": "D1", "hash_id": "cd1", "source_document_id": "dt1", "_targets": ["modern"]},
    ]
    return documents, sections, chunks


def _delta_records() -> list[dict[str, Any]]:
    return [
        _rec(1, **_code()),
        _rec(2, **_texte("D1", statut="ingere")),  # changed
        _rec(3, **_texte("D2", statut="a_supprimer")),  # au corpus -> cascade
    ]


def test_ingest_delta_dry_run_computes_plan_without_writes() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(_delta_records())
    piste = _FakePiste(_arts(("LEGIARTI001", "VIGUEUR"), ("LEGIARTI002", "VIGUEUR")))
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 1), D1=("hd1-old", 3), D2=("hd2", 2)))

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, dry_run=True, toc_date_millis=1000)

    assert summary["dry_run"] is True
    assert summary["plan"]["new"]["sample"] == ["LEGIARTI002"]
    assert summary["plan"]["changed"]["sample"] == ["D1"]
    assert summary["plan"]["abrogated"]["sample"] == ["D2"]
    assert summary["plan"]["pending_artifact"]["sample"] == ["LEGIARTI001"]  # actif, hors lake
    assert writer.article_bundles == [] and writer.texte_bundles == []
    assert writer.article_cascades == [] and writer.texte_cascades == []
    assert grist.writebacks == []
    assert piste.calls == [(LEGITEXT, 1000)]


def test_ingest_delta_apply_routes_tables_and_aggregates_code_writeback() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(_delta_records())
    piste = _FakePiste(
        _arts(
            ("LEGIARTI002", "VIGUEUR"),  # new -> ingest table legacy
            ("LEGIARTI004", "ABROGE"),  # au corpus -> cascade articles
        )
    )
    writer = _DeltaWriter(_corpus(LEGIARTI004=("h4", 1), LEGIARTI999=("h9", 1), D1=("hd1-old", 3), D2=("hd2", 2)))

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, toc_date_millis=1000)

    # Routage : article -> bundle legacy, texte -> bundle moderne.
    assert writer.article_bundles == ["LEGIARTI002"]
    assert writer.texte_bundles == ["D1"]
    # Cascades routées par table : articles (abrogé + stale hors TOC) vs textes.
    assert writer.article_cascades == [["LEGIARTI004", "LEGIARTI999"]]
    assert writer.texte_cascades == [["D2"]]
    assert summary["applied"] == {"ingested": 2, "skipped": 0, "deleted": 3, "failed": 0}

    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    # Textes : writeback per-ligne.
    assert by_record[2]["statut"] == "ingere" and by_record[2]["ingere_prod"] is True
    assert by_record[3]["statut"] == "supprime" and by_record[3]["ingere_prod"] is False
    # Code suivi : writeback agrégé sur SA ligne (1 ligne <-> N articles).
    assert by_record[1]["statut"] == "ingere"
    assert by_record[1]["ingere_prod"] is True
    assert by_record[1]["nb_chunks"] == 1  # le chunk de LEGIARTI002
    assert grist.update_calls == 1  # un seul lot


def test_ingest_delta_article_failure_marks_code_row_erreur() -> None:
    # Article VIGUEUR demandé explicitement mais absent du lake -> échec tracé,
    # agrégé en `erreur` sur la ligne code.
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste(_arts(("LEGIARTI002", "VIGUEUR")))
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


def test_ingest_delta_incomplete_bundle_defers_stale_cascade() -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste(_arts(("LEGIARTI_NEW", "VIGUEUR")))
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
    piste = _FakePiste(_arts(("LEGIARTI_NEW", "VIGUEUR")))
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


def test_ingest_delta_rejects_text_chunks_for_wrong_target() -> None:
    grist = _RecordingGrist([_rec(1, **_texte("D1", statut="ingere"))])
    piste = _FakePiste([])
    writer = _DeltaWriter(_corpus(D1=("old", 2), D_OLD=("stale", 1)))
    documents = [{"short_id": "D1", "doc_id": "doc-d1", "checksum": "new"}]
    sections = [{"doc_id": "doc-d1", "section_index": 0, "section_id": "section-d1"}]
    chunks = [{"short_id": "D1", "chunk_id": "legacy-only", "_targets": ["legacy"]}]

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

    assert "chunks modern" in summary["failed"]["D1"]
    assert summary["deferred_removals"] == ["D_OLD"]
    assert writer.texte_bundles == []
    assert writer.texte_cascades == []
    fields = dict(grist.writebacks)[1]
    assert fields["statut"] == "erreur"
    assert "ingere_prod" not in fields


def test_ingest_delta_failure_keeps_stale_but_applies_explicit_abrogation(monkeypatch: pytest.MonkeyPatch) -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste(_arts(("LEGIARTI_NEW", "VIGUEUR"), ("LEGIARTI_ABROGE", "ABROGE")))
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


def test_ingest_delta_targeted_text_does_not_overwrite_code_aggregate() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist([_rec(1, **_code()), _rec(2, **_texte("D1", statut="ingere"))])
    piste = _FakePiste(_arts(("LEGIARTI001", "VIGUEUR")))
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 7), D1=("old", 2)))

    legifrance_ingestion.ingest_delta(
        writer,
        grist,
        piste,
        documents,
        sections,
        chunks,
        requested={"D1"},
        toc_date_millis=1000,
    )

    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert 1 not in by_record
    assert by_record[2]["statut"] == "ingere"


def test_ingest_delta_limbo_code_is_protected_without_writeback() -> None:
    grist = _RecordingGrist([_rec(1, **_code(statut="en_attente"))])
    piste = _FakePiste([])
    writer = _DeltaWriter(_corpus(LEGIARTI001=("h1", 3)))

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, [], [], [], toc_date_millis=1000)

    assert summary["plan"]["protected_limbo"]["sample"] == ["LEGIARTI001"]
    assert grist.writebacks == []
    assert writer.article_cascades == []


def test_ingest_delta_staging_only_touches_toggles() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(_delta_records())
    piste = _FakePiste(_arts(("LEGIARTI002", "VIGUEUR")))
    writer = _DeltaWriter(_corpus(D1=("hd1-old", 3), D2=("hd2", 2)))

    legifrance_ingestion.ingest_delta(writer, grist, piste, documents, sections, chunks, target_env="staging", toc_date_millis=1000)

    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[1] == {"ingere_staging": True}  # code agrégé
    assert by_record[2] == {"ingere_staging": True}  # texte ingéré
    assert by_record[3] == {"ingere_staging": False}  # texte cascadé
    assert all("statut" not in fields for fields in by_record.values())


def test_ingest_delta_mass_stale_guard_blocks_cascade() -> None:
    # Bout-en-bout job : au-delà de --max-auto-stale, aucune cascade des stale.
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste(_arts(("LEGIARTI001", "VIGUEUR")))
    corpus = _corpus(LEGIARTI001=("h1", 1))
    corpus.update({f"VIEUX_DOC_{i:04d}": {"doc_id": f"d{i}", "checksum": "h", "nb_chunks": 1} for i in range(10)})
    writer = _DeltaWriter(corpus)
    documents = [{"short_id": "LEGIARTI001", "doc_id": "da1", "checksum": "h1"}]

    summary = legifrance_ingestion.ingest_delta(writer, grist, piste, documents, [], [], toc_date_millis=1000, max_auto_stale=5)

    assert summary["plan"]["mass_stale_guard"] is True
    assert summary["plan"]["flagged"]["count"] == 10
    assert summary["applied"]["deleted"] == 0
    assert writer.article_cascades == [] and writer.texte_cascades == []


def test_ingest_delta_piste_http_error_raises_piste_error() -> None:
    grist = _RecordingGrist([_rec(1, **_code())])
    piste = _FakePiste(fail=True)
    writer = _DeltaWriter({})

    with pytest.raises(PisteError, match="tableMatieres"):
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
    piste = _FakePiste(_arts(("LEGIARTI002", "VIGUEUR")))
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
    assert payload["applied"] == {"ingested": 1, "skipped": 0, "deleted": 0, "failed": 0}
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
