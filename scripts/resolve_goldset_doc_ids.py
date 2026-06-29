#!/usr/bin/env python3
"""Pre-resolve goldset ``gold_sources`` to corpus ``doc_id``s.

Goldsets reference sources by human-facing labels (Service-Public fiche codes,
MATTE document names, annex codes, Légifrance article codes/ranges) that do not
equal the ``doc_id``s the retriever returns, so deterministic retrieval overlap
under-counts. This resolves each row's ``gold_sources`` once and stores the result
in ``goldset_questions_v2.gold_doc_ids`` (added if missing); the eval runner then
uses that column for deterministic matching.

Usage:
    uv run python scripts/resolve_goldset_doc_ids.py --dsn-env SCW_POSTGRES_DSN_STAGING
    uv run python scripts/resolve_goldset_doc_ids.py --dsn-env SCW_POSTGRES_DSN_STAGING --tag baseline_v1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.goldset.eval import load_gold_id_maps, parse_text_list, resolve_dsn, resolve_gold_doc_ids  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--tag", default=None, help="Limit to rows carrying this tag (default: all rows with gold_sources).")
    parser.add_argument("--dry-run", action="store_true", help="Report resolution without writing.")
    args = parser.parse_args(argv)

    dsn = resolve_dsn(args.dsn, args.dsn_env)
    maps = load_gold_id_maps(dsn)
    print(f"corpus maps: doc_short={len(maps['doc_short'])} matte_short={len(maps['matte_short'])} article={len(maps['article'])}")

    where = ["gold_sources IS NOT NULL", "btrim(gold_sources) <> ''"]
    params: list[object] = []
    if args.tag:
        where.append("tags @> %s::text[]")
        params.append([args.tag])

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        conn.execute("ALTER TABLE public.goldset_questions_v2 ADD COLUMN IF NOT EXISTS gold_doc_ids TEXT[]")
        rows = conn.execute(
            f"SELECT id, gold_sources FROM public.goldset_questions_v2 WHERE {' AND '.join(where)} ORDER BY id", params
        ).fetchall()
        print(f"rows to resolve: {len(rows)}")

        updated = resolved_beyond_raw = 0
        for row in rows:
            gold_sources = parse_text_list(row["gold_sources"])
            gold_doc_ids = resolve_gold_doc_ids(gold_sources, maps)
            # "resolved beyond raw" = produced at least one id that was not just the raw token
            if set(gold_doc_ids) - {str(s).strip() for s in gold_sources}:
                resolved_beyond_raw += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE public.goldset_questions_v2 SET gold_doc_ids = %s, updated_at = now() WHERE id = %s",
                    (gold_doc_ids, row["id"]),
                )
            updated += 1

    verb = "would update" if args.dry_run else "updated"
    print(f"{verb} {updated} rows; {resolved_beyond_raw} resolved to corpus doc_ids beyond the raw label")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
