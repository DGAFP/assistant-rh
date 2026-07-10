#!/usr/bin/env python3
"""Seed the live selector prompt from the canonical fallback file (issue #299).

The runtime reads the selector prompt from the ``system_prompts`` DB row
``v3_selector_business.md`` and only falls back to the packaged file
``assistant_rh_rag_pipeline/prompts/selector.md`` when the DB is unreachable.
This script pushes the packaged file's content into the live row so both stay
aligned — run it against each environment (dev → staging) after editing the
fallback file.

The write is idempotent: when the DB row already matches the file, nothing
happens. When it differs (including admin-tuned content), the unified diff is
printed so the operator sees exactly what would be overwritten.

Safety:
  - Dry-run by default: prints the diff and writes nothing. Pass ``--apply``
    to persist.
  - Reads the DSN from the standard environment (same resolution as the app).

Usage (via ``uv`` so the workspace package ``assistant_rh_rag_pipeline`` is on
the path — a plain ``python scripts/...`` fails with ModuleNotFoundError)::

    uv run python scripts/seed_selector_prompt.py           # dry-run
    uv run python scripts/seed_selector_prompt.py --apply   # write
"""

from __future__ import annotations

import argparse
import difflib
import sys

PROMPT_NAME = "v3_selector_business.md"
PROMPT_TYPE = "llm_selector"
DESCRIPTION = "Selector V3 — cascade ministérielle, sélection généreuse, périmètre FPE (#299)"


def _diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{PROMPT_NAME} (DB)",
            tofile=f"{PROMPT_NAME} (fichier)",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry-run).")
    parser.add_argument("--updated-by", default="issue-299-selector-cascade")
    args = parser.parse_args()

    # Imported lazily so ``--help`` works without a DSN.
    from assistant_rh_rag_pipeline.db_helpers import _PROMPTS_DIR, _db_conn, has_dsn

    content = (_PROMPTS_DIR / "selector.md").read_text(encoding="utf-8")

    if not has_dsn():
        print("ERROR: no database connection (set the DSN env, e.g. SCW_POSTGRES_DSN).", file=sys.stderr)
        return 2
    conn = _db_conn()
    if conn is None:
        print("ERROR: database connection failed (DSN set but unreachable).", file=sys.stderr)
        return 2

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM system_prompts WHERE name = %s", (PROMPT_NAME,))
            row = cur.fetchone()
            before = row[0] if row and row[0] else ""

            if before == content:
                print(f"{PROMPT_NAME}: already up to date — nothing to do.")
                return 0

            print(f"=== {PROMPT_NAME} ===" if row else f"=== {PROMPT_NAME} (nouvelle ligne) ===")
            print(_diff(before, content))

            if args.apply:
                cur.execute(
                    """INSERT INTO system_prompts (name, content, description, prompt_type, updated_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (name) DO UPDATE SET content = EXCLUDED.content,
                       description = EXCLUDED.description, prompt_type = EXCLUDED.prompt_type,
                       updated_by = EXCLUDED.updated_by, updated_at = CURRENT_TIMESTAMP""",
                    (PROMPT_NAME, content, DESCRIPTION, PROMPT_TYPE, args.updated_by),
                )
                conn.commit()
                print("Applied.")
            else:
                print("Dry-run only — re-run with --apply to persist.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
