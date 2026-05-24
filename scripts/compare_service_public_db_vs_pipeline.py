from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from assistant_rh_data_engineering.service_public import ServicePublicPipeline, ServicePublicPipelineConfig
from assistant_rh_data_engineering.service_public.config import LakePaths
from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter


def chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def text_signature(text: str) -> str:
    text = (text or "").strip().lower()
    text = " ".join(text.split())
    return text[:300]


def summarize_rows(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "rows": len(rows)}
    if rows:
        columns = sorted({key for row in rows for key in row.keys()})
        out["columns"] = columns
        role_counts = Counter(str(row.get("role", "")) for row in rows)
        out["role_counts"] = dict(role_counts)
        text_lengths = [len(str(row.get("text", "") or "")) for row in rows]
        out["text_chars_total"] = int(sum(text_lengths))
        out["text_chars_mean"] = float(sum(text_lengths) / len(text_lengths))
    return out


def compare_rows(db_rows: list[dict[str, Any]], generated_rows: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "db": summarize_rows(db_rows, "db"),
        "generated": summarize_rows(generated_rows, "generated"),
    }
    db_columns = set(report["db"].get("columns", []))
    gen_columns = set(report["generated"].get("columns", []))
    report["common_columns"] = sorted(db_columns & gen_columns)
    report["db_only_columns"] = sorted(db_columns - gen_columns)
    report["generated_only_columns"] = sorted(gen_columns - db_columns)

    db_sig = Counter(text_signature(str(row.get("text", "") or "")) for row in db_rows if row.get("text"))
    gen_sig = Counter(text_signature(str(row.get("text", "") or "")) for row in generated_rows if row.get("text"))
    intersection = sum((db_sig & gen_sig).values())
    report["text_overlap"] = {
        "shared_signatures": intersection,
        "db_coverage_ratio": round(intersection / max(1, sum(db_sig.values())), 4),
        "generated_coverage_ratio": round(intersection / max(1, sum(gen_sig.values())), 4),
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only compare DB Service-Public chunks vs local XML pipeline output.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", default="rag_chunks_service_public")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fiche-id", dest="fiche_ids", action="append")
    parser.add_argument("--situation", choices=["FPE", "FPT", "FPH"])
    parser.add_argument("--lake-root", default="data/lake/service_public_compare")
    parser.add_argument(
        "--report-path",
        default="tests/service_public_db_vs_pipeline_report.json",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db = ServicePublicDbWriter(schema=args.schema)

    fiche_ids = list(args.fiche_ids or [])
    if not fiche_ids:
        fiche_ids = db.list_fiche_ids(table=args.table, id_column="short_id", limit=args.limit)
    elif args.limit:
        fiche_ids = fiche_ids[: args.limit]

    config = ServicePublicPipelineConfig(paths=LakePaths(root_dir=Path(args.lake_root)))
    config.fiche_ids = fiche_ids
    config.silver.situation_filter = args.situation
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False

    pipeline = ServicePublicPipeline(config)
    bronze_assets = pipeline.run_bronze()

    all_generated_rows: list[dict[str, Any]] = []
    generated_by_short_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bronze_batch in chunked(bronze_assets, args.batch_size):
        silver_bundles = pipeline.run_silver(bronze_batch)
        gold_bundles = pipeline.run_gold(silver_bundles)
        for bundle in gold_bundles:
            all_generated_rows.extend(bundle.chunks)
            generated_by_short_id[bundle.document["short_id"]].extend(bundle.chunks)

    db_rows = db.fetch_service_public_chunks(fiche_ids, table=args.table)
    db_by_short_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in db_rows:
        db_by_short_id[str(row.get("short_id") or "")].append(row)

    report = {
        "fiche_count": len(fiche_ids),
        "fiches": fiche_ids,
        "global": compare_rows(db_rows, all_generated_rows),
        "per_fiche": {},
    }
    for fiche_id in fiche_ids:
        report["per_fiche"][fiche_id] = compare_rows(
            db_by_short_id.get(fiche_id, []),
            generated_by_short_id.get(fiche_id, []),
        )

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), "fiche_count": len(fiche_ids)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
