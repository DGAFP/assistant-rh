#!/usr/bin/env python3
"""Seed the v2 selector prompt as a NEW row and switch the runtime pointer (issue #299).

La table ``system_prompts`` n'a pas de versioning (PK = ``name``) : la
convention du repo est de versionner par le nom (cf. ``system_prompt_V6_optimized.md``)
et de basculer le pointeur runtime ``rag_config.v3_selector_prompt_name``.

Ce script pousse le contenu du fichier fallback packagé
(``assistant_rh_rag_pipeline/prompts/selector.md``) dans une **nouvelle** ligne
``v3_selector_business_v2.md`` — la ligne ``v3_selector_business.md`` existante
n'est pas touchée, elle reste disponible pour rollback et comparaison au rejeu
(#298). La bascule du pointeur est une étape séparée (``--activate``).

Idempotent : si la ligne v2 porte déjà le contenu du fichier (et que le
pointeur est déjà basculé, avec ``--activate``), le script ne fait rien.

Safety:
  - Dry-run par défaut : affiche le diff (vs la v1 active, ou vs la v2 si elle
    existe déjà) et n'écrit rien. ``--apply`` écrit la ligne v2 ;
    ``--activate`` (implique ``--apply``) bascule en plus le pointeur.
  - DSN lu dans l'environnement standard (même résolution que l'app).

Usage (via ``uv`` so the workspace package ``assistant_rh_rag_pipeline`` is on
the path — a plain ``python scripts/...`` fails with ModuleNotFoundError)::

    uv run python scripts/seed_selector_prompt.py              # dry-run
    uv run python scripts/seed_selector_prompt.py --apply      # écrit la ligne v2
    uv run python scripts/seed_selector_prompt.py --activate   # écrit + bascule le pointeur
"""

from __future__ import annotations

import argparse
import difflib
import sys

OLD_PROMPT_NAME = "v3_selector_business.md"
PROMPT_NAME = "v3_selector_business_v2.md"
PROMPT_TYPE = "llm_selector"
DESCRIPTION = "Selector V3 v2 — cascade ministérielle, sélection généreuse, périmètre FPE (#299)"
POINTER_KEY = "v3_selector_prompt_name"


def _diff(before: str, after: str, from_label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{from_label} (DB)",
            tofile=f"{PROMPT_NAME} (fichier)",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Écrit la ligne v2 (défaut : dry-run).")
    parser.add_argument(
        "--activate",
        action="store_true",
        help=f"Écrit la ligne v2 (implique --apply) puis pointe rag_config.{POINTER_KEY} dessus.",
    )
    parser.add_argument("--updated-by", default="issue-299-selector-cascade")
    args = parser.parse_args()
    apply = args.apply or args.activate

    # Imported lazily so ``--help`` works without a DSN.
    from assistant_rh_rag_pipeline.db_helpers import (
        _PROMPTS_DIR,
        _db_conn,
        get_runtime_config,
        has_dsn,
        update_runtime_config,
    )

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
            cur.execute(
                "SELECT name, content FROM system_prompts WHERE name IN (%s, %s)",
                (OLD_PROMPT_NAME, PROMPT_NAME),
            )
            rows = {name: row_content or "" for name, row_content in cur.fetchall()}

            row_up_to_date = rows.get(PROMPT_NAME) == content
            if row_up_to_date:
                print(f"{PROMPT_NAME}: déjà à jour.")
            else:
                # Diff vs la v2 si elle existe (re-seed), sinon vs la v1 active
                # pour que l'opérateur voie ce que la nouvelle version change.
                if PROMPT_NAME in rows:
                    before, label = rows[PROMPT_NAME], PROMPT_NAME
                else:
                    before, label = rows.get(OLD_PROMPT_NAME, ""), OLD_PROMPT_NAME
                print(f"=== {PROMPT_NAME} (nouvelle ligne, {OLD_PROMPT_NAME} non modifiée) ===")
                print(_diff(before, content, label))

            if apply and not row_up_to_date:
                cur.execute(
                    """INSERT INTO system_prompts (name, content, description, prompt_type, updated_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (name) DO UPDATE SET content = EXCLUDED.content,
                       description = EXCLUDED.description, prompt_type = EXCLUDED.prompt_type,
                       updated_by = EXCLUDED.updated_by, updated_at = CURRENT_TIMESTAMP""",
                    (PROMPT_NAME, content, DESCRIPTION, PROMPT_TYPE, args.updated_by),
                )
                conn.commit()
                print(f"Ligne {PROMPT_NAME} écrite.")
    finally:
        conn.close()

    pointer = get_runtime_config().get(POINTER_KEY)
    print(f"Pointeur runtime {POINTER_KEY} = {pointer!r}")
    if args.activate:
        if pointer == PROMPT_NAME:
            print("Pointeur déjà basculé — rien à faire.")
        elif update_runtime_config({POINTER_KEY: PROMPT_NAME}, updated_by=args.updated_by):
            print(f"Pointeur basculé sur {PROMPT_NAME}.")
        else:
            print("ERROR: bascule du pointeur échouée.", file=sys.stderr)
            return 1
    elif pointer != PROMPT_NAME:
        print(f"Bascule non demandée : le runtime reste sur {pointer!r} (utiliser --activate).")
    if not apply:
        print("Dry-run only — re-run with --apply (ou --activate) to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
