#!/usr/bin/env python3
"""Migrate DB-stored system prompts to ministry-agnostic placeholders.

The RAG prompts (``system_prompts`` table) were authored for a single ministry
(MATTE). They now carry ``{ministere_label}`` / ``{ministere_sigle}`` placeholders
that the pipeline resolves per request to the selected ministry (see
``ministry_scope.render_ministry_prompt``). This script rewrites the *live* DB
rows of each environment to that placeholder form.

The transform is deterministic and **idempotent**: once a prompt carries the
placeholders (and no bare ``MATTE`` remains), re-running is a no-op. It is also
content-agnostic — it works on whatever each environment currently stores,
including admin-tuned prompts, because it targets the ministry tokens rather
than whole-line matches.

Safety:
  - Dry-run by default: prints a unified diff per changed prompt and writes
    nothing. Pass ``--apply`` to persist.
  - Reads the DSN from the standard environment (same resolution as the app).

Usage::

    python scripts/migrate_ministry_agnostic_prompts.py              # dry-run
    python scripts/migrate_ministry_agnostic_prompts.py --apply       # write
    python scripts/migrate_ministry_agnostic_prompts.py --name system_prompt_V6_optimized.md
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from typing import List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Transform (pure, unit-tested)
# ─────────────────────────────────────────────────────────────────────────────

# Ordered replacements. Order matters: the quoted requested_source value and the
# full ministry name are handled before the catch-all bare-word rule so they map
# to the right placeholder / generic category.
_FULL_NAME = r"Minist[eè]re\s+de\s+la\s+Transition\s+[EÉ]cologique"

_REPLACEMENTS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    # intent requested_source enum → generic categories. Combined form first so
    # the standalone rules below don't partially rewrite it.
    (re.compile(r'"MATTE\s*\|\s*Service-Public"'), '"ministere|service_public"'),
    (re.compile(r'"MATTE"'), '"ministere"'),
    (re.compile(r"'MATTE'"), "'ministere'"),
    (re.compile(r'"Service-Public"'), '"service_public"'),
    (re.compile(r"'Service-Public'"), "'service_public'"),
    # tenant full name, with or without the trailing "- MATTE" / "(MATTE)"
    (re.compile(_FULL_NAME + r"\s*[-–(]\s*MATTE\s*\)?"), "{ministere_label} ({ministere_sigle})"),
    (re.compile(_FULL_NAME), "{ministere_label}"),
    # catch-all: any remaining bare MATTE (source tag / practice reference)
    (re.compile(r"\bMATTE\b"), "{ministere_sigle}"),
)


def ministry_agnostic_transform(content: str) -> str:
    """Return *content* with ministry-specific wording replaced by placeholders."""
    for pattern, replacement in _REPLACEMENTS:
        content = pattern.sub(replacement, content)
    return content


# ─────────────────────────────────────────────────────────────────────────────
# DB migration
# ─────────────────────────────────────────────────────────────────────────────


def _load_prompts(cur, names: List[str] | None):
    if names:
        cur.execute(
            "SELECT name, prompt_type, content FROM system_prompts WHERE is_active = TRUE AND name = ANY(%s) ORDER BY prompt_type, name",
            (names,),
        )
    else:
        cur.execute("SELECT name, prompt_type, content FROM system_prompts WHERE is_active = TRUE ORDER BY prompt_type, name")
    return cur.fetchall()


def _diff(name: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{name} (before)",
            tofile=f"{name} (after)",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry-run).")
    parser.add_argument("--name", action="append", dest="names", help="Limit to a specific prompt name (repeatable).")
    parser.add_argument("--updated-by", default="ministry-agnostic-migration")
    args = parser.parse_args()

    # Imported lazily so ``--help`` and the pure transform work without a DSN.
    from assistant_rh_rag_pipeline.db_helpers import _db_conn

    conn = _db_conn()
    if conn is None:
        print("ERROR: no database connection (set the DSN env, e.g. SCW_POSTGRES_DSN).", file=sys.stderr)
        return 2

    changed = 0
    scanned = 0
    leftover_matte: List[str] = []
    try:
        with conn.cursor() as cur:
            rows = _load_prompts(cur, args.names)
            for name, prompt_type, content in rows:
                scanned += 1
                new_content = ministry_agnostic_transform(content)
                if new_content == content:
                    continue
                changed += 1
                print(f"\n=== {name}  [{prompt_type}] ===")
                print(_diff(name, content, new_content))
                if re.search(r"\bMATTE\b", new_content):
                    leftover_matte.append(name)
                if args.apply:
                    cur.execute(
                        "UPDATE system_prompts SET content = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE name = %s",
                        (new_content, args.updated_by, name),
                    )
        if args.apply:
            conn.commit()
    finally:
        conn.close()

    print(f"\nScanned {scanned} active prompt(s); {changed} would change.")
    if leftover_matte:
        print(f"WARNING: residual 'MATTE' after transform in: {', '.join(leftover_matte)}", file=sys.stderr)
    if not args.apply:
        print("Dry-run only — re-run with --apply to persist.")
    else:
        print("Applied." if changed else "Nothing to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
