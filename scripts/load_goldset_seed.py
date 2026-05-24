from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load a goldset seed JSONL into PostgreSQL.")
    parser.add_argument(
        "--input",
        type=Path,
        default=REPO_ROOT / "tests/conformance/seeds/goldset_questions_v2.synthetic_docs_v1.jsonl",
        help="Input JSONL path.",
    )
    parser.add_argument(
        "--target-dsn",
        type=str,
        default=None,
        help="Target PostgreSQL DSN (overrides --target-dsn-env).",
    )
    parser.add_argument(
        "--target-dsn-env",
        type=str,
        default="SCW_POSTGRES_DSN",
        help="Environment variable containing target DSN.",
    )
    parser.add_argument(
        "--replace-goldset",
        action="store_true",
        help="Delete existing rows for goldset_name values present in the seed before upsert.",
    )
    return parser


def resolve_target_dsn(explicit_dsn: str | None, dsn_env: str) -> str:
    if explicit_dsn:
        return explicit_dsn

    first_try = os.getenv(dsn_env, "").strip()
    if first_try:
        return first_try

    for fallback_env in ("SCALINGO_POSTGRESQL_URL",):
        value = os.getenv(fallback_env, "").strip()
        if value:
            return value

    raise RuntimeError(f"No DSN found. Provide --target-dsn or set {dsn_env}.")


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"Seed file not found: {path}")

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)

            question = str(obj.get("question", "") or "").strip()
            if not question:
                raise RuntimeError(f"Missing non-empty question at line {idx}")

            tags = obj.get("tags") or []
            if not isinstance(tags, list):
                raise RuntimeError(f"Expected list for tags at line {idx}")

            rows.append(
                {
                    "question": question,
                    "gold_answer": obj.get("gold_answer"),
                    "gold_sources": obj.get("gold_sources"),
                    "theme": obj.get("theme"),
                    "source": obj.get("source"),
                    "goldset_name": obj.get("goldset_name"),
                    "comment": obj.get("comment"),
                    "original_turn_id": obj.get("original_turn_id"),
                    "difficulty": obj.get("difficulty"),
                    "tags": tags,
                    "updated_at": datetime.now(tz=UTC),
                }
            )
    return rows


def ensure_required_table(conn: psycopg.Connection) -> None:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'goldset_questions_v2'
        ) AS table_exists
        """
    ).fetchone()
    if not row or not row["table_exists"]:
        raise RuntimeError(
            "Required table public.goldset_questions_v2 is missing. Apply Supabase migrations first."
        )


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    rows = parse_jsonl(args.input)
    if not rows:
        raise SystemExit("Seed file is empty")

    target_dsn = resolve_target_dsn(args.target_dsn, args.target_dsn_env)
    goldset_names_set: set[str] = set()
    for row in rows:
        raw_name = row.get("goldset_name")
        if raw_name is None:
            continue

        name = str(raw_name).strip()
        if not name:
            continue

        goldset_names_set.add(name)

    goldset_names = sorted(goldset_names_set)

    insert_sql = """
        INSERT INTO public.goldset_questions_v2 (
            question,
            gold_answer,
            gold_sources,
            theme,
            source,
            goldset_name,
            comment,
            original_turn_id,
            difficulty,
            tags,
            updated_at
        )
        VALUES (
            %(question)s,
            %(gold_answer)s,
            %(gold_sources)s,
            %(theme)s,
            %(source)s,
            %(goldset_name)s,
            %(comment)s,
            %(original_turn_id)s,
            %(difficulty)s,
            %(tags)s,
            %(updated_at)s
        )
        ON CONFLICT (question) DO UPDATE
        SET
            gold_answer = EXCLUDED.gold_answer,
            gold_sources = EXCLUDED.gold_sources,
            theme = EXCLUDED.theme,
            source = EXCLUDED.source,
            goldset_name = EXCLUDED.goldset_name,
            comment = EXCLUDED.comment,
            original_turn_id = EXCLUDED.original_turn_id,
            difficulty = EXCLUDED.difficulty,
            tags = EXCLUDED.tags,
            updated_at = EXCLUDED.updated_at
    """

    with psycopg.connect(target_dsn, autocommit=False, row_factory=psycopg.rows.dict_row) as conn:
        ensure_required_table(conn)

        deleted_rows = 0
        if args.replace_goldset and goldset_names:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.goldset_questions_v2 WHERE goldset_name = ANY(%s)",
                    (goldset_names,),
                )
                deleted_rows = cur.rowcount or 0

        with conn.cursor() as cur:
            for row in rows:
                cur.execute(insert_sql, row)

        conn.commit()

        total_after = conn.execute("SELECT COUNT(*) AS count FROM public.goldset_questions_v2").fetchone()["count"]
        eligible_after = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM public.goldset_questions_v2
            WHERE goldset_name = ANY(%s)
              AND gold_sources IS NOT NULL
              AND btrim(gold_sources) <> ''
            """,
            (goldset_names,),
        ).fetchone()["count"]

    print(
        json.dumps(
            {
                "status": "ok",
                "input": str(args.input),
                "loaded_rows": len(rows),
                "deleted_rows": deleted_rows,
                "goldset_names": goldset_names,
                "eligible_rows_after": int(eligible_after),
                "table_rows_after": int(total_after),
                "target_dsn_env": args.target_dsn_env,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
