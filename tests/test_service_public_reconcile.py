"""Tests de la réconciliation delta Service-Public (E2.3-a, #289).

Deux niveaux :
- fonctions pures (``select_manifest_rows`` / ``build_service_public_plan`` /
  ``plan_summary`` / ``writeback_fiche``) — aucun I/O ;
- job (``ingest_delta``) avec un Grist et un writer factices en mémoire.

Un test croise la sélection SP avec ``scripts/generate_service_public_config.py``
pour garantir que le manifest delta et la config générée ne divergent pas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from assistant_rh_data_engineering.jobs import service_public_ingestion
from assistant_rh_data_engineering.service_public import reconcile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_service_public_config as gen  # noqa: E402


def _rec(record_id: int, **fields: Any) -> dict[str, Any]:
    return {"id": record_id, "fields": fields}


def _sp(**fields: Any) -> dict[str, Any]:
    base = {"source_corpus": "service-public"}
    base.update(fields)
    return base


# --- select_manifest_rows -----------------------------------------------------


def test_select_manifest_rows_classifies_active_abrogated_limbo() -> None:
    records = [
        _rec(1, **_sp(id_extraction="F1", statut="ingere", abroge="non")),
        _rec(2, **_sp(id_extraction="F2", statut="a_ingerer")),
        _rec(3, **_sp(id_extraction="F3", statut="ingere", abroge="oui")),
        _rec(4, **_sp(id_extraction="F4", statut="a_supprimer")),
        _rec(5, **_sp(id_extraction="F5", statut="en_attente")),
        _rec(6, **_sp(id_extraction="F6", statut="supprime")),
        _rec(7, **_sp(id_extraction="F7", statut="erreur")),
    ]

    rows = {row.uid: row for row in reconcile.select_manifest_rows(records)}

    assert rows["F1"].active and not rows["F1"].abrogated
    assert rows["F2"].active
    assert rows["F3"].abrogated and not rows["F3"].active  # abroge juridique
    assert rows["F4"].abrogated  # a_supprimer opérateur
    assert rows["F5"].limbo and not rows["F5"].active and not rows["F5"].abrogated
    assert rows["F6"].abrogated  # supprime terminal
    assert rows["F7"].active  # erreur = retry
    assert rows["F1"].record_id == 1


def test_select_manifest_rows_rejects_service_public_row_without_fcode() -> None:
    records = [
        _rec(1, source_corpus="legifrance", uid="LEGIARTI0001", statut="ingere"),
        _rec(2, **_sp(titre_document="Une fiche sans code", statut="ingere")),
        _rec(3, **_sp(id_extraction="F9", statut="ingere")),
    ]

    with pytest.raises(reconcile.GristContractError, match="Grist 2"):
        reconcile.select_manifest_rows(records)


def test_select_manifest_rows_rejects_abrogated_row_without_fcode() -> None:
    # Sans F-code on ne sait pas QUOI cascader : refus franc, comme pour une active.
    records = [_rec(4, **_sp(titre_document="Fiche à retirer", statut="a_supprimer"))]

    with pytest.raises(reconcile.GristContractError, match="Grist 4"):
        reconcile.select_manifest_rows(records)


def test_select_manifest_rows_skips_limbo_row_without_fcode(capsys: Any) -> None:
    # Un brouillon opérateur (limbo, pas encore de F-code) ne doit pas bloquer le
    # cron quotidien : la ligne ne participe ni à l'ingestion ni à la cascade,
    # elle est ignorée avec un warning au lieu de tuer le run.
    records = [
        _rec(5, **_sp(titre_document="Brouillon en cours", statut="en_attente")),
        _rec(6, **_sp(titre_document="Brouillon sans statut")),
        _rec(3, **_sp(id_extraction="F9", statut="ingere")),
    ]

    rows = reconcile.select_manifest_rows(records)

    assert [row.uid for row in rows] == ["F9"]
    out = capsys.readouterr().out
    assert "Grist 5" in out and "Grist 6" in out and "ignorée" in out


def test_select_manifest_rows_extracts_fcode_from_title_then_uid() -> None:
    records = [
        _rec(1, **_sp(titre_document="Congés (F12345)", statut="ingere")),
        _rec(2, **_sp(uid="f6789", statut="ingere")),
    ]

    rows = {row.uid: row for row in reconcile.select_manifest_rows(records)}

    assert set(rows) == {"F12345", "F6789"}  # uppercase, titre puis uid


def test_select_manifest_rows_dedup_active_wins_over_removal() -> None:
    records = [
        _rec(1, **_sp(id_extraction="F1", statut="a_supprimer")),
        _rec(2, **_sp(id_extraction="F1", statut="ingere")),
    ]

    rows = reconcile.select_manifest_rows(records)

    assert len(rows) == 1
    assert rows[0].active and rows[0].record_id == 2


def test_select_manifest_rows_dedup_juridical_abrogation_beats_active() -> None:
    # Une abrogation juridique (abroge=oui) ne doit JAMAIS être réautorisée par une
    # ligne active dupliquée : un texte abrogé ne peut pas rester servi.
    records = [
        _rec(1, **_sp(id_extraction="F1", statut="", abroge="oui")),
        _rec(2, **_sp(id_extraction="F1", statut="ingere", abroge="non")),
    ]

    rows = reconcile.select_manifest_rows(records)

    assert len(rows) == 1
    assert rows[0].abrogated and not rows[0].active and rows[0].record_id == 1


# --- build_service_public_plan ------------------------------------------------


def _rows(records: list[dict[str, Any]]) -> list[reconcile.ServicePublicManifestRow]:
    return reconcile.select_manifest_rows(records)


def _corpus(**entries: tuple[str, int]) -> dict[str, dict[str, Any]]:
    return {uid: {"doc_id": f"d-{uid}", "checksum": checksum, "nb_chunks": nb} for uid, (checksum, nb) in entries.items()}


def test_build_plan_new_changed_unchanged_and_removals() -> None:
    rows = _rows(
        [
            _rec(1, **_sp(id_extraction="F1", statut="ingere")),  # unchanged
            _rec(2, **_sp(id_extraction="F2", statut="a_ingerer")),  # new
            _rec(3, **_sp(id_extraction="F3", statut="ingere")),  # changed
            _rec(4, **_sp(id_extraction="F4", statut="ingere", abroge="oui")),  # abrogated in corpus
            _rec(5, **_sp(id_extraction="F5", statut="a_supprimer")),  # abrogated, absent -> acknowledged
            _rec(6, **_sp(id_extraction="F6", statut="en_attente")),  # limbo -> protected
        ]
    )
    silver = {"F1": "h1", "F2": "h2", "F3": "h3-new"}
    corpus = _corpus(F1=("h1", 3), F3=("h3-old", 2), F4=("h4", 1), F6=("h6", 1), F7=("h7", 1))

    sp_plan = reconcile.build_service_public_plan(rows, silver, corpus)
    plan = sp_plan.plan

    assert plan.new == ("F2",)
    assert plan.changed == ("F3",)
    assert plan.unchanged == ("F1",)
    assert set(plan.auto_removals) == {"F4", "F7"}  # abrogé + stale, autoritaires
    assert set(plan.acknowledged) == {"F5"}
    assert sp_plan.protected == ("F6",)  # limbo jamais touché
    assert plan.flagged_removals == ()
    assert sp_plan.record_ids["F2"] == 2


def test_build_plan_zero_chunks_forces_reingest_despite_matching_hash() -> None:
    rows = _rows([_rec(1, **_sp(id_extraction="F1", statut="ingere"))])
    corpus = _corpus(F1=("h1", 0))  # hash égal mais 0 chunk (ingestion legacy non convergée)

    plan = reconcile.build_service_public_plan(rows, {"F1": "h1"}, corpus).plan

    assert plan.changed == ("F1",)
    assert plan.unchanged == ()


def test_build_plan_requested_subset_spares_rest_of_corpus() -> None:
    rows = _rows(
        [
            _rec(1, **_sp(id_extraction="F1", statut="ingere")),
            _rec(3, **_sp(id_extraction="F3", statut="ingere", abroge="oui")),
        ]
    )
    corpus = _corpus(F1=("h1", 3), F3=("h3", 2), F7=("h7", 1))

    # Run ciblé sur {F1, F3} : F7 (hors sous-ensemble) ne doit jamais être cascadé.
    plan = reconcile.build_service_public_plan(rows, {"F1": "h1"}, corpus, requested={"F1", "F3"}).plan

    assert plan.unchanged == ("F1",)
    assert set(plan.auto_removals) == {"F3"}
    assert "F7" not in plan.auto_removals


def test_build_plan_empty_manifest_guard_downgrades_stale_to_flagged() -> None:
    # Aucune ligne SP en Grist (fetch échoué) mais corpus non vide : anti-purge.
    plan = reconcile.build_service_public_plan([], {}, _corpus(F7=("h7", 1))).plan

    assert plan.auto_removals == ()
    assert plan.flagged_removals == ("F7",)


def test_build_plan_full_run_protects_active_fiche_absent_from_lake() -> None:
    # Run complet : une fiche active en Grist mais hors du lake chargé (config en
    # retard) est PENDING (protégée), jamais forcée en to_ingest ni cascadée ; son
    # statut corpus sain n'est pas touché (pas de faux échec).
    rows = _rows(
        [
            _rec(1, **_sp(id_extraction="F1", statut="ingere")),  # chargée
            _rec(2, **_sp(id_extraction="F2", statut="ingere")),  # active, pas chargée
        ]
    )
    corpus = _corpus(F1=("h1", 3), F2=("h2", 4))  # F2 saine en base
    sp_plan = reconcile.build_service_public_plan(rows, {"F1": "h1"}, corpus)  # silver: seule F1
    plan = sp_plan.plan

    assert plan.unchanged == ("F1",)
    assert "F2" not in plan.to_ingest
    assert "F2" not in plan.auto_removals
    assert sp_plan.pending == ("F2",)


def test_build_plan_targeted_removal_of_dropped_fiche_stays_authoritative() -> None:
    # Run ciblé --fiche-id F1 : F1 retirée du référentiel (absente des lignes) mais
    # encore au corpus. Le garde-fou anti-purge ne doit PAS s'armer (Grist a
    # répondu, lignes non vides) -> suppression autoritaire, comme un run complet.
    rows = _rows([_rec(2, **_sp(id_extraction="F2", statut="ingere"))])
    corpus = _corpus(F1=("h1", 1), F2=("h2", 1))

    plan = reconcile.build_service_public_plan(rows, {}, corpus, requested={"F1"}).plan

    assert plan.auto_removals == ("F1",)
    assert plan.flagged_removals == ()


def test_plan_summary_reports_counts_and_samples() -> None:
    rows = _rows(
        [
            _rec(2, **_sp(id_extraction="F2", statut="a_ingerer")),
            _rec(4, **_sp(id_extraction="F4", statut="ingere", abroge="oui")),
        ]
    )
    corpus = _corpus(F4=("h4", 1), F7=("h7", 1))

    summary = reconcile.plan_summary(reconcile.build_service_public_plan(rows, {"F2": "h2"}, corpus))

    assert summary["new"] == {"count": 1, "sample": ["F2"]}
    assert summary["abrogated"]["count"] == 1
    assert summary["stale"] == {"count": 1, "sample": ["F7"]}
    assert summary["to_ingest"]["count"] == 1
    assert summary["auto_removals"]["count"] == 2


# --- writeback_fiche ----------------------------------------------------------


class _RecordingGrist:
    def __init__(self, records: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self._records = records or []
        self._fail = fail
        self.writebacks: list[tuple[int, dict[str, Any]]] = []
        self.update_calls = 0

    def list_records(self, table_id: str | None = None) -> list[dict[str, Any]]:
        return self._records

    def update_records(self, records: list[dict[str, Any]], table_id: str | None = None) -> None:
        if self._fail:
            raise RuntimeError("grist down")
        self.update_calls += 1
        self.writebacks.extend((int(record["id"]), dict(record["fields"])) for record in records)


def test_writeback_fiche_writes_statut_and_reality_columns() -> None:
    grist = _RecordingGrist()

    reconcile.writeback_fiche(
        grist,
        42,
        statut=reconcile.STATUT_INGERE,
        statut_reel=reconcile.STATUT_REEL_INGERE,
        nb_chunks=7,
        hash_contenu="abc",
    )

    (record_id, fields) = grist.writebacks[0]
    assert record_id == 42
    assert fields["statut"] == "ingere"
    assert "statut_ingestion" not in fields
    assert fields["statut_ingestion_reelle"] == "ingere"
    assert fields["nb_chunks"] == 7
    assert fields["hash_contenu"] == "abc"
    assert "derniere_ingestion" in fields


def test_build_writeback_fields_routes_by_env() -> None:
    # prod (canonique) : statut + métadonnées + toggle ingere_prod.
    prod = reconcile.build_writeback_fields(statut="ingere", nb_chunks=3, corpus_present=True)
    assert prod["statut"] == "ingere"
    assert prod["ingere_prod"] is True
    assert "ingere_staging" not in prod

    # staging : UNIQUEMENT le toggle ingere_staging — jamais le statut canonique.
    staging = reconcile.build_writeback_fields(statut="ingere", nb_chunks=3, env="staging", corpus_present=True)
    assert staging == {"ingere_staging": True}

    # staging, réalité inconnue (échec) : rien à écrire.
    assert reconcile.build_writeback_fields(statut="erreur", erreur="boom", env="staging") == {}


def test_writeback_fiche_noop_without_record_id() -> None:
    grist = _RecordingGrist()
    reconcile.writeback_fiche(grist, None, statut=reconcile.STATUT_INGERE)
    assert grist.writebacks == []


def test_writeback_fiche_swallows_grist_errors(capsys: Any) -> None:
    grist = _RecordingGrist(fail=True)
    reconcile.writeback_fiche(grist, 1, statut=reconcile.STATUT_ERREUR, erreur="boom")
    assert "writeback Grist échoué" in capsys.readouterr().out


# --- cohérence avec le générateur de config (anti-drift) ----------------------


def test_reconcile_active_selection_matches_config_generator() -> None:
    records = [
        _rec(1, **_sp(id_extraction="F1", statut="ingere", abroge="non")),
        _rec(2, **_sp(id_extraction="F2", statut="a_ingerer")),
        _rec(3, **_sp(id_extraction="F3", statut="ingere", abroge="oui")),  # exclu (abrogé)
        _rec(4, **_sp(id_extraction="F4", statut="a_supprimer")),  # exclu (suppression)
        _rec(5, **_sp(id_extraction="F5", statut="en_attente")),  # exclu (limbo)
        _rec(6, source_corpus="legifrance", uid="LEGIARTI1", statut="ingere"),  # autre corpus
    ]

    active = sorted(row.uid for row in reconcile.select_manifest_rows(records) if row.active)

    assert active == gen.selected_fiche_ids(records)


# --- ingest_delta (job) -------------------------------------------------------


class _DeltaWriter:
    def __init__(self, corpus: dict[str, dict[str, Any]]) -> None:
        self._corpus = corpus
        self.bundles: list[str] = []
        self.cascaded: tuple[list[str], str] | None = None

    def list_short_ids_with_checksum(self, source: str, table: str | None = None) -> dict[str, dict[str, Any]]:
        return self._corpus

    def ingest_document_bundle(self, document: dict[str, Any], sections: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, int]:
        self.bundles.append(str(document.get("short_id")).upper())
        return {"documents": 1, "sections": len(sections), "chunks_deleted": 0, "chunks": len(chunks)}

    def delete_documents_cascade(self, short_ids: list[str], table: str | None = None, *, source: str) -> dict[str, int]:
        self.cascaded = (list(short_ids), source)
        return {"chunks": 5, "sections": 2, "documents": len(short_ids)}


def _artifacts() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents = [
        {"short_id": "F1", "doc_id": "d1", "checksum": "h1"},
        {"short_id": "F2", "doc_id": "d2", "checksum": "h2new"},
    ]
    sections = [{"doc_id": "d2", "section_index": 0, "section_id": "s2"}]
    chunks = [{"short_id": "F2", "hash_id": "c2", "source_document_id": "d2", "section_id": "s2"}]
    return documents, sections, chunks


def test_ingest_delta_dry_run_computes_plan_without_writes() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(
        [
            _rec(101, **_sp(id_extraction="F1", statut="ingere")),
            _rec(102, **_sp(id_extraction="F2", statut="a_ingerer")),
        ]
    )
    writer = _DeltaWriter(_corpus(F1=("h1", 3), F7=("h7", 1)))

    summary = service_public_ingestion.ingest_delta(writer, grist, documents, sections, chunks, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["plan"]["new"]["sample"] == ["F2"]
    assert summary["plan"]["unchanged"]["sample"] == ["F1"]
    assert summary["plan"]["stale"]["sample"] == ["F7"]
    assert summary["applied"] == {"ingested": 0, "skipped": 0, "deleted": 0, "failed": 0}
    assert writer.bundles == []  # aucune écriture
    assert writer.cascaded is None
    assert grist.writebacks == []


def test_ingest_delta_apply_ingests_changed_cascades_and_writes_back() -> None:
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(
        [
            _rec(101, **_sp(id_extraction="F1", statut="ingere")),  # unchanged
            _rec(102, **_sp(id_extraction="F2", statut="a_ingerer")),  # new -> ingest
            _rec(103, **_sp(id_extraction="F3", statut="ingere", abroge="oui")),  # abrogé -> cascade
        ]
    )
    writer = _DeltaWriter(_corpus(F1=("h1", 3), F3=("h3", 2), F7=("h7", 1)))

    summary = service_public_ingestion.ingest_delta(writer, grist, documents, sections, chunks)

    assert writer.bundles == ["F2"]  # seule la nouvelle est ré-ingérée
    assert writer.cascaded == (["F3", "F7"], "service_public")  # abrogé + stale
    assert summary["applied"] == {"ingested": 1, "skipped": 1, "deleted": 2, "failed": 0}

    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[102]["statut"] == "ingere"
    assert by_record[102]["statut_ingestion_reelle"] == "ingere"
    assert by_record[102]["ingere_prod"] is True  # run prod par défaut
    assert by_record[101]["statut"] == "ingere"  # unchanged tracé aussi
    assert by_record[103]["statut"] == "supprime"
    assert by_record[103]["statut_ingestion_reelle"] == "non_trouve"
    assert by_record[103]["ingere_prod"] is False
    # F7 (stale, sans ligne Grist) est cascadé mais non writeback (pas de record_id).
    assert set(by_record) == {101, 102, 103}
    # Writebacks poussés en UN lot (pas un appel API par fiche).
    assert grist.update_calls == 1


def test_ingest_delta_staging_only_touches_its_toggle() -> None:
    # Run staging : le doc Grist est partagé — on ne réécrit JAMAIS le statut
    # canonique (propriété du run prod), seulement ingere_staging. Un échec ne
    # touche même pas le toggle (réalité inconnue).
    documents, sections, chunks = _artifacts()
    grist = _RecordingGrist(
        [
            _rec(101, **_sp(id_extraction="F1", statut="ingere")),  # unchanged
            _rec(102, **_sp(id_extraction="F2", statut="a_ingerer")),  # new -> ingest
            _rec(103, **_sp(id_extraction="F3", statut="ingere", abroge="oui")),  # abrogé -> cascade
            _rec(104, **_sp(id_extraction="F4", statut="a_ingerer")),  # artefact absent -> échec
        ]
    )
    writer = _DeltaWriter(_corpus(F1=("h1", 3), F3=("h3", 2)))

    summary = service_public_ingestion.ingest_delta(
        writer, grist, documents, sections, chunks, requested={"F1", "F2", "F3", "F4"}, target_env="staging"
    )

    assert summary["applied"]["failed"] == 1
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[101] == {"ingere_staging": True}
    assert by_record[102] == {"ingere_staging": True}
    assert by_record[103] == {"ingere_staging": False}
    # F4 en échec : aucun writeback staging (le statut canonique reste intact).
    assert 104 not in by_record


def test_ingest_delta_acknowledged_terminal_row_not_rewritten_each_run() -> None:
    # Ligne déjà `supprime` et absente du corpus : acquittement déjà fait, on ne
    # la re-touche pas (sinon derniere_ingestion avancerait chaque nuit). Une
    # ligne `a_supprimer` absente du corpus reçoit en revanche son acquittement.
    grist = _RecordingGrist(
        [
            _rec(201, **_sp(id_extraction="F1", statut="supprime")),  # déjà acquittée
            _rec(202, **_sp(id_extraction="F2", statut="a_supprimer")),  # à acquitter
        ]
    )
    writer = _DeltaWriter({})

    summary = service_public_ingestion.ingest_delta(writer, grist, [], [], [])

    assert summary["applied"] == {"ingested": 0, "skipped": 0, "deleted": 0, "failed": 0}
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert set(by_record) == {202}
    assert by_record[202]["statut"] == "supprime"


def test_ingest_delta_targeted_missing_artifact_is_a_failure() -> None:
    # Run CIBLÉ --fiche-id F2 : l'opérateur demande explicitement F2, absente du
    # lake -> erreur (le bon signal), pas de crash.
    grist = _RecordingGrist([_rec(102, **_sp(id_extraction="F2", statut="a_ingerer"))])
    writer = _DeltaWriter({})

    summary = service_public_ingestion.ingest_delta(writer, grist, [], [], [], requested={"F2"})

    assert summary["status"] == "partial"
    assert summary["applied"]["failed"] == 1
    assert "F2" in summary["failed"]
    assert writer.bundles == []
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[102]["statut"] == "erreur"


# --- main() end-to-end (dispatch argparse -> GristClient -> ingest_delta) ------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _seed_lake(lake_root: Path, short_id: str, checksum: str) -> None:
    _write_json(
        lake_root / "silver" / "documents" / f"{short_id}.document.json",
        {"doc_id": f"doc-{short_id}", "short_id": short_id, "title": short_id, "checksum": checksum},
    )
    _write_jsonl(
        lake_root / "silver" / "sections" / f"{short_id}.sections.jsonl",
        [{"section_id": f"section-{short_id}", "doc_id": f"doc-{short_id}", "section_index": 0}],
    )
    _write_jsonl(
        lake_root / "gold" / "chunks" / f"{short_id}.chunks.jsonl",
        [{"hash_id": f"chunk-{short_id}", "short_id": short_id, "source_document_id": f"doc-{short_id}", "section_id": f"section-{short_id}"}],
    )


def _patch_grist(monkeypatch: pytest.MonkeyPatch, grist: _RecordingGrist) -> None:
    import assistant_rh_data_engineering.utils.grist as grist_module

    monkeypatch.setattr(grist_module, "GristClient", lambda *a, **k: grist)


def _patch_writer(monkeypatch: pytest.MonkeyPatch, writer: _DeltaWriter) -> None:
    import assistant_rh_data_engineering.service_public.db as service_public_db

    monkeypatch.setattr(service_public_db, "ServicePublicDbWriter", lambda *a, **k: writer)


def test_main_delta_dry_run_prints_plan_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake_root = tmp_path / "lake"
    _seed_lake(lake_root, "F1", "h1")
    config_path = tmp_path / "service_public_fiches.json"
    _write_json(config_path, {"fiche_ids": ["F1"]})

    grist = _RecordingGrist([_rec(101, **_sp(id_extraction="F1", statut="ingere"))])
    writer = _DeltaWriter(_corpus(F1=("h1", 3), F7=("h7", 1)))
    _patch_grist(monkeypatch, grist)
    _patch_writer(monkeypatch, writer)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-ingest", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--dsn", "postgresql://unused", "--delta", "--dry-run"],
    )

    assert service_public_ingestion.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "delta"
    assert payload["dry_run"] is True
    assert payload["plan"]["unchanged"]["sample"] == ["F1"]
    assert payload["plan"]["stale"]["sample"] == ["F7"]
    assert writer.bundles == [] and writer.cascaded is None
    assert grist.writebacks == []


def test_main_delta_apply_ingests_and_cascades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake_root = tmp_path / "lake"
    _seed_lake(lake_root, "F2", "h2new")
    config_path = tmp_path / "service_public_fiches.json"
    _write_json(config_path, {"fiche_ids": ["F2"]})

    grist = _RecordingGrist(
        [
            _rec(102, **_sp(id_extraction="F2", statut="a_ingerer")),
            _rec(103, **_sp(id_extraction="F3", statut="ingere", abroge="oui")),
        ]
    )
    writer = _DeltaWriter(_corpus(F3=("h3", 2)))
    _patch_grist(monkeypatch, grist)
    _patch_writer(monkeypatch, writer)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-ingest", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--dsn", "postgresql://unused", "--delta"],
    )

    assert service_public_ingestion.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == {"ingested": 1, "skipped": 0, "deleted": 1, "failed": 0}
    assert writer.bundles == ["F2"]
    assert writer.cascaded == (["F3"], "service_public")
    by_record = {record_id: fields for record_id, fields in grist.writebacks}
    assert by_record[102]["statut"] == "ingere"
    assert by_record[103]["statut"] == "supprime"


def test_main_delta_grist_failure_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Un échec Grist (env manquant, fetch KO, contrat violé) doit sortir avec un
    # message opérateur propre (SystemExit), pas une stacktrace brute.
    import assistant_rh_data_engineering.utils.grist as grist_module

    lake_root = tmp_path / "lake"
    _seed_lake(lake_root, "F1", "h1")
    config_path = tmp_path / "service_public_fiches.json"
    _write_json(config_path, {"fiche_ids": ["F1"]})

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise grist_module.GristError("GRIST_API_KEY manquant")

    _patch_writer(monkeypatch, _DeltaWriter({}))
    monkeypatch.setattr(grist_module, "GristClient", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sp-ingest", "--lake-root", str(lake_root), "--fiche-config", str(config_path), "--dsn", "postgresql://unused", "--delta"],
    )

    with pytest.raises(SystemExit, match="Échec Grist en mode --delta"):
        service_public_ingestion.main()
