#!/usr/bin/env python3
"""Offline/read-only MATTE ingestion audit helper.

The script inspects repository state only and emits SQL statements that an
operator can run separately. It never opens a DB connection and never writes
data. See docs/MATTE_SOURCE_INGESTION_AUDIT.md for the full runbook.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

CANONICAL_TABLE = "rag_chunks_matte"
CANONICAL_EMBED_COL_ALBERT = "embedding_m3"
CANONICAL_EMBED_COL_BGE = "embedding_bge_scw"
KNOWN_EMBED_COLS = ["embedding_m3", "embedding_bge_scw", "embedding_qwen3", "embedding_ctx", "embedding_bge"]
EXPECTED_NOTEBOOKS = [
    "scripts/extract_matte.ipynb",
    "scripts/amelioration_matte.ipynb",
    "scripts/ingestion_matte.ipynb",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit offline de l'ingestion MATTE.")
    parser.add_argument("--repo-root", default=".", help="Racine du repo à inspecter.")
    parser.add_argument(
        "--sql-only",
        action="store_true",
        help="Conserve le comportement offline : émet uniquement le rapport et les requêtes SQL.",
    )
    return parser


def parse_pdf_paths_from_notebook(notebook_path: Path) -> list[str]:
    if not notebook_path.exists():
        return []
    try:
        payload = json.loads(notebook_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Impossible de parser {notebook_path}") from exc

    paths: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"Path\([\"']([^\"']+\.pdf)[\"']\)")
    for cell in payload.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source") or []
        text = "".join(source) if isinstance(source, list) else str(source)
        for match in pattern.finditer(text):
            value = match.group(1)
            if value not in seen:
                seen.add(value)
                paths.append(value)
    return paths


def inspect_expected_notebooks(repo_root: Path) -> list[dict[str, Any]]:
    findings = []
    for relative in EXPECTED_NOTEBOOKS:
        path = repo_root / relative
        findings.append(
            {
                "path": relative,
                "present": path.exists(),
                "note": "present" if path.exists() else "referenced by issue/docs but absent from origin/main",
            }
        )
    return findings


def build_sql_statements() -> list[dict[str, str]]:
    embed_cols = ", ".join(f"COUNT(*) FILTER (WHERE {col} IS NULL) AS {col}_null" for col in KNOWN_EMBED_COLS)
    return [
        {
            "name": "embedding_coverage",
            "description": "Counts NULL embeddings for every known MATTE embedding column.",
            "sql": f"SELECT COUNT(*) AS total_rows, {embed_cols} FROM {CANONICAL_TABLE};",
        },
        {
            "name": "canonical_embedding_columns",
            "description": "Lists embedding columns present on rag_chunks_matte.",
            "sql": (
                "SELECT column_name, data_type, udt_name "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                f"AND table_name = '{CANONICAL_TABLE}' "
                "AND column_name IN ('embedding_m3','embedding_bge_scw','embedding_qwen3','embedding_ctx','embedding_bge') "
                "ORDER BY column_name;"
            ),
        },
        {
            "name": "empty_text",
            "description": "Detects rows with no indexable text.",
            "sql": f"SELECT COUNT(*) FILTER (WHERE COALESCE(chunk_text, text, '') = '') AS empty_text FROM {CANONICAL_TABLE};",
        },
        {
            "name": "duplicate_hash_id",
            "description": "Detects duplicate chunk identifiers.",
            "sql": f"SELECT hash_id, COUNT(*) AS n FROM {CANONICAL_TABLE} GROUP BY hash_id HAVING COUNT(*) > 1 LIMIT 20;",
        },
        {
            "name": "indexes",
            "description": "Lists indexes and confirms whether an embedding_m3 vector index exists.",
            "sql": "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'rag_chunks_matte' ORDER BY indexname;",
        },
    ]


def build_report(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    notebooks = inspect_expected_notebooks(repo_root)
    pdf_paths = parse_pdf_paths_from_notebook(repo_root / "scripts" / "amelioration_matte.ipynb")
    missing = [item["path"] for item in notebooks if not item["present"]]
    diagnostics = []
    if missing:
        diagnostics.append("STALE_NOTEBOOKS")
    if not pdf_paths:
        diagnostics.append("NO_DECLARED_PDFS_FOUND")

    return {
        "repo_root": str(repo_root),
        "canonical_table": CANONICAL_TABLE,
        "canonical_embed_col_albert": CANONICAL_EMBED_COL_ALBERT,
        "canonical_embed_col_bge": CANONICAL_EMBED_COL_BGE,
        "notebooks": notebooks,
        "pdf_paths_declared": pdf_paths,
        "sql_statements": build_sql_statements(),
        "diagnostics": diagnostics,
        "notes": [
            "Offline audit only: no DB connection is opened.",
            "Run emitted SQL manually against an approved read-only connection.",
        ],
    }


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(Path(args.repo_root))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
