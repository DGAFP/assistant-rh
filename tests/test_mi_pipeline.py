"""Pipeline médaillon MI (#246): silver/gold, réconciliation et writeback.

Tout est testé hors réseau: Grist, dropzone, OCR et Postgres sont des fakes
en mémoire; les embeddings sont désactivés dans la config de test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from assistant_rh_data_engineering.mi.bronze import MiBronzeAsset
from assistant_rh_data_engineering.mi.config import (
    LakePaths,
    MiPipelineConfig,
)
from assistant_rh_data_engineering.mi.gold import MiGoldBuilder
from assistant_rh_data_engineering.mi.pipeline import MiPipeline, plan_reconciliation
from assistant_rh_data_engineering.mi.silver import MiSilverBuilder
from assistant_rh_data_engineering.utils.grist import ManifestRow
from assistant_rh_data_engineering.utils.ocr import OcrResult

OCR_MARKDOWN = """## Dispositions générales

Les agents du ministère bénéficient du présent dispositif.

### Conditions d'éligibilité

Une ancienneté de deux ans est requise pour en bénéficier pleinement.

### Modalités de demande

La demande est déposée par voie hiérarchique.
"""


def make_row(uid: str = "MI-0001", *, statut: str = "en_vigueur", record_id: int = 11, **fields: Any) -> ManifestRow:
    return ManifestRow(
        record_id=record_id,
        corpus="MI",
        uid=uid,
        titre=fields.pop("titre", f"Circulaire {uid}"),
        cle_bucket=fields.pop("cle_bucket", f"mi/{uid.lower()}_circulaire.pdf"),
        statut=statut,
        fields=fields,
    )


def make_ocr(markdown: str = OCR_MARKDOWN) -> OcrResult:
    return OcrResult(provider="albert", version="mistral-ocr-2512", markdown=markdown, pages=[{"index": 0}])


def make_asset(row: ManifestRow, sha256: str = "a" * 64, markdown: str = OCR_MARKDOWN) -> MiBronzeAsset:
    return MiBronzeAsset(
        row=row,
        sha256=sha256,
        source_path=Path("/nonexistent/source.pdf"),
        ocr=make_ocr(markdown),
        ocr_from_cache=False,
    )


def make_config(tmp_path: Path) -> MiPipelineConfig:
    config = MiPipelineConfig(paths=LakePaths(root_dir=tmp_path / "lake"), target_env="staging")
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    return config


# --- plan_reconciliation (fonction pure) -------------------------------------


def test_plan_classifies_new_changed_unchanged_and_orphans() -> None:
    expected = {"MI-0001": make_row("MI-0001"), "MI-0002": make_row("MI-0002"), "MI-0003": make_row("MI-0003")}
    current = {
        "MI-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4},
        "MI-0002": {"doc_id": "d2", "checksum": "old", "nb_chunks": 4},
        "MI-0999": {"doc_id": "d9", "checksum": "x", "nb_chunks": 2},
    }
    checksums = {"MI-0001": "a" * 64, "MI-0002": "b" * 64, "MI-0003": "c" * 64}

    plan = plan_reconciliation(expected, current, checksums)

    assert plan["ignore_inchange"] == ["MI-0001"]
    assert plan["ingest"] == ["MI-0002", "MI-0003"]
    assert plan["delete"] == ["MI-0999"]


def test_plan_retries_zero_chunk_documents() -> None:
    # Leçon de l'audit MATTE: hash identique mais zéro chunk => retraiter.
    expected = {"MI-0001": make_row("MI-0001")}
    current = {"MI-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 0}}

    plan = plan_reconciliation(expected, current, {"MI-0001": "a" * 64})

    assert plan["ingest"] == ["MI-0001"]


def test_plan_force_reocr_reingests_everything() -> None:
    expected = {"MI-0001": make_row("MI-0001")}
    current = {"MI-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4}}

    plan = plan_reconciliation(expected, current, {"MI-0001": "a" * 64}, force_reocr=True)

    assert plan["ingest"] == ["MI-0001"]
    assert plan["ignore_inchange"] == []


# --- Silver -------------------------------------------------------------------


def test_silver_builds_document_and_sections(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    builder = MiSilverBuilder(config.silver)
    row = make_row(theme="Action sociale")

    document, sections = builder.build_bundle(make_asset(row))

    assert document["source"] == "mi"
    assert document["short_id"] == "MI-0001"
    assert document["checksum"] == "a" * 64
    assert document["publisher"] == "Ministère de l'Intérieur"
    assert document["storage_path"] == row.cle_bucket
    assert document["metadata"]["theme"] == "Action sociale"
    assert document["doc_markdown"].startswith("## Circulaire MI-0001")
    # Titre + heading OCR ## + 2 sous-sections ###
    assert len(sections) >= 3
    assert all(section["doc_id"] == document["doc_id"] for section in sections)


def test_silver_doc_ids_are_stable_across_runs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    builder = MiSilverBuilder(config.silver)

    doc_a, _ = builder.build_bundle(make_asset(make_row()))
    doc_b, _ = builder.build_bundle(make_asset(make_row(), sha256="b" * 64))

    assert doc_a["doc_id"] == doc_b["doc_id"]


def test_silver_headingless_ocr_still_yields_a_section(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    builder = MiSilverBuilder(config.silver)

    document, sections = builder.build_bundle(
        make_asset(make_row(), markdown="Texte OCR brut sans aucun heading markdown, sur une seule ligne assez longue.")
    )

    # Le titre prépendu (## titre) garantit au moins une section couvrant tout.
    assert len(sections) == 1
    assert "Texte OCR brut" in sections[0]["section_markdown"]


def test_silver_h1_headings_are_detected_as_sections(tmp_path: Path) -> None:
    # mistral-ocr émet les titres de section en # (H1); sans rétrogradation le
    # splitter les ignore (constat d'audit du 2026-07-04 sur le corpus MI réel).
    config = make_config(tmp_path)
    markdown = (
        "# 1. Dispositions communes\n\nContenu de la première partie, suffisamment long pour être indexable.\n\n"
        "# 2. Dispositions particulières\n\nContenu de la seconde partie, suffisamment long pour être indexable.\n"
    )

    _, sections = MiSilverBuilder(config.silver).build_bundle(make_asset(make_row(), markdown=markdown))

    headings = [section["heading"] for section in sections]
    assert "1. Dispositions communes" in headings
    assert "2. Dispositions particulières" in headings


def test_silver_strips_page_boilerplate_and_adds_page_markers(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.mi.silver import normalize_ocr_markdown

    header = "Direction des ressources humaines ministérielle"
    pages = [{"index": i, "markdown": f"{header}\n\n## Partie {i}\n\nContenu de la page numéro {i}.\n\n- Page {i} -"} for i in range(1, 5)]
    ocr = OcrResult(provider="albert", version="v", markdown="", pages=pages)

    normalized = normalize_ocr_markdown(ocr, "Guide test")

    assert normalized.startswith("## Guide test")
    assert header not in normalized
    assert "- Page 2 -" not in normalized
    assert "<!-- PAGE: 3 -->" in normalized
    assert "Contenu de la page numéro 3." in normalized
    # Rétrogradation: les ## OCR deviennent ### sous le titre.
    assert "### Partie 2" in normalized


def test_silver_keeps_legitimate_repeated_content(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.mi.silver import normalize_ocr_markdown

    # Contenu répétitif au milieu de page (cellules de formulaire): conservé.
    repeated = "Ce motif de dispense n'est valable que :"
    pages = [
        {
            "index": i,
            "markdown": (
                f"Première ligne de la page {i}.\nDeuxième ligne de la page {i}.\nTroisième ligne de la page {i}.\n\n"
                f"{repeated}\n\n{repeated}\n\n"
                f"Antépénultième ligne {i}.\nAvant-dernière ligne {i}.\nDernière ligne de la page {i}."
            ),
        }
        for i in range(1, 4)
    ]
    ocr = OcrResult(provider="albert", version="v", markdown="", pages=pages)

    normalized = normalize_ocr_markdown(ocr, "Formulaire")

    assert repeated in normalized


def test_silver_sections_partition_the_document(tmp_path: Path) -> None:
    # Le contenu d'une section parente ne doit pas inclure celui de ses
    # enfants: sinon chaque texte est chunké 2-3x (constat d'audit 2026-07-04).
    config = make_config(tmp_path)
    markdown = (
        "# Partie unique\n\nIntroduction de la partie, assez longue pour être indexable sans problème.\n\n"
        "## Sous-partie A\n\nContenu détaillé de la sous-partie A, également assez long pour être indexable.\n"
    )

    document, sections = MiSilverBuilder(config.silver).build_bundle(make_asset(make_row(), markdown=markdown))

    parent = next(s for s in sections if s["heading"] == "Partie unique")
    child = next(s for s in sections if s["heading"] == "Sous-partie A")
    assert "Introduction de la partie" in parent["section_markdown"]
    assert "Contenu détaillé" not in parent["section_markdown"]
    assert "Contenu détaillé" in child["section_markdown"]

    # Et au global: le texte des chunks ne dépasse pas ~1x le document.
    chunks = MiGoldBuilder(config.embeddings, config.gold).build_chunks(document, sections)
    total_chunk_chars = sum(len(c["text"]) for c in chunks)
    assert total_chunk_chars <= len(document["doc_markdown"]) * 1.2


# --- Gold ---------------------------------------------------------------------


def test_gold_chunks_carry_mi_source_and_stable_hash(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    document, sections = MiSilverBuilder(config.silver).build_bundle(make_asset(make_row(theme="Mobilité")))
    builder = MiGoldBuilder(config.embeddings, config.gold)

    chunks = builder.build_chunks(document, sections)

    assert chunks
    assert {chunk["source"] for chunk in chunks} == {"MI"}
    assert {chunk["short_id"] for chunk in chunks} == {"MI-0001"}
    assert all(chunk["source_document_id"] == document["doc_id"] for chunk in chunks)
    assert all(chunk["role"] == "SECTION_ATOMIC" for chunk in chunks)
    assert all(chunk["thematique"] == "Mobilité" for chunk in chunks)

    rerun = builder.build_chunks(document, sections)
    assert [chunk["hash_id"] for chunk in rerun] == [chunk["hash_id"] for chunk in chunks]


def test_gold_skips_non_indexable_sections(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    document, sections = MiSilverBuilder(config.silver).build_bundle(make_asset(make_row()))
    for section in sections:
        section["is_indexable"] = False

    chunks = MiGoldBuilder(config.embeddings, config.gold).build_chunks(document, sections)

    assert chunks == []


# --- Fakes pour le pipeline complet --------------------------------------------


class FakeGrist:
    def __init__(self, records: list[dict[str, Any]]):
        self.records = records
        self.writebacks: list[tuple[int, dict[str, Any]]] = []

    def list_columns(self, table_id: str | None = None) -> list[str]:
        return ["source_corpus", "uid", "titre_document", "cle_bucket", "abroge", "date_publication"]

    def list_records(self, table_id: str | None = None, *, filter: dict | None = None) -> list[dict[str, Any]]:
        return self.records

    def writeback_status(self, record_id: int, fields: dict[str, Any], table_id: str | None = None) -> None:
        self.writebacks.append((record_id, fields))

    def status_for(self, record_id: int) -> list[str]:
        return [fields["statut_ingestion"] for rid, fields in self.writebacks if rid == record_id]


class FakeStore:
    """Dropzone + cache OCR en mémoire (mêmes signatures que PdfSourceStore)."""

    def __init__(self, documents: dict[str, bytes]):
        self.documents = documents
        self.ocr_cache: dict[str, OcrResult] = {}
        self.put_pdf_calls: list[str] = []
        self.put_ocr_calls: list[str] = []

    def fetch_source_pdf(self, cle_bucket: str, destination: Path) -> Path:
        if cle_bucket not in self.documents:
            raise RuntimeError(f"PDF introuvable dans la dropzone: {cle_bucket}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.documents[cle_bucket])
        return destination

    def get_cached_ocr(self, target_env: str, ministere: str, provider: str, version: str, sha256: str) -> OcrResult | None:
        return self.ocr_cache.get(sha256)

    def put_pdf(self, target_env: str, ministere: str, sha256: str, pdf_path: Path) -> None:
        self.put_pdf_calls.append(sha256)

    def put_ocr(self, target_env: str, ministere: str, sha256: str, result: OcrResult) -> None:
        self.ocr_cache[sha256] = result
        self.put_ocr_calls.append(sha256)


class FakeOcrProvider:
    name = "albert"
    version = "mistral-ocr-2512"

    def __init__(self) -> None:
        self.calls = 0

    def ocr_pdf(self, pdf_bytes: bytes, document_name: str = "document.pdf") -> OcrResult:
        self.calls += 1
        return make_ocr()


class FakeDbWriter:
    def __init__(self, state: dict[str, dict[str, Any]] | None = None):
        self.state = dict(state or {})
        self.upserted_documents: list[dict[str, Any]] = []
        self.upserted_sections: list[dict[str, Any]] = []
        self.replaced_chunks: list[tuple[list[str], int]] = []
        self.cascade_deletes: list[list[str]] = []
        self.runs: list[dict[str, Any]] = []

    def list_short_ids_with_checksum(self, source: str, table: str | None = None) -> dict[str, dict[str, Any]]:
        return dict(self.state)

    def upsert_documents(self, documents: list[dict[str, Any]]) -> int:
        self.upserted_documents.extend(documents)
        return len(documents)

    def upsert_sections(self, sections: list[dict[str, Any]]) -> int:
        self.upserted_sections.extend(sections)
        return len(sections)

    def replace_chunks_by_short_ids(self, short_ids: list[str], chunks: list[dict[str, Any]], **kwargs: Any) -> tuple[int, int]:
        self.replaced_chunks.append((short_ids, len(chunks)))
        for short_id in short_ids:
            document = next((doc for doc in self.upserted_documents if doc["short_id"] == short_id), None)
            self.state[short_id] = {
                "doc_id": document["doc_id"] if document else short_id,
                "checksum": document["checksum"] if document else None,
                "nb_chunks": len(chunks),
            }
        return (0, len(chunks))

    def delete_documents_cascade(self, short_ids: list[str], table: str | None = None, *, source: str) -> dict[str, int]:
        self.cascade_deletes.append(sorted(short_ids))
        chunks = sum(int(self.state.get(short_id, {}).get("nb_chunks") or 0) for short_id in short_ids)
        for short_id in short_ids:
            self.state.pop(short_id, None)
        return {"chunks": chunks, "sections": 0, "documents": len(short_ids)}

    def insert_ingestion_run(self, run: dict[str, Any]) -> int:
        self.runs.append(run)
        return 1


def grist_record(uid: str, *, record_id: int, abroge: str = "", cle_bucket: str | None = None) -> dict[str, Any]:
    return {
        "id": record_id,
        "fields": {
            "source_corpus": "MI",
            "uid": uid,
            "titre_document": f"Circulaire {uid}",
            "cle_bucket": cle_bucket if cle_bucket is not None else f"mi/{uid.lower()}_circulaire.pdf",
            "abroge": abroge,
        },
    }


def build_pipeline(
    tmp_path: Path,
    *,
    records: list[dict[str, Any]],
    documents: dict[str, bytes],
    state: dict[str, dict[str, Any]] | None = None,
) -> tuple[MiPipeline, FakeGrist, FakeStore, FakeOcrProvider, FakeDbWriter]:
    grist = FakeGrist(records)
    store = FakeStore(documents)
    ocr = FakeOcrProvider()
    writer = FakeDbWriter(state)
    pipeline = MiPipeline(
        make_config(tmp_path),
        grist_client=grist,
        store=store,
        ocr_provider=ocr,
        db_writer=writer,
    )
    return pipeline, grist, store, ocr, writer


# --- Pipeline: run complet ------------------------------------------------------


def test_run_ingests_new_documents_and_writes_back_ok(tmp_path: Path) -> None:
    records = [grist_record("MI-0001", record_id=11), grist_record("MI-0002", record_id=12)]
    documents = {
        "mi/mi-0001_circulaire.pdf": b"%PDF-doc1",
        "mi/mi-0002_circulaire.pdf": b"%PDF-doc2",
    }
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 2
    assert summary["failed_count"] == 0
    assert ocr.calls == 2
    assert len(writer.upserted_documents) == 2
    assert len(writer.replaced_chunks) == 2
    assert grist.status_for(11) == ["ok"]
    assert grist.status_for(12) == ["ok"]
    assert len(writer.runs) == 1
    assert writer.runs[0]["run_id"] == summary["run_id"]
    # Le cache bronze est alimenté (PDF + OCR) pour les runs suivants.
    assert len(store.put_ocr_calls) == 2


def test_rerun_is_idempotent_all_ignore_inchange(tmp_path: Path) -> None:
    records = [grist_record("MI-0001", record_id=11)]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    pipeline.run(ingest=True)
    assert ocr.calls == 1

    # Re-run immédiat: mêmes fichiers, état DB porté par le FakeDbWriter.
    pipeline2 = MiPipeline(
        make_config(tmp_path),
        grist_client=grist,
        store=store,
        ocr_provider=ocr,
        db_writer=writer,
    )
    summary = pipeline2.run(ingest=True)

    assert summary["ingested_count"] == 0
    assert summary["skipped_count"] == 1
    assert ocr.calls == 1  # aucun re-paiement OCR
    assert grist.status_for(11) == ["ok", "ignore_inchange"]


def test_removed_manifest_row_triggers_cascade_delete(tmp_path: Path) -> None:
    state = {"MI-0009": {"doc_id": "d9", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MI-0001", record_id=11)]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents, state=state)

    summary = pipeline.run(ingest=True)

    assert summary["deleted_count"] == 1
    assert writer.cascade_deletes == [["MI-0009"]]
    assert summary["details"]["MI-0009"]["statut"] == "supprime"


def test_abrogated_row_is_deleted_and_written_back(tmp_path: Path) -> None:
    state = {"MI-0001": {"doc_id": "d1", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MI-0001", record_id=11, abroge="oui")]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={}, state=state)

    summary = pipeline.run(ingest=True)

    assert writer.cascade_deletes == [["MI-0001"]]
    assert grist.status_for(11) == ["supprime"]
    assert summary["deleted_count"] == 1


def test_rejected_manifest_row_is_written_back_and_run_continues(tmp_path: Path) -> None:
    records = [
        grist_record("MI-0001", record_id=11),
        grist_record("MI-0002", record_id=12, cle_bucket=""),  # rejetée: cle_bucket vide
    ]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert summary["rejected_count"] == 1
    assert grist.status_for(12) == ["erreur"]


def test_document_failure_writes_back_erreur_and_run_continues(tmp_path: Path) -> None:
    records = [grist_record("MI-0001", record_id=11), grist_record("MI-0002", record_id=12)]
    # MI-0002 absent de la dropzone => échec au téléchargement.
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert summary["failed_count"] == 1
    assert grist.status_for(11) == ["ok"]
    assert grist.status_for(12) == ["erreur"]


def test_dry_run_makes_no_writes(tmp_path: Path) -> None:
    state = {"MI-0009": {"doc_id": "d9", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MI-0001", record_id=11)]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents, state=state)

    summary = pipeline.run(ingest=True, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["plan"]["ingest"] == ["MI-0001"]
    assert summary["plan"]["delete"] == ["MI-0009"]
    assert ocr.calls == 0
    assert grist.writebacks == []
    assert writer.upserted_documents == []
    assert writer.cascade_deletes == []
    assert writer.runs == []


def test_doc_id_filter_disables_orphan_deletion(tmp_path: Path) -> None:
    state = {"MI-0009": {"doc_id": "d9", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MI-0001", record_id=11), grist_record("MI-0002", record_id=12)]
    documents = {
        "mi/mi-0001_circulaire.pdf": b"%PDF-doc1",
        "mi/mi-0002_circulaire.pdf": b"%PDF-doc2",
    }
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents, state=state)

    summary = pipeline.run(ingest=True, doc_ids=["mi-0001"])

    assert summary["ingested_count"] == 1
    assert summary["deleted_count"] == 0
    assert writer.cascade_deletes == []


def test_skip_grist_writeback(tmp_path: Path) -> None:
    records = [grist_record("MI-0001", record_id=11)]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True, skip_grist_writeback=True)

    assert summary["ingested_count"] == 1
    assert grist.writebacks == []


def test_ocr_cache_hit_skips_provider_call(tmp_path: Path) -> None:
    records = [grist_record("MI-0001", record_id=11)]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr()

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert ocr.calls == 0
    assert summary["details"]["MI-0001"]["ocr_from_cache"] is True


def test_force_reocr_bypasses_cache_and_delta(tmp_path: Path) -> None:
    records = [grist_record("MI-0001", record_id=11)]
    documents = {"mi/mi-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr()

    summary = pipeline.run(ingest=True, force_reocr=True)

    assert summary["ingested_count"] == 1
    assert ocr.calls == 1


# --- Embedder Albert -------------------------------------------------------------


def test_albert_api_embedder_batches_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from assistant_rh_data_engineering.utils import gold as gold_utils

    monkeypatch.setenv("ALBERT_API_KEY", "test-key")

    calls: list[list[str]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, Any]):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int) -> FakeResponse:
        calls.append(list(json["input"]))
        data = [{"index": index, "embedding": [float(len(text))]} for index, text in reversed(list(enumerate(json["input"])))]
        return FakeResponse({"data": data})

    monkeypatch.setattr("requests.post", fake_post)

    embedder = gold_utils.AlbertApiEmbedder("BAAI/bge-m3", "embedding_m3", batch_size=2)
    vectors = embedder.embed_texts(["a", "bb", "ccc"])

    assert calls == [["a", "bb"], ["ccc"]]
    assert vectors == [[1.0], [2.0], [3.0]]


def test_build_embedders_albert_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from assistant_rh_data_engineering.utils.gold import AlbertApiEmbedder, build_embedders

    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    config = make_config(tmp_path).embeddings
    config.enable_m3 = True
    config.enable_bge_scaleway = False

    embedders = build_embedders(config)

    assert len(embedders) == 1
    assert isinstance(embedders[0], AlbertApiEmbedder)
    assert embedders[0].column_name == "embedding_m3"


# --- Conversion bureautique ------------------------------------------------------


def test_ensure_pdf_passthrough_and_unknown_extension(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.utils.convert import PdfConversionError, ensure_pdf

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-")
    assert ensure_pdf(pdf, tmp_path / "out") == pdf

    other = tmp_path / "doc.txt"
    other.write_text("x")
    with pytest.raises(PdfConversionError):
        ensure_pdf(other, tmp_path / "out")
