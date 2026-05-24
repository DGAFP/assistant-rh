from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_SUPABASE_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a reproducible goldset seed JSONL file.")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "tests/conformance/seeds/goldset_questions_v2.synthetic_docs_v1.jsonl",
        help="Output JSONL path.",
    )
    parser.add_argument(
        "--goldset-name",
        action="append",
        default=None,
        help="goldset_questions_v2.goldset_name filter (repeatable).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of rows to export (0 = no limit).",
    )
    parser.add_argument(
        "--source-dsn",
        type=str,
        default=None,
        help="Source PostgreSQL DSN (overrides --source-dsn-env).",
    )
    parser.add_argument(
        "--source-dsn-env",
        type=str,
        default="LOCAL_SUPABASE_DSN",
        help="Environment variable containing source DSN.",
    )
    return parser


def resolve_source_dsn(explicit_dsn: str | None, dsn_env: str) -> str:
    if explicit_dsn:
        return explicit_dsn

    env_dsn = os.getenv(dsn_env, "").strip()
    if env_dsn:
        return env_dsn

    return DEFAULT_LOCAL_SUPABASE_DSN


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    raw_goldset_names = args.goldset_name or ["synthetic_docs_v1"]
    goldset_names = sorted({name.strip() for name in raw_goldset_names if name.strip()})
    if not goldset_names:
        raise SystemExit("Provide at least one --goldset-name value")

    source_dsn = resolve_source_dsn(args.source_dsn, args.source_dsn_env)

    where = ["gold_sources IS NOT NULL", "btrim(gold_sources) <> ''"]
    params: list[Any] = []

    placeholders = ",".join(["%s"] * len(goldset_names))
    where.append(f"goldset_name IN ({placeholders})")
    params.extend(goldset_names)

    limit_sql = ""
    if args.limit and args.limit > 0:
        limit_sql = " LIMIT %s"
        params.append(args.limit)

    sql = f"""
        SELECT
            question,
            gold_answer,
            gold_sources,
            theme,
            source,
            goldset_name,
            comment,
            original_turn_id,
            difficulty,
            tags
        FROM public.goldset_questions_v2
        WHERE {' AND '.join(where)}
        ORDER BY id
        {limit_sql}
    """

    with psycopg.connect(source_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    payload_rows: list[dict[str, Any]] = []
    for row in rows:
        payload_rows.append(
            {
                "question": row["question"],
                "gold_answer": row.get("gold_answer"),
                "gold_sources": row.get("gold_sources"),
                "theme": row.get("theme"),
                "source": row.get("source"),
                "goldset_name": row.get("goldset_name"),
                "comment": row.get("comment"),
                "original_turn_id": row.get("original_turn_id"),
                "difficulty": row.get("difficulty"),
                "tags": row.get("tags") or [],
            }
        )

    if not payload_rows:
        raise SystemExit(
            "No eligible rows found for selected goldset_name values (requires non-empty gold_sources)."
        )

    write_jsonl(args.output, payload_rows)

    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "row_count": len(payload_rows),
                "goldset_names": goldset_names,
                "limit": args.limit,
                "source_dsn_env": args.source_dsn_env,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
