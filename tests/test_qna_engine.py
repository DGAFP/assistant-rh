"""Moteur QNA (pdf_ministry/qna) — portage des notebooks legacy MATTE/MSO (#248).

Tout hors réseau. Les tests verrouillent le comportement legacy: routage par
mode, formats de chunks par corpus (Q:/R: MATTE vs Titre:/Section: MSO),
rôles Q_ONLY/QA_COMPOSITE/A_ATOMIC/TABLE, stabilité des hashes, fallback.
"""

from __future__ import annotations

from pathlib import Path

from assistant_rh_data_engineering.matte.config import QNA_ENGINE_CONFIG as MATTE_ENGINE
from assistant_rh_data_engineering.mso.config import QNA_ENGINE_CONFIG as MSO_ENGINE
from assistant_rh_data_engineering.pdf_ministry.qna.engine import (
    QnaEngineConfig,
    detect_document_mode,
    parse_document,
    parse_qna_markers_blocks,
    section_blocks_to_chunks,
)
from assistant_rh_data_engineering.pdf_ministry.qna.silver import flatten_ocr_to_text
from assistant_rh_data_engineering.utils.grist import ManifestRow
from assistant_rh_data_engineering.utils.ocr import OcrResult

FAQ_TEXT = "\n".join(
    ["FAQ Protection sociale complémentaire", ""]
    + [f"{i}. Comment fonctionne la garantie numéro {i} du dispositif santé ?\nLa garantie {i} couvre les soins courants." for i in range(1, 8)]
)

MATTE_QR_TEXT = """FICHE 3 - La rémunération des contractuels

Q: Comment est calculée la rémunération d'un agent contractuel ?
La rémunération est fixée par référence aux grilles indiciaires.
Elle tient compte de l'expérience professionnelle.

Quelle est la procédure de réévaluation ?
La réévaluation intervient au moins tous les trois ans.

Q: Le contractuel a-t-il droit au supplément familial ?
Oui, dans les mêmes conditions que les fonctionnaires.
"""

GUIDE_TEXT = """I- Dispositions générales

Le présent guide s'applique aux agents contractuels du ministère.

A. Champ d'application

Sont concernés les agents recrutés sur le fondement du CGFP, pour une durée déterminée ou indéterminée.

B. Textes de référence

Le code général de la fonction publique et ses décrets d'application.
"""


def test_detect_mode_faq() -> None:
    assert detect_document_mode(FAQ_TEXT, "PSC_FAQ Santé.pdf") == "faq"


def test_detect_mode_guide_par_defaut() -> None:
    assert detect_document_mode(GUIDE_TEXT, "guide.pdf") == "guide"


def test_matte_qna_markers_parsing() -> None:
    blocks = parse_qna_markers_blocks(MATTE_QR_TEXT, "fiche3.pdf", "remuneration")

    questions = [b.section_title for b in blocks]
    assert "Q: Comment est calculée la rémunération d'un agent contractuel ?" in questions
    assert "Quelle est la procédure de réévaluation ?" in questions
    # La section FICHE alimente le chemin de section.
    assert any("La rémunération des contractuels" in (b.parent_section_path or "") for b in blocks)
    # Sous-question (lexique SUBQ: « procédure ») rattachée à la racine.
    sub = next(b for b in blocks if "réévaluation" in b.section_title)
    root = next(b for b in blocks if "Comment est calculée" in b.section_title)
    assert sub.parent_qa_id == root.qa_id


def test_matte_routing_prefers_qna_markers() -> None:
    mode, blocks, _ = parse_document(MATTE_QR_TEXT, "fiche3.pdf", "remuneration", MATTE_ENGINE)
    assert mode == "qna_markers"
    assert blocks


def test_mso_routing_has_no_qna_markers_mode() -> None:
    # Le même texte Q:/R: passe en guide côté MSO (fidélité au routage legacy
    # MSO qui n'avait pas le parseur à marqueurs explicites).
    mode, _, _ = parse_document(MATTE_QR_TEXT, "fiche3.pdf", "remuneration", MSO_ENGINE)
    assert mode != "qna_markers"


def test_chunks_format_qr_matte() -> None:
    _, blocks, _ = parse_document(MATTE_QR_TEXT, "fiche3.pdf", "remuneration", MATTE_ENGINE)
    chunks = section_blocks_to_chunks(blocks, MATTE_ENGINE)

    roles = {c.role for c in chunks}
    assert {"Q_ONLY", "QA_COMPOSITE", "A_ATOMIC"} <= roles
    composite = next(c for c in chunks if c.role == "QA_COMPOSITE")
    assert composite.text.startswith("Q: ")
    assert "\n\nR: " in composite.text
    assert len(composite.text) <= 1500
    assert composite.chunk_index == 1
    atomic = next(c for c in chunks if c.role == "A_ATOMIC")
    assert atomic.text.startswith("Q: ")
    assert "\nR: " in atomic.text
    q_only = next(c for c in chunks if c.role == "Q_ONLY")
    assert q_only.chunk_index == 0


def test_chunks_format_titre_section_mso() -> None:
    mode, blocks, _ = parse_document(FAQ_TEXT, "PSC_FAQ.pdf", "psc", MSO_ENGINE)
    assert mode == "faq"
    chunks = section_blocks_to_chunks(blocks, MSO_ENGINE)

    q_only = next(c for c in chunks if c.role == "Q_ONLY")
    assert q_only.text.startswith("Titre: ")
    assert "Question utilisateur probable:" in q_only.text
    composite = next(c for c in chunks if c.role == "QA_COMPOSITE")
    assert "Contenu:" in composite.text
    assert len(composite.text) <= 3000
    # Ordre des index legacy: Q=0, composite=1, atomiques >= 2.
    for c in chunks:
        if c.role == "A_ATOMIC":
            assert c.chunk_index >= 2


def test_chunks_stables_et_dedupliques() -> None:
    _, blocks, _ = parse_document(MATTE_QR_TEXT, "fiche3.pdf", "remuneration", MATTE_ENGINE)
    first = section_blocks_to_chunks(blocks, MATTE_ENGINE)
    second = section_blocks_to_chunks(blocks, MATTE_ENGINE)

    def key(c):
        # Tuple équivalent au seed hash_id (propriété de utils/gold.build_chunk_row).
        return (c.source_name, c.qa_id, c.role, c.chunk_index, c.text[:256])

    assert [key(c) for c in first] == [key(c) for c in second]
    assert len({key(c) for c in first}) == len(first)


def test_fallback_un_bloc_couvre_tout() -> None:
    text = "Texte brut sans structure exploitable, mais du contenu métier réel sur les congés."
    mode, blocks, _ = parse_document(text, "note.pdf", "conges", QnaEngineConfig(modes=("faq",)))

    assert mode == "fallback"
    assert len(blocks) == 1
    assert blocks[0].answer == text


def test_flatten_ocr_to_text() -> None:
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[
            {"index": 0, "markdown": "# I- Dispositions générales\n\nDu texte **important**.\n\n![img-0.jpeg](img-0.jpeg)"},
            {"index": 1, "markdown": "| Acte | 1° | Déconcentré |\n| --- | --- | --- |\n| Avenant | 2° | CBCM |"},
        ],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "[PAGE 1]" in flat and "[PAGE 2]" in flat  # marqueurs format legacy
    assert "# " not in flat and "I- Dispositions générales" in flat  # headings aplatis
    assert "**" not in flat and "important" in flat
    assert "img-0.jpeg" not in flat  # refs d'images non annotées retirées
    assert "|" not in flat and "Acte 1° Déconcentré" in flat  # lignes de tableau dé-pipées


def test_flatten_inlines_externalized_ocr_tables() -> None:
    # mistral-ocr externalise les grands tableaux: la page ne porte que la
    # référence [tbl-0.md], le contenu vit dans page["tables"]. Sans inlining,
    # le mode table_matrix ne voit rien (régression rebuild MSO du 05/07).
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[
            {
                "index": 0,
                "markdown": "Références : Vademecum de gestion.\n\n[tbl-0.md](tbl-0.md)\n\nNotice",
                "tables": [
                    {
                        "id": "tbl-0.md",
                        "content": "\n".join(
                            ["|  Type d'actes | Entité de gestion  |   |", "| --- | --- | --- |"]
                            + [f"|  Acte de gestion numéro {i} pour un agent contractuel | {i}° | DRH-BPECO  |" for i in range(1, 7)]
                            + [f"|  Autre acte déconcentré numéro {i} | 1° | Déconcentré  |" for i in range(1, 4)]
                        ),
                    }
                ],
            }
        ],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "[tbl-0.md]" not in flat  # référence remplacée par le contenu
    assert "Type d'actes Entité de gestion" in flat  # tableau inliné et dé-pipé
    assert "Acte de gestion numéro 1 pour un agent contractuel 1° DRH-BPECO" in flat
    # Le contenu inliné suffit à router le doc en table_matrix (sinon: guide).
    from assistant_rh_data_engineering.pdf_ministry.qna.engine import detect_document_mode

    assert detect_document_mode(flat, "Liste des actes déconcentrés.pdf") == "table_matrix"


# --- Builders silver/gold sur un asset fake ---------------------------------------


def make_asset(markdown: str, cle_bucket: str = "matte/MATTE-0001_fiche3.pdf"):
    from assistant_rh_data_engineering.pdf_ministry.bronze import BronzeAsset

    row = ManifestRow(
        record_id=7,
        corpus="MATTE",
        uid="MATTE-0001",
        titre="Fiche 3 - Rémunération",
        cle_bucket=cle_bucket,
        statut="en_vigueur",
        fields={"theme": "Rémunération"},
    )
    ocr = OcrResult(provider="albert", version="mistral-ocr-2512", markdown=markdown, pages=[{"index": 0, "markdown": markdown}])
    return BronzeAsset(row=row, sha256="a" * 64, source_path=Path("/nonexistent.pdf"), ocr=ocr, ocr_from_cache=False, ocr_markdown_raw=markdown)


def test_qna_silver_and_gold_end_to_end_matte(tmp_path: Path) -> None:
    from assistant_rh_data_engineering.matte.config import IDENTITY
    from assistant_rh_data_engineering.pdf_ministry.config import EmbeddingConfig, GoldConfig
    from assistant_rh_data_engineering.pdf_ministry.qna.gold import QnaGoldBuilder
    from assistant_rh_data_engineering.pdf_ministry.qna.silver import QnaSilverBuilder

    silver = QnaSilverBuilder(IDENTITY, MATTE_ENGINE)
    document, sections = silver.build_bundle(make_asset(MATTE_QR_TEXT))

    assert document["source"] == "matte"
    assert document["short_id"] == "MATTE-0001"
    assert document["checksum"] == "a" * 64  # sha256 du FICHIER (contrat socle)
    assert document["quality_flags"]["parse_mode"] == "qna_markers"
    assert sections and all(s["metadata"]["qna"]["qa_id"] for s in sections)

    embeddings = EmbeddingConfig(enable_m3=False, enable_bge_scaleway=False)
    gold = QnaGoldBuilder(IDENTITY, embeddings, GoldConfig(table_name="rag_chunks_matte"), MATTE_ENGINE)
    chunks = gold.build_chunks(document, sections)

    assert chunks
    # AC #248: champ source correct sur TOUS les chunks (fini le hardcode).
    assert {c["source"] for c in chunks} == {"MATTE"}
    assert {c["short_id"] for c in chunks} == {"MATTE-0001"}
    assert all(c["source_document_id"] == document["doc_id"] for c in chunks)
    assert all(c["section_id"] for c in chunks)
    assert all(c["hash_id"] for c in chunks)
    rerun = gold.build_chunks(document, sections)
    assert [c["hash_id"] for c in rerun] == [c["hash_id"] for c in chunks]


def test_matte_pipeline_smoke_over_fakes(tmp_path: Path) -> None:
    """Run complet MattePipeline sur fakes: infra du socle + builders QNA."""
    from assistant_rh_data_engineering.matte import MattePipeline, MattePipelineConfig
    from assistant_rh_data_engineering.pdf_ministry.config import LakePaths

    # Fakes minimaux (mêmes contrats que ceux des tests mi/masa).
    class FakeGrist:
        def __init__(self, records):
            self.records = records
            self.writebacks = []

        def list_columns(self, table_id=None):
            return ["source_corpus", "uid", "titre_document", "cle_bucket", "abroge", "date_publication"]

        def list_records(self, table_id=None, *, filter=None):
            return self.records

        def writeback_status(self, record_id, fields, table_id=None):
            self.writebacks.append((record_id, fields))

    class FakeStore:
        def __init__(self, documents):
            self.documents = documents
            self.ocr_cache = {}

        def fetch_source_pdf(self, cle_bucket, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.documents[cle_bucket])
            return destination

        def get_cached_ocr(self, *args):
            return self.ocr_cache.get(args[-1])

        def put_pdf(self, *args):
            pass

        def put_ocr(self, *args):
            pass

        def get_cached_image_annotations(self, *args):
            return None

        def put_image_annotations(self, *args):
            pass

    class FakeOcr:
        name = "albert"
        version = "mistral-ocr-2512"

        def ocr_pdf(self, pdf_bytes, document_name="d.pdf"):
            return OcrResult(provider="albert", version="mistral-ocr-2512", markdown=MATTE_QR_TEXT, pages=[{"index": 0, "markdown": MATTE_QR_TEXT}])

    class FakeDb:
        def __init__(self):
            self.bundles = []
            self.runs = []

        def list_short_ids_with_checksum(self, source, table=None):
            return {}

        def ingest_document_bundle(self, document, sections, chunks, **kwargs):
            self.bundles.append((document, sections, chunks))
            return {"documents": 1, "sections": len(sections), "chunks_deleted": 0, "chunks": len(chunks)}

        def delete_documents_cascade(self, short_ids, table=None, *, source):
            return {"chunks": 0, "sections": 0, "documents": 0}

        def delete_chunks_not_in_short_ids(self, short_ids_to_keep, table=None):
            self.purge_keep_lists = getattr(self, "purge_keep_lists", [])
            self.purge_keep_lists.append(sorted(short_ids_to_keep))
            return 0

        def insert_ingestion_run(self, run):
            self.runs.append(run)
            return 1

    config = MattePipelineConfig(paths=LakePaths(root_dir=tmp_path / "lake"), target_env="staging")
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    config.images.enabled = False

    records = [
        {
            "id": 7,
            "fields": {
                "source_corpus": "MATTE",
                "uid": "MATTE-0001",
                "titre_document": "Fiche 3",
                "cle_bucket": "matte/MATTE-0001_fiche3.pdf",
                "abroge": "",
                "statut_ingestion": "",
            },
        }
    ]
    grist = FakeGrist(records)
    db = FakeDb()
    pipeline = MattePipeline(
        config,
        grist_client=grist,
        store=FakeStore({"matte/MATTE-0001_fiche3.pdf": b"%PDF-doc"}),
        ocr_provider=FakeOcr(),
        db_writer=db,
    )

    summary = pipeline.run(ingest=True)

    assert summary["ingested_count"] == 1
    assert summary["failed_count"] == 0
    document, sections, chunks = db.bundles[0]
    assert document["source"] == "matte"
    assert {c["source"] for c in chunks} == {"MATTE"}
    assert {c["role"] for c in chunks} >= {"Q_ONLY", "QA_COMPOSITE", "A_ATOMIC"}
    assert grist.writebacks and grist.writebacks[-1][1]["statut_ingestion"] == "ok"
    assert db.runs and db.runs[0]["ministere"] == "matte"


def test_mso_module_contract() -> None:
    import assistant_rh_data_engineering.mso as mso

    assert mso.Pipeline is mso.MsoPipeline
    assert mso.PipelineConfig().retry_zero_chunk is False
    assert mso.PipelineConfig().images.enabled is True
    assert mso.CHUNK_TABLE == "rag_chunks_mso"
    assert MSO_ENGINE.chunk_format == "titre_section"
    assert MATTE_ENGINE.chunk_format == "qr"
    assert MATTE_ENGINE.emit_table_chunks is True


def test_coverage_guard_discards_low_coverage_mode() -> None:
    # Garde-fou (audit rebuild MSO 05/07): un logigramme « process » dont le
    # parseur ne capture presque rien ne doit pas jeter le contenu — le mode
    # suivant (guide) ou le bloc fallback doit couvrir le document.
    # Comme les vrais logigrammes: l'essentiel du contenu arrive AVANT tout
    # titre d'étape — le parseur process legacy le jette (pas de current_step).
    content = "\n\n".join(
        f"paragraphe métier numéro {i} décrivant en détail une règle de gestion des agents contractuels, "
        f"avec des montants, des délais et des conditions précises applicables au cas {i}."
        for i in range(1, 15)
    )
    text = f"Le logigramme de la procédure de recrutement\n\n{content}\n\nvalidation du dossier"

    config = QnaEngineConfig(modes=("process", "guide"), min_parse_coverage=0.35)
    mode, blocks, _ = parse_document(text, "Processus - Je recrute.pdf", "", config)

    captured = sum(len(b.answer) for b in blocks)
    assert captured >= 0.35 * len(content)  # le contenu n'est plus jeté
    assert mode in {"guide", "fallback"}  # le mode process famélique a été écarté

    # Sans le garde-fou, le mode process est retenu et jette tout.
    permissive = QnaEngineConfig(modes=("process", "guide"), min_parse_coverage=0.0)
    mode_legacy, blocks_legacy, _ = parse_document(text, "Processus - Je recrute.pdf", "", permissive)
    assert mode_legacy == "process"
    assert sum(len(b.answer) for b in blocks_legacy) < 0.1 * len(content)


# --- Issue #302: structure OCR préservée, sommaires, letterhead, matrices ----


def test_flatten_translates_headings_into_structure_markers() -> None:
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[{"index": 0, "markdown": "# 1. Périmètre d'application\n\nCorps du texte.\n\n## Il doit permettre de :\n\n- premier point"}],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "(Titre) 1. Périmètre d'application" in flat
    assert "(Intertitre) Il doit permettre de :" in flat
    assert "# " not in flat


def test_guide_follows_ocr_structure_not_synthetic_headings() -> None:
    # Instruction RDR SD (6598D90CA5): l'en-tête de courrier « MINISTÈRE / DU
    # TRAVAIL / ET DES SOLIDARITÉS » et les rangées de tableau aplaties
    # (« A 80 % 20 % 60 % 40 % ») devenaient des titres de section; les vrais
    # headings markdown de l'OCR étaient détruits par l'aplatissement.
    text = "\n".join(
        [
            "[PAGE 1]",
            "MINISTÈRE",
            "DU TRAVAIL",
            "ET DES SOLIDARITÉS",
            "(Titre) 1. Périmètre d'application",
            "Le référentiel a vocation à servir de cadre de référence pour le recrutement des agents contractuels.",
            "(Intertitre) 2. Utilisation du référentiel",
            "La grille se lit par métier et par niveau d'expérience du candidat.",
            "A 80 % 20 % 60 % 40 %",
            "C 100 % 0 % 90 % 10 %",
            "(Titre) 3. Éléments et principes de rémunération",
            "Trois éléments composent la rémunération des agents contractuels.",
        ]
    )

    config = QnaEngineConfig(modes=("guide",), chunk_format="titre_section")
    mode, blocks, _ = parse_document(text, "instruction_rdr.pdf", "", config)
    paths = [b.section_path for b in blocks]

    assert mode == "guide"
    assert "1. Périmètre d'application" in paths
    # Numérotation prioritaire sur le niveau du marqueur: « 2. » (émis en ##
    # par l'OCR) est un frère de « 1. », pas son enfant.
    assert "2. Utilisation du référentiel" in paths
    assert "3. Éléments et principes de rémunération" in paths
    assert not any("ET DES SOLIDARITÉS" in p for p in paths)
    assert not any("80 %" in p for p in paths)


def test_strip_toc_handles_accents_and_short_dot_leaders() -> None:
    # Sommaire MATTE 2011: « Table des matières » (accentué) + entrées à 3
    # points (« 2.1 - Les services concernés...6 ») partaient entiers dans les
    # chunks, et les entrées devenaient des titres de section.
    text = "\n".join(
        [
            "(Titre) Instruction relative à l'aménagement et à la réduction du temps de travail",
            "(Titre) Table des matières",
            "PRÉAMBULE...4",
            "1 - CONCERTATION LOCALE...5",
            "2 - CADRE JURIDIQUE ET CHAMP D'APPLICATION...6",
            "2.1 - Les services concernés...6",
            "(Titre) PRÉAMBULE",
            "Corps du préambule décrivant le cadre général applicable aux services.",
            "(Titre) 1 - CONCERTATION LOCALE",
            "La concertation locale est organisée dans chaque service.",
        ]
    )

    config = QnaEngineConfig(modes=("guide",), chunk_format="qr")
    _, blocks, _ = parse_document(text, "instruction_2011.pdf", "", config)
    paths = [b.section_path for b in blocks]

    assert not any("..." in p for p in paths)  # aucune entrée de sommaire en titre
    assert not any("table des matières" in p.lower() for p in paths)
    assert "PRÉAMBULE" in paths
    assert "1 - CONCERTATION LOCALE" in paths


def test_flatten_removes_repeated_page_letterhead() -> None:
    # Slides MSO (B3B0F8FA68): « MINISTÈRES SOCIAUX / Liberté / Égalité … »
    # répété sur chaque slide devenait le titre de section de 35 chunks sur 61.
    pages = [
        {
            "index": i,
            "markdown": (
                "MINISTÈRES SOCIAUX\nLiberté\nÉgalité\nFraternité\nSecrétariat général\n"
                f"# Slide numéro {i}\n\nContenu propre à la slide {i}.\n\n27/01/2026\nC1 - Public"
            ),
        }
        for i in range(1, 6)
    ]
    ocr = OcrResult(provider="albert", version="v", markdown="", pages=pages)

    flat = flatten_ocr_to_text(ocr)

    assert "MINISTÈRES SOCIAUX" not in flat
    assert "Liberté" not in flat
    assert "C1 - Public" not in flat
    assert "(Titre) Slide numéro 3" in flat
    assert "Contenu propre à la slide 3." in flat


def test_flatten_serializes_checkbox_matrix_tables_with_column_names() -> None:
    # Tableaux « qui produit quoi » (B3B0F8FA68): le dé-pipage legacy perdait
    # la colonne porteuse de la coche — « Décision X ☑ ☑ » ne dit pas si c'est
    # la DRHM ou le SD qui produit l'acte depuis le 1er janvier 2026.
    table = "\n".join(
        [
            "|  Décision ou Acte | AUJOURD'HUI, PRODUIT PAR : |   | A/c du 1er janvier 2026, PRODUIT PAR :  |   |",
            "| --- | --- | --- | --- | --- |",
            "|   |  DRHM | SD | DRHM | SD  |",
            "|  **Arrêté de congés maternité/naissance/adoption** | ☑ |  |  | ☑  |",
            "|  Autorisation de télétravail |  | ☑ |  | ☑  |",
        ]
    )
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[
            {"index": 0, "markdown": "# Les décisions ou arrêtés visés\n\n[tbl-0.md](tbl-0.md)", "tables": [{"id": "tbl-0.md", "content": table}]}
        ],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "Arrêté de congés maternité/naissance/adoption — AUJOURD'HUI, PRODUIT PAR : DRHM ; A/c du 1er janvier 2026, PRODUIT PAR : SD" in flat
    assert "Autorisation de télétravail — AUJOURD'HUI, PRODUIT PAR : SD ; A/c du 1er janvier 2026, PRODUIT PAR : SD" in flat
    assert "**" not in flat
    assert "DRHM SD DRHM SD" not in flat  # le sous-en-tête ne fuit plus en ligne isolée


def test_legacy_flat_tables_unchanged_for_table_matrix_docs() -> None:
    # Le tableau des actes déconcentrés (352963BCB1) n'est PAS une matrice à
    # cases cochées: il garde le dé-pipage legacy dont dépend split_table_row.
    table = "\n".join(
        [
            "|  Type d'actes | Entité de gestion  |   |",
            "| --- | --- | --- |",
            "|  Congé maternité | 14° | Déconcentré  |",
        ]
    )
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[{"index": 0, "markdown": "[tbl-0.md](tbl-0.md)", "tables": [{"id": "tbl-0.md", "content": table}]}],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "Congé maternité 14° Déconcentré" in flat


def test_multiline_table_cells_are_joined_before_depiping() -> None:
    # La rangée-rubrique « Signature des contrats et avenants (La production…) »
    # s'étale sur deux lignes physiques dans le markdown OCR: sans recollage la
    # rubrique n'était jamais reconnue et 12 actes partaient dans la rubrique
    # précédente.
    table = "\n".join(
        [
            "|  Type d'actes | Entité de gestion  |   |",
            "| --- | --- | --- |",
            "|  Signature des contrats et avenants",
            "(La production de tout type de contrat reste de la compétence de la DRHM)  |   |   |",
            "|  Recrutement en CDI | 1° | DRH-BPECO  |",
        ]
    )
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[{"index": 0, "markdown": "[tbl-0.md](tbl-0.md)", "tables": [{"id": "tbl-0.md", "content": table}]}],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "Signature des contrats et avenants (La production de tout type de contrat reste de la compétence de la DRHM)" in flat
    assert "Recrutement en CDI 1° DRH-BPECO" in flat


def test_faq_routing_and_parsing_ignore_structure_markers() -> None:
    # Les marqueurs (Titre)/(Intertitre) ne concernent que le mode guide: un
    # doc FAQ dont les titres sont marqués route et parse comme avant.
    marked = "(Titre) FAQ Protection sociale complémentaire\n" + "\n".join(
        f"{i}. Comment fonctionne la garantie numéro {i} du dispositif santé ?\nLa garantie {i} couvre les soins courants." for i in range(1, 8)
    )

    assert detect_document_mode(marked, "PSC_FAQ Santé.pdf") == "faq"

    config = QnaEngineConfig(modes=("faq",), chunk_format="titre_section")
    mode, blocks, _ = parse_document(marked, "PSC_FAQ Santé.pdf", "", config)
    assert mode == "faq"
    assert any("garantie numéro 3" in b.section_title for b in blocks)


def test_table_cells_strip_image_refs_like_legacy_flatten() -> None:
    # Parité legacy: les refs d'images non annotées étaient strippées sur la
    # ligne entière AVANT le dé-pipage — le chemin tableau ne doit pas les
    # laisser fuir dans les chunks.
    ocr = OcrResult(
        provider="albert",
        version="v",
        markdown="",
        pages=[{"index": 0, "markdown": "| Colonne A | Colonne B |\n| --- | --- |\n| Voir schéma ![img-3.jpeg](img-3.jpeg) | Valeur |"}],
    )

    flat = flatten_ocr_to_text(ocr)

    assert "img-3.jpeg" not in flat
    assert "Voir schéma Valeur" in flat


def test_sommaire_without_dot_leaders_does_not_swallow_blank_lines() -> None:
    # Un « Sommaire » de FAQ numéroté (sans pointillés) ne doit pas laisser le
    # mode TOC actif jusqu'à la fin du document — sinon toutes les lignes
    # vides sont avalées et les paragraphes du corps se collent.
    from assistant_rh_data_engineering.pdf_ministry.qna.engine import strip_table_of_contents

    text = "Sommaire\n1. Comment ça marche ? 4\n2. Qui contacter ? 5\n\nCorps du document.\n\nParagraphe deux.\n\nParagraphe trois."

    out = strip_table_of_contents(text)

    assert "Corps du document.\n\nParagraphe deux." in out
    assert "Paragraphe deux.\n\nParagraphe trois." in out
