"""Pipeline médaillon MASA (#247): silver/gold, réconciliation et writeback.

Copie adaptée de test_mi_pipeline.py (le module masa est copié du template
MI et ses tests le suivent — ils divergeront avec le parsing). Les tests des
utils partagés (embedder Albert, conversion bureautique) ne sont pas
dupliqués ici. Tout est testé hors réseau: Grist, dropzone, OCR et Postgres
sont des fakes en mémoire; les embeddings sont désactivés en config de test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from assistant_rh_data_engineering.masa.bronze import MasaBronzeAsset
from assistant_rh_data_engineering.masa.config import (
    LakePaths,
    MasaPipelineConfig,
)
from assistant_rh_data_engineering.masa.gold import MasaGoldBuilder
from assistant_rh_data_engineering.masa.pipeline import MasaPipeline
from assistant_rh_data_engineering.masa.silver import MasaSilverBuilder
from assistant_rh_data_engineering.pdf_ministry.pipeline import plan_reconciliation
from assistant_rh_data_engineering.utils.grist import ManifestRow
from assistant_rh_data_engineering.utils.ocr import OcrResult

OCR_MARKDOWN = """## Dispositions générales

Les agents du ministère bénéficient du présent dispositif.

### Conditions d'éligibilité

Une ancienneté de deux ans est requise pour en bénéficier pleinement.

### Modalités de demande

La demande est déposée par voie hiérarchique.
"""


def make_row(uid: str = "MASA-0001", *, statut: str = "en_vigueur", record_id: int = 11, **fields: Any) -> ManifestRow:
    return ManifestRow(
        record_id=record_id,
        corpus="MASA",
        uid=uid,
        titre=fields.pop("titre", f"Circulaire {uid}"),
        cle_bucket=fields.pop("cle_bucket", f"masa/{uid.lower()}_circulaire.pdf"),
        statut=statut,
        fields=fields,
    )


def make_ocr(markdown: str = OCR_MARKDOWN) -> OcrResult:
    return OcrResult(provider="albert", version="mistral-ocr-2512", markdown=markdown, pages=[{"index": 0}])


def make_asset(row: ManifestRow, sha256: str = "a" * 64, markdown: str = OCR_MARKDOWN) -> MasaBronzeAsset:
    return MasaBronzeAsset(
        row=row,
        sha256=sha256,
        source_path=Path("/nonexistent/source.pdf"),
        ocr=make_ocr(markdown),
        ocr_from_cache=False,
    )


def make_config(tmp_path: Path) -> MasaPipelineConfig:
    config = MasaPipelineConfig(paths=LakePaths(root_dir=tmp_path / "lake"), target_env="staging")
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    # L'enrichissement d'images est testé explicitement avec un fake annotator;
    # désactivé par défaut pour ne pas exiger ALBERT_API_KEY.
    config.images.enabled = False
    # Idem re-passe vision pleine page: testée explicitement (fake reconstructor).
    config.page_vision.enabled = False
    return config


# --- plan_reconciliation (fonction pure) -------------------------------------


def test_plan_classifies_new_changed_unchanged_and_orphans() -> None:
    expected = {"MASA-0001": make_row("MASA-0001"), "MASA-0002": make_row("MASA-0002"), "MASA-0003": make_row("MASA-0003")}
    current = {
        "MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4},
        "MASA-0002": {"doc_id": "d2", "checksum": "old", "nb_chunks": 4},
        "MASA-0999": {"doc_id": "d9", "checksum": "x", "nb_chunks": 2},
    }
    checksums = {"MASA-0001": "a" * 64, "MASA-0002": "b" * 64, "MASA-0003": "c" * 64}

    plan = plan_reconciliation(expected, current, checksums)

    assert plan["ignore_inchange"] == ["MASA-0001"]
    assert plan["ingest"] == ["MASA-0002", "MASA-0003"]
    assert plan["delete"] == ["MASA-0999"]


def test_plan_zero_chunk_document_with_matching_checksum_converges() -> None:
    # Divergence vs MI: le filtre payload gold rend légitime un doc à zéro
    # chunk (image-only décoratif); ingest_document_bundle étant
    # transactionnel, un checksum en base prouve un lot complet =>
    # ignore_inchange, pas de retraitement perpétuel.
    expected = {"MASA-0001": make_row("MASA-0001")}
    current = {"MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 0}}

    plan = plan_reconciliation(
        expected,
        current,
        {"MASA-0001": "a" * 64},
        retry_zero_chunk=MasaPipelineConfig().retry_zero_chunk,
    )

    assert plan["ignore_inchange"] == ["MASA-0001"]
    assert plan["ingest"] == []


def test_plan_force_reocr_reingests_everything() -> None:
    expected = {"MASA-0001": make_row("MASA-0001")}
    current = {"MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4}}

    plan = plan_reconciliation(expected, current, {"MASA-0001": "a" * 64}, force_reocr=True)

    assert plan["ingest"] == ["MASA-0001"]
    assert plan["ignore_inchange"] == []


# --- Silver -------------------------------------------------------------------


def test_silver_builds_document_and_sections(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    builder = MasaSilverBuilder(config.silver)
    row = make_row(theme="Action sociale")

    document, sections = builder.build_bundle(make_asset(row))

    assert document["source"] == "masa"
    assert document["short_id"] == "MASA-0001"
    assert document["checksum"] == "a" * 64
    assert document["publisher"] == "Ministère de l'Agriculture et de la Souveraineté alimentaire"
    assert document["storage_path"] == row.cle_bucket
    assert document["metadata"]["theme"] == "Action sociale"
    assert document["doc_markdown"].startswith("## Circulaire MASA-0001")
    # Titre + heading OCR ## + 2 sous-sections ###
    assert len(sections) >= 3
    assert all(section["doc_id"] == document["doc_id"] for section in sections)


def test_silver_doc_ids_are_stable_across_runs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    builder = MasaSilverBuilder(config.silver)

    doc_a, _ = builder.build_bundle(make_asset(make_row()))
    doc_b, _ = builder.build_bundle(make_asset(make_row(), sha256="b" * 64))

    assert doc_a["doc_id"] == doc_b["doc_id"]


def test_silver_headingless_ocr_still_yields_a_section(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    builder = MasaSilverBuilder(config.silver)

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

    _, sections = MasaSilverBuilder(config.silver).build_bundle(make_asset(make_row(), markdown=markdown))

    headings = [section["heading"] for section in sections]
    assert "1. Dispositions communes" in headings
    assert "2. Dispositions particulières" in headings


def test_silver_strips_page_boilerplate_and_adds_page_markers(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.masa.silver import normalize_ocr_markdown

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
    from assistant_rh_data_engineering.masa.silver import normalize_ocr_markdown

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

    document, sections = MasaSilverBuilder(config.silver).build_bundle(make_asset(make_row(), markdown=markdown))

    parent = next(s for s in sections if s["heading"] == "Partie unique")
    child = next(s for s in sections if s["heading"] == "Sous-partie A")
    assert "Introduction de la partie" in parent["section_markdown"]
    assert "Contenu détaillé" not in parent["section_markdown"]
    assert "Contenu détaillé" in child["section_markdown"]

    # Et au global: le texte des chunks ne dépasse pas ~1x le document.
    chunks = MasaGoldBuilder(config.embeddings, config.gold).build_chunks(document, sections)
    total_chunk_chars = sum(len(c["text"]) for c in chunks)
    assert total_chunk_chars <= len(document["doc_markdown"]) * 1.2


# --- Gold ---------------------------------------------------------------------


def test_gold_chunks_carry_masa_source_and_stable_hash(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    document, sections = MasaSilverBuilder(config.silver).build_bundle(make_asset(make_row(theme="Mobilité")))
    builder = MasaGoldBuilder(config.embeddings, config.gold)

    chunks = builder.build_chunks(document, sections)

    assert chunks
    assert {chunk["source"] for chunk in chunks} == {"MASA"}
    assert {chunk["short_id"] for chunk in chunks} == {"MASA-0001"}
    assert all(chunk["source_document_id"] == document["doc_id"] for chunk in chunks)
    assert all(chunk["role"] == "SECTION_ATOMIC" for chunk in chunks)
    assert all(chunk["thematique"] == "Mobilité" for chunk in chunks)

    rerun = builder.build_chunks(document, sections)
    assert [chunk["hash_id"] for chunk in rerun] == [chunk["hash_id"] for chunk in chunks]


def test_gold_skips_non_indexable_sections(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    document, sections = MasaSilverBuilder(config.silver).build_bundle(make_asset(make_row()))
    for section in sections:
        section["is_indexable"] = False

    chunks = MasaGoldBuilder(config.embeddings, config.gold).build_chunks(document, sections)

    assert chunks == []


def test_gold_filters_low_payload_chunks(tmp_path: Path) -> None:
    # Divergence MASA (audit du lot réel 2026-07-04): les supports type slides
    # produisent des chunks sans contenu — images seules, titres de slide sans
    # corps, pages de garde d'annexes. Filtrés en gold par payload utile.
    config = make_config(tmp_path)
    markdown = (
        "## Support formation webinaire déconcentration\n\n"
        "![img-0.jpeg](img-0.jpeg)\n\n![img-1.jpeg](img-1.jpeg)\n\n"
        "## Liste des questions pour un entretien de recrutement structuré\n\n"
        "ANNEXE N° 7\n\n"
        "## Les différents types de contrats\n\n"
        "Le contrat à durée déterminée est conclu pour une durée maximale de trois ans, "
        "renouvelable dans la limite de six ans au total sur le même emploi."
    )
    document, sections = MasaSilverBuilder(config.silver).build_bundle(make_asset(make_row(), markdown=markdown))

    chunks = MasaGoldBuilder(config.embeddings, config.gold).build_chunks(document, sections)

    texts = [chunk["text"] for chunk in chunks]
    assert any("durée déterminée" in text for text in texts)
    for text in texts:
        assert "![img-" not in text or "durée déterminée" in text
        assert text.strip() != "ANNEXE N° 7"
    # Les chunks image-seule et heading-seul ont disparu.
    assert not any(text.strip().endswith("![img-1.jpeg](img-1.jpeg)") for text in texts)


def test_gold_large_tables_split_on_rows_with_header_repeated() -> None:
    from assistant_rh_data_engineering.masa.gold import split_section_markdown

    header = "| Axe | Objectif | Public | Durée |\n| --- | --- | --- | --- |"
    rows = "\n".join(f"| Axe {i} | Objectif de formation numéro {i}, décrit assez longuement | Tout public | {i} H |" for i in range(1, 60))
    table = f"{header}\n{rows}"

    chunks = split_section_markdown(f"### Priorités de formation\n\n{table}", 1200, 200)

    assert len(chunks) > 1
    # Chaque tranche de tableau (hors la première, qui porte le heading fusionné)
    # commence par l'en-tête de colonnes.
    for chunk in chunks[1:]:
        assert chunk.splitlines()[0] == "| Axe | Objectif | Public | Durée |"
        assert "| --- |" in chunk.splitlines()[1]
    # Aucune tranche ne coupe une ligne de tableau en deux.
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("|"):
                assert line.rstrip().endswith("|"), line


def test_gold_orphan_heading_merged_into_next_chunk() -> None:
    from assistant_rh_data_engineering.masa.gold import split_section_markdown

    header = "| Col A | Col B |\n| --- | --- |"
    rows = "\n".join(f"| valeur {i} très détaillée pour occuper de la place | autre valeur {i} |" for i in range(1, 40))
    chunks = split_section_markdown(f"## Priorités de formation 2026\n\n{header}\n{rows}", 1200, 200)

    assert chunks[0].startswith("## Priorités de formation 2026\n\n| Col A | Col B |")
    assert all(len(chunk) >= 120 for chunk in chunks)


def test_gold_page_split_table_is_stitched_and_header_detected_by_repetition() -> None:
    # mistral-ocr coupe un grand tableau à chaque page: blocs séparés par des
    # marqueurs de page, en-tête ré-émis, séparatrice au mauvais endroit.
    from assistant_rh_data_engineering.masa.gold import split_section_markdown

    header = "| Périmètre | Intitulé du stage | Public cible | Durée |"
    rows1 = "\n".join(f"| SG | Stage numéro {i} avec un intitulé assez long pour peser | Tout public | {i} H |" for i in range(1, 20))
    rows2 = "\n".join(f"| PN | Formation numéro {i} avec un intitulé assez long pour peser | OPJ | {i} H |" for i in range(1, 20))
    page1 = f"{header}\n{rows1}"
    page2 = f"{header}\n| --- | --- | --- | --- |\n{rows2}"

    chunks = split_section_markdown(f"{page1}\n\n<!-- PAGE: 2 -->\n\n{page2}", 1200, 200)

    assert len(chunks) > 1
    for chunk in chunks:
        assert "<!-- PAGE:" not in chunk
        assert chunk.splitlines()[0] == header
    # L'en-tête n'est pas dupliqué dans le corps des tranches.
    assert all(chunk.splitlines()[2:].count(header) == 0 for chunk in chunks)


def test_gold_prose_chunking_unchanged() -> None:
    from assistant_rh_data_engineering.masa.gold import split_section_markdown

    prose = "\n\n".join(f"Paragraphe {i} avec un contenu suffisant pour peser dans le découpage." for i in range(30))
    chunks = split_section_markdown(prose, 400, 100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 400 for chunk in chunks)


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
        self.purge_keep_lists: list[list[str]] = []
        self.runs: list[dict[str, Any]] = []

    def delete_chunks_not_in_short_ids(self, short_ids_to_keep: list[str], table: str | None = None) -> int:
        self.purge_keep_lists.append(sorted(short_ids_to_keep))
        return 0

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

    def ingest_document_bundle(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, int]:
        self.upserted_documents.append(document)
        self.upserted_sections.extend(sections)
        short_id = document["short_id"]
        self.replaced_chunks.append(([short_id], len(chunks)))
        self.state[short_id] = {
            "doc_id": document["doc_id"],
            "checksum": document["checksum"],
            "nb_chunks": len(chunks),
        }
        return {"documents": 1, "sections": len(sections), "chunks_deleted": 0, "chunks": len(chunks)}

    def delete_documents_cascade(self, short_ids: list[str], table: str | None = None, *, source: str) -> dict[str, int]:
        self.cascade_deletes.append(sorted(short_ids))
        chunks = sum(int(self.state.get(short_id, {}).get("nb_chunks") or 0) for short_id in short_ids)
        for short_id in short_ids:
            self.state.pop(short_id, None)
        return {"chunks": chunks, "sections": 0, "documents": len(short_ids)}

    def insert_ingestion_run(self, run: dict[str, Any]) -> int:
        self.runs.append(run)
        return 1


def grist_record(
    uid: str,
    *,
    record_id: int,
    abroge: str = "",
    cle_bucket: str | None = None,
    statut_ingestion: str = "",
) -> dict[str, Any]:
    return {
        "id": record_id,
        "fields": {
            "source_corpus": "MASA",
            "uid": uid,
            "titre_document": f"Circulaire {uid}",
            "cle_bucket": cle_bucket if cle_bucket is not None else f"masa/{uid.lower()}_circulaire.pdf",
            "abroge": abroge,
            "statut_ingestion": statut_ingestion,
        },
    }


def build_pipeline(
    tmp_path: Path,
    *,
    records: list[dict[str, Any]],
    documents: dict[str, bytes],
    state: dict[str, dict[str, Any]] | None = None,
) -> tuple[MasaPipeline, FakeGrist, FakeStore, FakeOcrProvider, FakeDbWriter]:
    grist = FakeGrist(records)
    store = FakeStore(documents)
    ocr = FakeOcrProvider()
    writer = FakeDbWriter(state)
    pipeline = MasaPipeline(
        make_config(tmp_path),
        grist_client=grist,
        store=store,
        ocr_provider=ocr,
        db_writer=writer,
    )
    return pipeline, grist, store, ocr, writer


# --- Pipeline: run complet ------------------------------------------------------


def test_run_ingests_new_documents_and_writes_back_ok(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11), grist_record("MASA-0002", record_id=12)]
    documents = {
        "masa/masa-0001_circulaire.pdf": b"%PDF-doc1",
        "masa/masa-0002_circulaire.pdf": b"%PDF-doc2",
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
    records = [grist_record("MASA-0001", record_id=11)]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    pipeline.run(ingest=True)
    assert ocr.calls == 1

    # Re-run immédiat: mêmes fichiers, état DB porté par le FakeDbWriter.
    pipeline2 = MasaPipeline(
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
    assert grist.status_for(11) == ["ok", "ok"]  # statut consolidé (détail dans la trace de run)


def test_removed_manifest_row_triggers_cascade_delete(tmp_path: Path) -> None:
    state = {"MASA-0009": {"doc_id": "d9", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MASA-0001", record_id=11)]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents, state=state)

    summary = pipeline.run(ingest=True)

    assert summary["deleted_count"] == 1
    assert writer.cascade_deletes == [["MASA-0009"]]
    assert summary["details"]["MASA-0009"]["statut"] == "supprime"


def test_abrogated_row_is_deleted_and_written_back(tmp_path: Path) -> None:
    state = {"MASA-0001": {"doc_id": "d1", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MASA-0001", record_id=11, abroge="oui")]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={}, state=state)

    summary = pipeline.run(ingest=True)

    assert writer.cascade_deletes == [["MASA-0001"]]
    assert grist.status_for(11) == ["supprime"]
    assert summary["deleted_count"] == 1


def test_rejected_manifest_row_is_written_back_and_run_continues(tmp_path: Path) -> None:
    records = [
        grist_record("MASA-0001", record_id=11),
        grist_record("MASA-0002", record_id=12, cle_bucket=""),  # rejetée: cle_bucket vide
    ]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert summary["rejected_count"] == 1
    assert grist.status_for(12) == ["erreur"]


def test_document_failure_writes_back_erreur_and_run_continues(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11), grist_record("MASA-0002", record_id=12)]
    # MASA-0002 absent de la dropzone => échec au téléchargement.
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert summary["failed_count"] == 1
    assert grist.status_for(11) == ["ok"]
    assert grist.status_for(12) == ["erreur"]


def test_dry_run_makes_no_writes(tmp_path: Path) -> None:
    state = {"MASA-0009": {"doc_id": "d9", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MASA-0001", record_id=11)]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents, state=state)

    summary = pipeline.run(ingest=True, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["plan"]["ingest"] == ["MASA-0001"]
    assert summary["plan"]["delete"] == ["MASA-0009"]
    assert ocr.calls == 0
    assert grist.writebacks == []
    assert writer.upserted_documents == []
    assert writer.cascade_deletes == []
    assert writer.runs == []


def test_doc_id_filter_disables_orphan_deletion(tmp_path: Path) -> None:
    state = {"MASA-0009": {"doc_id": "d9", "checksum": "z" * 64, "nb_chunks": 3}}
    records = [grist_record("MASA-0001", record_id=11), grist_record("MASA-0002", record_id=12)]
    documents = {
        "masa/masa-0001_circulaire.pdf": b"%PDF-doc1",
        "masa/masa-0002_circulaire.pdf": b"%PDF-doc2",
    }
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents, state=state)

    summary = pipeline.run(ingest=True, doc_ids=["masa-0001"])

    assert summary["ingested_count"] == 1
    assert summary["deleted_count"] == 0
    assert writer.cascade_deletes == []


def test_skip_grist_writeback(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11)]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True, skip_grist_writeback=True)

    assert summary["ingested_count"] == 1
    assert grist.writebacks == []


def test_ocr_cache_hit_skips_provider_call(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11)]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr()

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert ocr.calls == 0
    assert summary["details"]["MASA-0001"]["ocr_from_cache"] is True


def test_force_reocr_bypasses_cache_and_delta(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11)]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr()

    summary = pipeline.run(ingest=True, force_reocr=True)

    assert summary["ingested_count"] == 1
    assert ocr.calls == 1


# --- Régressions revue de code (2026-07-04) --------------------------------------


def test_transient_download_failure_never_deletes_existing_document(tmp_path: Path) -> None:
    # P1: un incident S3 transitoire sur un doc déjà ingéré ne doit JAMAIS le
    # classer orphelin (suppression cascade d'un document sain).
    state = {"MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4}}
    records = [grist_record("MASA-0001", record_id=11)]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={}, state=state)

    summary = pipeline.run(ingest=True)

    assert summary["failed_count"] == 1
    assert summary["deleted_count"] == 0
    assert writer.cascade_deletes == []
    assert "MASA-0001" in writer.state
    assert summary["details"]["MASA-0001"]["statut"] == "erreur"


def test_doc_id_filter_still_deletes_targeted_abrogated_document(tmp_path: Path) -> None:
    state = {
        "MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4},
        "MASA-0002": {"doc_id": "d2", "checksum": "b" * 64, "nb_chunks": 4},
    }
    records = [grist_record("MASA-0001", record_id=11, abroge="oui"), grist_record("MASA-0002", record_id=12)]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={}, state=state)

    summary = pipeline.run(ingest=True, doc_ids=["MASA-0001"])

    assert writer.cascade_deletes == [["MASA-0001"]]
    assert "MASA-0002" in writer.state  # hors filtre: intouché
    assert grist.status_for(11) == ["supprime"]
    assert summary["deleted_count"] == 1


def test_dry_run_reports_failed_count(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11)]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={})

    summary = pipeline.run(ingest=True, dry_run=True)

    assert summary["failed_count"] == 1  # dropzone vide => échec de téléchargement visible


def test_gold_distinct_adjacent_tables_are_not_stitched() -> None:
    from assistant_rh_data_engineering.masa.gold import split_section_markdown

    table_a = "| Col A | Col B |\n| --- | --- |\n" + "\n".join(
        f"| valeur {i} assez longue pour peser dans la découpe du bloc | détail {i} |" for i in range(30)
    )
    table_b = "| X | Y | Z |\n| --- | --- | --- |\n" + "\n".join(
        f"| a{i} | b{i} avec du contenu supplémentaire pour la taille | c{i} |" for i in range(30)
    )

    chunks = split_section_markdown(f"{table_a}\n\n{table_b}", 1200, 200)

    # Aucune tranche du second tableau ne doit porter l'en-tête du premier.
    for chunk in chunks:
        if "| a1 |" in chunk or "| c5 |" in chunk:
            assert not chunk.startswith("| Col A | Col B |")


def test_gold_repeated_data_row_is_not_chosen_as_header() -> None:
    from assistant_rh_data_engineering.masa.gold import _table_header_and_body

    rows = ["| N/A | 12 H |", "| Périmètre | Durée |", "| N/A | 12 H |"] + [f"| stage {i} | {i} H |" for i in range(40)]
    header, body = _table_header_and_body("\n".join(rows))

    # « | N/A | 12 H | » est répétée mais numérique: pas un en-tête.
    assert header == [] or "N/A" not in header[0]


# --- Colonne de statut unique (opérateurs + jobs, 2026-07-04) --------------------


def test_operator_a_supprimer_triggers_cascade_delete(tmp_path: Path) -> None:
    state = {"MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4}}
    records = [grist_record("MASA-0001", record_id=11, statut_ingestion="a_supprimer")]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={}, state=state)

    summary = pipeline.run(ingest=True)

    assert writer.cascade_deletes == [["MASA-0001"]]
    assert grist.status_for(11) == ["supprime"]
    assert summary["deleted_count"] == 1


def test_operator_a_supprimer_on_never_ingested_row_is_acknowledged(tmp_path: Path) -> None:
    records = [grist_record("MASA-0001", record_id=11, statut_ingestion="a_supprimer")]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={})

    summary = pipeline.run(ingest=True)

    assert writer.cascade_deletes == []  # rien en base
    assert grist.status_for(11) == ["supprime"]
    assert summary["details"]["MASA-0001"]["statut"] == "supprime"


def test_supprime_row_is_neither_reingested_nor_repatched(tmp_path: Path) -> None:
    # Ligne supprimée au run précédent: inactive tant que l'opérateur ne vide
    # pas la cellule — pas de ré-ingestion, pas de re-PATCH à chaque run.
    records = [grist_record("MASA-0001", record_id=11, statut_ingestion="supprime")]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 0
    assert ocr.calls == 0
    assert grist.writebacks == []
    assert writer.upserted_documents == []


def test_cleared_statut_reactivates_the_row(tmp_path: Path) -> None:
    # Ré-activation: l'opérateur vide la cellule => la ligne redevient à ingérer.
    records = [grist_record("MASA-0001", record_id=11, statut_ingestion="")]
    documents = {"masa/masa-0001_circulaire.pdf": b"%PDF-doc1"}
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents=documents)

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert grist.status_for(11) == ["ok"]


def test_acknowledgment_requires_ingest_mode(tmp_path: Path) -> None:
    # Sans --ingest (pas d'accès base), aucun acquittement « supprime »:
    # l'état réel du corpus n'a pas été consulté.
    records = [grist_record("MASA-0001", record_id=11, statut_ingestion="a_supprimer")]
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=records, documents={})

    pipeline.run(ingest=False, skip_grist_writeback=False)

    assert grist.writebacks == []


def test_rejected_row_never_deletes_its_ingested_document(tmp_path: Path) -> None:
    # Une faute de saisie (titre vidé) rejette la ligne — le document déjà
    # ingéré ne doit PAS devenir orphelin et être supprimé en cascade.
    state = {"MASA-0001": {"doc_id": "d1", "checksum": "a" * 64, "nb_chunks": 4}}
    record = grist_record("MASA-0001", record_id=11)
    record["fields"]["titre_document"] = ""
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=[record], documents={}, state=state)

    summary = pipeline.run(ingest=True)

    assert summary["rejected_count"] == 1
    assert summary["deleted_count"] == 0
    assert writer.cascade_deletes == []
    assert "MASA-0001" in writer.state
    assert grist.status_for(11) == ["erreur"]


def test_rejected_row_keeps_operator_inactive_status(tmp_path: Path) -> None:
    # Colonne partagée: le rejet n'écrase jamais a_supprimer/supprime.
    record = grist_record("MASA-0001", record_id=11, statut_ingestion="a_supprimer")
    record["fields"]["titre_document"] = ""
    pipeline, grist, store, ocr, writer = build_pipeline(tmp_path, records=[record], documents={})

    summary = pipeline.run(ingest=True)

    assert summary["rejected_count"] == 1
    assert grist.writebacks == []  # statut opérateur préservé


# --- Enrichissement des images OCR (divergence MASA, 2026-07-04) ------------------


class FakeAnnotator:
    """Annotateur vision en mémoire: informative pour img-1, decorative sinon."""

    name = "albert"
    version = "fake-vlm"

    def __init__(self) -> None:
        self.calls = 0

    def annotate(self, image_data_url: str) -> dict[str, str]:
        self.calls += 1
        if "AAAA" in image_data_url:  # marqueur du crop informatif
            return {"type_image": "informative", "description": "Écran RenoiRH, menu Contrat, champ Nature."}
        return {"type_image": "decorative", "description": ""}


class FakeAnnotationStore(FakeStore):
    def __init__(self, documents: dict[str, bytes]):
        super().__init__(documents)
        self.annotation_cache: dict[str, dict[str, dict[str, str]]] = {}

    def get_cached_image_annotations(self, target_env, ministere, name, version, sha256):
        return self.annotation_cache.get(sha256)

    def put_image_annotations(self, target_env, ministere, name, version, sha256, annotations):
        self.annotation_cache[sha256] = annotations


def make_ocr_with_images() -> OcrResult:
    markdown = (
        "## Formation SGCD\n\n![img-1.jpeg](img-1.jpeg)\n\nCorps du support de formation.\n\n![img-2.jpeg](img-2.jpeg)\n\n![img-9.jpeg](img-9.jpeg)"
    )
    pages = [
        {
            "index": 0,
            "markdown": markdown,
            "images": [
                {"id": "img-1.jpeg", "image_base64": "AAAA"},
                {"id": "img-2.jpeg", "image_base64": "BBBB"},
                # img-9: référencée dans le markdown mais sans base64 (non annotable).
                {"id": "img-9.jpeg", "image_base64": None},
            ],
        }
    ]
    return OcrResult(provider="albert", version="mistral-ocr-2512-img", markdown=markdown, pages=pages)


def test_ocr_provider_include_images_uses_dedicated_cache_version(monkeypatch) -> None:
    from assistant_rh_data_engineering.utils.ocr import AlbertOcrProvider

    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    plain = AlbertOcrProvider()
    with_images = AlbertOcrProvider(include_images=True)

    # Namespace de cache distinct: un cache rempli sans crops n'empêche
    # jamais l'enrichissement des documents déjà OCRisés.
    assert plain.version == "mistral-ocr-2512"
    assert with_images.version == "mistral-ocr-2512-img"


def test_parse_annotation_tolerates_json_fences() -> None:
    from assistant_rh_data_engineering.utils.image_annotation import _parse_annotation

    parsed = _parse_annotation('```json\n{"type_image": "informative", "description": "Écran RenoiRH."}\n```')

    assert parsed == {"type_image": "informative", "description": "Écran RenoiRH."}


def test_apply_image_annotations_replaces_informative_and_strips_decorative() -> None:
    from assistant_rh_data_engineering.utils.image_annotation import apply_image_annotations

    markdown = "Avant.\n\n![fig](img-1.jpeg)\n\n![deco](img-2.jpeg)\n\n![inconnue](img-9.jpeg)\n\nAprès."
    annotations = {
        "img-1.jpeg": {"type_image": "informative", "description": "Écran RenoiRH, menu Contrat."},
        "img-2.jpeg": {"type_image": "decorative", "description": ""},
    }

    result = apply_image_annotations(markdown, annotations)

    assert "[Illustration — Écran RenoiRH, menu Contrat.]" in result
    assert "img-2.jpeg" not in result
    # Non annotée (échec VLM ou hors budget): la référence est conservée.
    assert "![inconnue](img-9.jpeg)" in result


def test_apply_image_annotations_keeps_informative_without_description() -> None:
    # Réponse VLM incomplète (description omise sur une image informative):
    # on ne supprime jamais une image porteuse d'information sans description
    # de remplacement — la référence reste, comme pour une non-annotée.
    from assistant_rh_data_engineering.utils.image_annotation import apply_image_annotations

    markdown = "![capture](img-7.jpeg)"
    annotations = {"img-7.jpeg": {"type_image": "informative", "description": ""}}

    assert apply_image_annotations(markdown, annotations) == "![capture](img-7.jpeg)"


def test_annotator_version_depends_on_prompt(monkeypatch) -> None:
    # Le prompt entre dans la clé du cache bronze: le changer invalide les
    # annotations existantes (même piège que include_images pour l'OCR).
    from assistant_rh_data_engineering.utils import image_annotation as ia

    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    version_before = ia.AlbertImageAnnotator(model="openweight-medium").version
    monkeypatch.setattr(ia, "ANNOTATION_PROMPT", ia.ANNOTATION_PROMPT + " Autre consigne.")
    version_after = ia.AlbertImageAnnotator(model="openweight-medium").version

    assert version_before.startswith("openweight-medium-p")
    assert version_before != version_after


def test_bronze_annotates_images_and_caches_results(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.masa.bronze import MasaBronzeFetcher, MasaBronzeRepository

    row = make_row()
    store = FakeAnnotationStore({row.cle_bucket: b"%PDF-doc1"})
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr_with_images()
    annotator = FakeAnnotator()
    fetcher = MasaBronzeFetcher(
        store,
        FakeOcrProvider(),
        MasaBronzeRepository(tmp_path / "bronze"),
        target_env="staging",
        image_annotator=annotator,
    )

    asset = fetcher.fetch_asset(row, tmp_path / "src.pdf", sha)

    # img-9 n'a pas de base64: seuls img-1 et img-2 sont annotés.
    assert annotator.calls == 2
    assert asset.image_annotations["img-1.jpeg"]["type_image"] == "informative"
    assert asset.annotations_from_cache is False
    assert "[Illustration — Écran RenoiRH, menu Contrat, champ Nature.]" in asset.ocr.markdown
    assert "img-2.jpeg" not in asset.ocr.markdown
    assert "![img-9.jpeg](img-9.jpeg)" in asset.ocr.markdown
    # Les pages sont enrichies aussi (le silver sectionne par page).
    assert "[Illustration — " in asset.ocr.pages[0]["markdown"]

    # Re-run: annotations servies par le cache bronze, zéro appel VLM.
    asset2 = fetcher.fetch_asset(row, tmp_path / "src.pdf", sha)
    assert annotator.calls == 2
    assert asset2.annotations_from_cache is True
    assert asset2.image_annotations == asset.image_annotations


def test_bronze_annotation_failure_keeps_reference_and_run_continues(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.masa.bronze import MasaBronzeFetcher, MasaBronzeRepository

    class FailingAnnotator:
        name = "albert"
        version = "fake-vlm"

        def annotate(self, image_data_url: str) -> dict[str, str]:
            from assistant_rh_data_engineering.utils.image_annotation import ImageAnnotationError

            raise ImageAnnotationError("boom")

    row = make_row()
    store = FakeAnnotationStore({row.cle_bucket: b"%PDF-doc1"})
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr_with_images()
    fetcher = MasaBronzeFetcher(
        store,
        FakeOcrProvider(),
        MasaBronzeRepository(tmp_path / "bronze"),
        target_env="staging",
        image_annotator=FailingAnnotator(),
    )

    asset = fetcher.fetch_asset(row, tmp_path / "src.pdf", sha)

    # Aucune annotation: le markdown garde ses références telles quelles,
    # et le lot en échec n'est PAS mis en cache (retentative au prochain run
    # — une panne transitoire du VLM ne doit jamais être gelée).
    assert asset.image_annotations == {}
    assert "![img-1.jpeg](img-1.jpeg)" in asset.ocr.markdown
    assert store.annotation_cache == {}


def test_bronze_force_reocr_bypasses_annotation_cache(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.masa.bronze import MasaBronzeFetcher, MasaBronzeRepository

    class ImageOcrProvider(FakeOcrProvider):
        def ocr_pdf(self, pdf_bytes: bytes, document_name: str = "document.pdf") -> OcrResult:
            self.calls += 1
            return make_ocr_with_images()

    row = make_row()
    store = FakeAnnotationStore({row.cle_bucket: b"%PDF-doc1"})
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    # Cache pré-rempli avec des annotations périmées: force_reocr doit les ignorer.
    store.annotation_cache[sha] = {"img-1.jpeg": {"type_image": "decorative", "description": ""}}
    annotator = FakeAnnotator()
    fetcher = MasaBronzeFetcher(
        store,
        ImageOcrProvider(),
        MasaBronzeRepository(tmp_path / "bronze"),
        target_env="staging",
        force_reocr=True,
        image_annotator=annotator,
    )
    source = tmp_path / "src.pdf"
    source.write_bytes(b"%PDF-doc1")

    asset = fetcher.fetch_asset(row, source, sha)

    assert annotator.calls == 2  # ré-annotation malgré le cache
    assert asset.annotations_from_cache is False
    assert asset.image_annotations["img-1.jpeg"]["type_image"] == "informative"


def test_bronze_keeps_raw_markdown_for_silver(tmp_path: Path) -> None:
    # doc_markdown_raw (silver) et bronze/ocr/{short_id}.md portent la sortie
    # OCR brute — les substitutions VLM ne vivent que dans doc_markdown.
    from assistant_rh_data_engineering.masa.bronze import MasaBronzeFetcher, MasaBronzeRepository

    row = make_row()
    store = FakeAnnotationStore({row.cle_bucket: b"%PDF-doc1"})
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr_with_images()
    repository = MasaBronzeRepository(tmp_path / "bronze")
    fetcher = MasaBronzeFetcher(
        store,
        FakeOcrProvider(),
        repository,
        target_env="staging",
        image_annotator=FakeAnnotator(),
    )

    asset = fetcher.fetch_asset(row, tmp_path / "src.pdf", sha)

    assert "![img-1.jpeg](img-1.jpeg)" in asset.ocr_markdown_raw
    assert "[Illustration — " not in asset.ocr_markdown_raw
    saved = (repository.ocr_dir / f"{row.short_id}.md").read_text(encoding="utf-8")
    assert saved == asset.ocr_markdown_raw


# --- Re-passe vision pleine page (reconstruction de schémas OCR aplatis) ------


class FakePageVisionStore(FakeStore):
    def __init__(self, documents: dict[str, bytes]):
        super().__init__(documents)
        self.page_vision_cache: dict[str, dict[int, str]] = {}

    def get_cached_page_reconstructions(self, target_env, ministere, name, version, sha256):
        return self.page_vision_cache.get(sha256)

    def put_page_reconstructions(self, target_env, ministere, name, version, sha256, reconstructions):
        self.page_vision_cache[sha256] = reconstructions


class FakePageReconstructor:
    name = "albert-page-vision"
    version = "fake-pv-d150"
    dpi = 150

    def __init__(self) -> None:
        self.calls = 0

    def reconstruct(self, image_png: bytes) -> tuple[str, bool]:
        self.calls += 1
        # Reconstruction fidèle (reprend les 3 libellés OCR + la colonne
        # CONTRAT/AVENANT) -> passe le garde-fou de recouvrement.
        return (
            "- Changement de catégorie → CONTRAT\n- Changement d'indice → AVENANT\n- Modification du fondement juridique → CONTRAT",
            False,
        )


def make_ocr_with_risk_page() -> OcrResult:
    # Slide 57 telle que l'OCR l'aplatit: liste à puces sous un titre « OU »,
    # colonne droite CONTRAT/AVENANT perdue.
    slide = "3 MODIFICATION D'UN CONTRAT OU AVENANT\n\n- Changement de catégorie\n- Changement d'indice\n- Modification du fondement juridique\n"
    pages = [{"index": 0, "markdown": slide}]
    return OcrResult(provider="albert", version="mistral-ocr-2512", markdown=slide, pages=pages)


def test_bronze_page_vision_reconstructs_and_caches(tmp_path: Path, monkeypatch) -> None:
    from assistant_rh_data_engineering.masa.bronze import MasaBronzeFetcher, MasaBronzeRepository
    from assistant_rh_data_engineering.utils import page_vision as pv

    # Rendu PDF monkeypatché: un vrai PDF n'est pas nécessaire pour ce test.
    monkeypatch.setattr(pv, "render_pdf_pages", lambda pdf_bytes, indexes, *, dpi=150: {i: b"png" for i in indexes})

    row = make_row()
    store = FakePageVisionStore({row.cle_bucket: b"%PDF-doc1"})
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr_with_risk_page()
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-doc1")
    reconstructor = FakePageReconstructor()
    fetcher = MasaBronzeFetcher(
        store,
        FakeOcrProvider(),
        MasaBronzeRepository(tmp_path / "bronze"),
        target_env="staging",
        page_reconstructor=reconstructor,
    )

    asset = fetcher.fetch_asset(row, src, sha)

    # La mapping CONTRAT/AVENANT est restaurée dans le markdown servi au silver.
    assert "→ CONTRAT" in asset.ocr.markdown
    assert asset.ocr.pages[0].get("page_vision") is True
    assert reconstructor.calls == 1
    assert "→ CONTRAT" in asset.page_reconstructions[0]
    assert asset.page_vision_from_cache is False
    # Cache écrit; le brut OCR reste intact (mapping absente, comme l'OCR d'origine).
    assert sha in store.page_vision_cache
    assert "→ CONTRAT" not in asset.ocr_markdown_raw


def test_bronze_page_vision_uses_cache_without_recomputing(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.masa.bronze import MasaBronzeFetcher, MasaBronzeRepository

    row = make_row()
    store = FakePageVisionStore({row.cle_bucket: b"%PDF-doc1"})
    sha = hashlib.sha256(b"%PDF-doc1").hexdigest()
    store.ocr_cache[sha] = make_ocr_with_risk_page()
    # Cache pré-rempli: la reconstruction ne doit pas être recalculée (ni rendu ni VLM).
    store.page_vision_cache[sha] = {0: "- Changement de catégorie → CONTRAT"}

    class ExplodingReconstructor(FakePageReconstructor):
        def reconstruct(self, image_png: bytes) -> str:
            raise AssertionError("le VLM ne doit pas être appelé sur un cache-hit")

    fetcher = MasaBronzeFetcher(
        store,
        FakeOcrProvider(),
        MasaBronzeRepository(tmp_path / "bronze"),
        target_env="staging",
        page_reconstructor=ExplodingReconstructor(),
    )

    asset = fetcher.fetch_asset(row, tmp_path / "src.pdf", sha)

    assert asset.page_vision_from_cache is True
    assert "→ CONTRAT" in asset.ocr.markdown
    assert asset.ocr.pages[0].get("page_vision") is True
