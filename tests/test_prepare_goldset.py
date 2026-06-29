from __future__ import annotations
# ruff: noqa: E402,I001

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.goldset.prepare import (  # noqa: E402
    GoldsetResolver,
    SourceLink,
    apply_extra_tags,
    classify_source_label,
    parse_raw_rows,
    prepare_rows,
    read_csv_rows,
    split_source_labels,
    validate_enriched_rows,
    write_outputs,
)


FIXTURE = Path(__file__).parent / "fixtures" / "goldsets" / "priority_contractuels_sample.csv"


class FakeResolver(GoldsetResolver):
    def __init__(self, rows_by_kind: dict[str, list[dict[str, Any]]]):
        self.dsn = "postgresql://example.invalid/db"
        self.rows_by_kind = rows_by_kind

    def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if "FROM public.rag_documents" in sql:
            return self.rows_by_kind.get("documents", [])
        if "FROM public.rag_sections" in sql:
            return self.rows_by_kind.get("sections", [])
        if "FROM public.rag_chunks_dgafp" in sql:
            return self.rows_by_kind.get("legal", [])
        if "FROM public.rag_chunks_" in sql:
            return self.rows_by_kind.get("chunks", [])
        return []


def test_parse_raw_french_columns_and_normalizes_keywords() -> None:
    rows = parse_raw_rows(read_csv_rows(FIXTURE), goldset_name="priority_contractuels_v1")

    assert rows[0].question.startswith("Comment est fixée")
    assert rows[0].theme == "Rémunération"
    assert rows[0].ministere == "MATTE"
    assert rows[0].keywords == ["indice", "RIFSEEP", "salaire"]
    assert rows[0].goldset_name == "priority_contractuels_v1"


def test_split_source_labels_handles_matte_sp_legal_and_mso() -> None:
    labels = split_source_labels(
        "Fiche MATTE : Fiche 6 La fin de contrat - Juillet 2024 "
        "Article L-711-3 du Code général de la fonction publique "
        "Fiche SP : Fin d'un contrat à durée déterminée (CDD) "
        "Fiche MSO : Annexe 4 - Vademecum de gestion"
    )

    assert labels == [
        "Fiche MATTE : Fiche 6 La fin de contrat - Juillet 2024",
        "Article L-711-3 du Code général de la fonction publique",
        "Fiche SP : Fin d'un contrat à durée déterminée (CDD)",
        "Fiche MSO : Annexe 4 - Vademecum de gestion",
    ]
    assert classify_source_label(labels[0], "MATTE") == "matte"
    assert classify_source_label(labels[1], "MATTE") == "legal"
    assert classify_source_label(labels[2], "MATTE") == "service_public"
    assert classify_source_label(labels[3], "MSO") == "mso"


def test_resolver_scores_exact_document_and_attaches_section_and_chunk() -> None:
    raw = parse_raw_rows(read_csv_rows(FIXTURE), goldset_name="priority_contractuels_v1")[0]
    resolver = FakeResolver(
        {
            "documents": [
                {
                    "doc_id": "doc-1",
                    "short_id": "F3",
                    "title": "Fiche 3 Calcul de la rémunération et de son évolution - Juillet 2024",
                    "full_title": "",
                    "publisher": "MATTE",
                    "source_url": "",
                }
            ],
            "sections": [
                {
                    "section_id": "section-1",
                    "heading": "Base de calcul",
                    "heading_path": "Rémunération > Base de calcul",
                    "section_text": "indice de traitement RIFSEEP salaire",
                }
            ],
            "chunks": [
                {
                    "chunk_table": "rag_chunks_matte",
                    "chunk_id": "chunk-1",
                    "section_id": "section-1",
                }
            ],
        }
    )

    link = resolver.resolve_source("Fiche MATTE : Fiche 3 Calcul de la rémunération et de son évolution - Juillet 2024", raw)[0]

    assert link.status == "resolved"
    assert link.doc_short_id == "F3"
    assert link.section_id == "section-1"
    assert link.chunk_id == "chunk-1"


def test_resolver_marks_ambiguous_and_unresolved_candidates() -> None:
    raw = parse_raw_rows(read_csv_rows(FIXTURE), goldset_name="priority_contractuels_v1")[0]
    ambiguous = FakeResolver(
        {
            "documents": [
                {"doc_id": "doc-1", "short_id": "F1", "title": "Fiche contrat", "full_title": "", "publisher": "MATTE"},
                {"doc_id": "doc-2", "short_id": "F2", "title": "Fiche contrat", "full_title": "", "publisher": "MATTE"},
            ]
        }
    )
    links = ambiguous.resolve_source("Fiche MATTE : Fiche contrat", raw)

    assert links[0].status == "ambiguous"
    assert "Ambiguous document candidates" in links[0].warning

    unresolved = FakeResolver({})
    links = unresolved.resolve_source("Fiche MATTE : Source inconnue", raw)

    assert links == [
        SourceLink(
            source_label="Fiche MATTE : Source inconnue",
            source_kind="matte",
            status="unresolved",
            warning="No matching rag_documents or source_name candidate found.",
        )
    ]


def test_enriched_csv_validation_and_json_list_serialization(tmp_path: Path) -> None:
    raw_rows = parse_raw_rows(read_csv_rows(FIXTURE), goldset_name="priority_contractuels_v1")
    raw_rows = apply_extra_tags(raw_rows, ["iteration2"])
    prepared = prepare_rows(raw_rows, resolver=FakeResolver({}))
    enriched_path, _ = write_outputs(prepared, output_dir=tmp_path, goldset_name="priority_contractuels_v1")

    with enriched_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert validate_enriched_rows(rows) == []
    first = rows[0]
    assert isinstance(json.loads(first["source_labels"]), list)
    assert isinstance(json.loads(first["gold_sources"]), list)
    assert isinstance(json.loads(first["gold_chunk_ids"]), list)
    assert "iteration2" in json.loads(first["tags"])
    assert first["link_status"] == "unresolved"


def test_prepare_goldset_cli_smoke_without_db(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/prepare_goldset.py",
            "--input",
            str(FIXTURE),
            "--goldset-name",
            "priority_contractuels_v1",
            "--output-dir",
            str(tmp_path),
            "--skip-db",
            "--allow-unresolved",
            "--extra-tag",
            "iteration2",
        ],
        cwd=Path(__file__).parent.parent,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "priority_contractuels_v1.enriched.csv").exists()
    assert (tmp_path / "priority_contractuels_v1.source_links.csv").exists()
