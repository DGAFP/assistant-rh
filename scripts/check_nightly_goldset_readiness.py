from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_COLUMNS = ["id", "question", "goldset_name", "tags", "gold_sources"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Precheck conformance nightly goldset prerequisites.")
    parser.add_argument("--goldset-name", required=True, help="goldset name to validate.")
    parser.add_argument("--limit", type=int, default=0, help="Expected nightly limit (0 = no minimum cardinality check).")
    parser.add_argument("--dsn", type=str, default=None, help="PostgreSQL DSN (overrides --dsn-env).")
    parser.add_argument("--dsn-env", type=str, default="SCW_POSTGRES_DSN", help="Environment variable containing DSN.")
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=None,
        help="Optional markdown output path for GitHub step summary.",
    )
    return parser


def resolve_dsn(explicit_dsn: str | None, dsn_env: str) -> str:
    if explicit_dsn:
        return explicit_dsn

    first_try = os.getenv(dsn_env, "").strip()
    if first_try:
        return first_try

    raise RuntimeError(f"No DSN found. Provide --dsn or set {dsn_env}.")


def write_summary(path: Path | None, markdown: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    dsn = resolve_dsn(args.dsn, args.dsn_env)
    errors: list[str] = []
    diagnostics: list[str] = []

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        table_exists = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'goldset_questions_v2'
            ) AS value
            """
        ).fetchone()["value"]

        if not table_exists:
            errors.append("Table public.goldset_questions_v2 is missing.")
        else:
            rows = conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'goldset_questions_v2'
                """
            ).fetchall()
            available_columns = {row["column_name"] for row in rows}
            missing_columns = [name for name in REQUIRED_COLUMNS if name not in available_columns]
            if missing_columns:
                errors.append(
                    "Missing required columns on public.goldset_questions_v2: "
                    + ", ".join(missing_columns)
                )

        eligible_count = 0
        total_goldset_count = 0
        if not errors:
            total_goldset_count = conn.execute(
                """
                SELECT COUNT(*) AS value
                FROM public.goldset_questions_v2
                WHERE goldset_name = %s
                """,
                (args.goldset_name,),
            ).fetchone()["value"]

            eligible_count = conn.execute(
                """
                SELECT COUNT(*) AS value
                FROM public.goldset_questions_v2
                WHERE goldset_name = %s
                  AND gold_sources IS NOT NULL
                  AND btrim(gold_sources) <> ''
                """,
                (args.goldset_name,),
            ).fetchone()["value"]

            diagnostics.append(f"Rows for goldset_name={args.goldset_name}: {total_goldset_count}")
            diagnostics.append(f"Rows with non-empty gold_sources: {eligible_count}")

            if eligible_count == 0:
                errors.append(
                    "No eligible rows for nightly selection (gold_sources is NULL or empty for all rows)."
                )

            if args.limit > 0 and eligible_count < args.limit:
                errors.append(
                    f"Eligible row count ({eligible_count}) is below nightly limit ({args.limit})."
                )

    status = "✅ PASS" if not errors else "❌ FAIL"
    summary_lines = [
        "## Nightly goldset precheck",
        "",
        f"- Status: {status}",
        f"- Goldset: `{args.goldset_name}`",
        f"- Limit: `{args.limit}`",
    ]

    if diagnostics:
        summary_lines.append("")
        summary_lines.append("### Diagnostics")
        for line in diagnostics:
            summary_lines.append(f"- {line}")

    if errors:
        summary_lines.append("")
        summary_lines.append("### Missing prerequisites")
        for line in errors:
            summary_lines.append(f"- {line}")

    summary_markdown = "\n".join(summary_lines) + "\n"
    write_summary(args.summary_path, summary_markdown)

    if errors:
        raise SystemExit("; ".join(errors))

    print(summary_markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
