from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
PYTHONPATH_ENTRIES = [
    REPO_ROOT,
    REPO_ROOT / "packages/data-engineering/src",
    REPO_ROOT / "packages/shared-config/src",
]
for entry in reversed(PYTHONPATH_ENTRIES):
    entry_str = str(entry)
    if entry_str not in sys.path:
        sys.path.insert(0, entry_str)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_gold_chunks(gold_dir: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for path in sorted((gold_dir / "chunks").glob("*.chunks.jsonl")):
        chunks.extend(read_jsonl(path))
    return chunks


def normalize_for_compare(value: Any) -> Any:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def compare_common_fields(
    legacy_rows: list[dict[str, Any]],
    modern_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    legacy_by_id = {str(row["chunk_id"]): row for row in legacy_rows}
    modern_by_id = {str(row["hash_id"]): row for row in modern_rows}
    common_ids = sorted(set(legacy_by_id) & set(modern_by_id))
    common_columns = sorted(set(legacy_rows[0]) & set(modern_rows[0])) if common_ids else []

    mismatches: list[dict[str, Any]] = []
    exact_rows = 0
    for row_id in common_ids:
        legacy = legacy_by_id[row_id]
        modern = modern_by_id[row_id]
        row_diffs = [
            column
            for column in common_columns
            if normalize_for_compare(legacy.get(column))
            != normalize_for_compare(modern.get(column))
        ]
        if row_diffs:
            mismatches.append({"row_id": row_id, "columns": row_diffs})
        else:
            exact_rows += 1

    return {
        "common_id_count": len(common_ids),
        "common_columns": common_columns,
        "exact_common_rows": exact_rows,
        "exact_common_row_ratio": round(exact_rows / len(common_ids), 4) if common_ids else 1.0,
        "mismatch_count": len(mismatches),
        "mismatch_samples": mismatches[:10],
    }


def compare_per_document(
    legacy_rows: list[dict[str, Any]],
    modern_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    legacy_counts = Counter(str(row.get("number") or row.get("cid") or "") for row in legacy_rows)
    modern_counts = Counter(str(row.get("number") or row.get("cid") or "") for row in modern_rows)
    ids = sorted(set(legacy_counts) | set(modern_counts))
    deltas = [
        {
            "document_key": doc_id,
            "legacy_count": legacy_counts.get(doc_id, 0),
            "modern_count": modern_counts.get(doc_id, 0),
            "delta": modern_counts.get(doc_id, 0) - legacy_counts.get(doc_id, 0),
        }
        for doc_id in ids
        if legacy_counts.get(doc_id, 0) != modern_counts.get(doc_id, 0)
    ]
    return {
        "document_count_compared": len(ids),
        "documents_with_count_delta": len(deltas),
        "delta_samples": deltas[:10],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare les chunks gold Legifrance avec les deux schémas cibles "
            "rag_chunks_dgafp et rag_chunks_legifrance."
        )
    )
    parser.add_argument(
        "--lake-root",
        default="data/lake/legifrance",
        help="Racine locale du lake Legifrance.",
    )
    parser.add_argument(
        "--report",
        default="tests/legifrance_target_comparison_report.json",
        help="Chemin du rapport JSON de sortie.",
    )
    parser.add_argument(
        "--legacy-baseline-report",
        default="tests/legifrance_db_vs_pipeline_report.json",
        help="Rapport existant DGAFP vs pipeline pour enrichir le diagnostic.",
    )
    return parser


def main() -> int:
    from assistant_rh_data_engineering.legifrance.db import (
        LEGACY_TARGET_COLUMNS,
        MODERN_TARGET_COLUMNS,
        LegifranceDbWriter,
    )

    args = build_parser().parse_args()
    lake_root = (REPO_ROOT / args.lake_root).resolve()
    gold_chunks = load_gold_chunks(lake_root / "gold")
    if not gold_chunks:
        raise SystemExit(f"Aucun chunk gold trouvé dans {lake_root / 'gold' / 'chunks'}")

    legacy_rows = LegifranceDbWriter.project_legacy_chunks(gold_chunks)
    modern_rows = LegifranceDbWriter.project_modern_chunks(gold_chunks)

    report: dict[str, Any] = {
        "lake_root": str(lake_root),
        "gold_chunk_count": len(gold_chunks),
        "legacy_projection": {
            "row_count": len(legacy_rows),
            "expected_columns": sorted(
                column for column in LEGACY_TARGET_COLUMNS if not column.endswith("_tsv")
            ),
            "projected_columns": sorted(legacy_rows[0].keys()) if legacy_rows else [],
        },
        "modern_projection": {
            "row_count": len(modern_rows),
            "expected_columns": sorted(
                column for column in MODERN_TARGET_COLUMNS if not column.endswith("_tsv")
            ),
            "projected_columns": sorted(modern_rows[0].keys()) if modern_rows else [],
        },
        "row_id_alignment": {
            "same_total_row_count": len(legacy_rows) == len(modern_rows),
            "same_unique_id_count": len({row['chunk_id'] for row in legacy_rows})
            == len({row['hash_id'] for row in modern_rows}),
            "legacy_only_ids": sorted(
                set(str(row["chunk_id"]) for row in legacy_rows)
                - set(str(row["hash_id"]) for row in modern_rows)
            )[:10],
            "modern_only_ids": sorted(
                set(str(row["hash_id"]) for row in modern_rows)
                - set(str(row["chunk_id"]) for row in legacy_rows)
            )[:10],
        },
        "common_field_comparison": compare_common_fields(legacy_rows, modern_rows),
        "per_document_comparison": compare_per_document(legacy_rows, modern_rows),
    }

    baseline_path = (REPO_ROOT / args.legacy_baseline_report).resolve()
    if baseline_path.exists():
        baseline = read_json(baseline_path)
        report["legacy_baseline"] = {
            "report_path": str(baseline_path),
            "existing_articles": baseline.get("existing_articles"),
            "generated_articles": baseline.get("generated_articles"),
            "compared_articles": baseline.get("compared_articles"),
            "exact_article_matches": baseline.get("exact_article_matches"),
            "avg_article_similarity": baseline.get("avg_article_similarity"),
            "avg_chunk_count_delta": baseline.get("avg_chunk_count_delta"),
        }

    report_path = (REPO_ROOT / args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
