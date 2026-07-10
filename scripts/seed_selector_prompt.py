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
from collections.abc import Sequence

import psycopg

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


class SeedError(RuntimeError):
    """Raised when the seed cannot reach its requested durable state."""


def _prompt_is_current(row: dict[str, object] | None, content: str) -> bool:
    return bool(
        row and row["content"] == content and row["description"] == DESCRIPTION and row["prompt_type"] == PROMPT_TYPE and row["is_active"] is True
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Écrit la ligne v2 (défaut : dry-run).")
    parser.add_argument(
        "--activate",
        action="store_true",
        help=f"Écrit la ligne v2 (implique --apply) puis pointe rag_config.{POINTER_KEY} dessus.",
    )
    parser.add_argument("--updated-by", default="issue-299-selector-cascade")
    args = parser.parse_args(argv)
    apply = args.apply or args.activate

    # Imported lazily so ``--help`` works without a DSN.
    from assistant_rh_rag_pipeline.db_helpers import (
        _PROMPTS_DIR,
        _db_conn,
        has_dsn,
    )

    content = (_PROMPTS_DIR / "selector.md").read_text(encoding="utf-8")

    if not has_dsn():
        print("ERROR: no database connection (set the DSN env, e.g. SCW_POSTGRES_DSN).", file=sys.stderr)
        return 2
    conn = _db_conn()
    if conn is None:
        print("ERROR: database connection failed (DSN set but unreachable).", file=sys.stderr)
        return 2

    dirty = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT name, content, description, prompt_type, is_active
                   FROM system_prompts WHERE name IN (%s, %s)""",
                (OLD_PROMPT_NAME, PROMPT_NAME),
            )
            rows = {
                name: {
                    "content": row_content or "",
                    "description": description or "",
                    "prompt_type": prompt_type or "",
                    "is_active": is_active,
                }
                for name, row_content, description, prompt_type, is_active in cur.fetchall()
            }

            pointer_query = "SELECT config ->> %s::text FROM rag_config WHERE id = 1"
            if args.activate:
                pointer_query += " FOR UPDATE"
            cur.execute(pointer_query, (POINTER_KEY,))
            pointer_row = cur.fetchone()
            if pointer_row is None:
                raise SeedError("rag_config id=1 introuvable ; aucune modification appliquée.")
            pointer = pointer_row[0]

            row_up_to_date = _prompt_is_current(rows.get(PROMPT_NAME), content)
            if row_up_to_date:
                print(f"{PROMPT_NAME}: déjà à jour.")
            else:
                # Diff vs la v2 si elle existe (re-seed), sinon vs la v1 active
                # pour que l'opérateur voie ce que la nouvelle version change.
                if PROMPT_NAME in rows:
                    before, label = str(rows[PROMPT_NAME]["content"]), PROMPT_NAME
                else:
                    before = str((rows.get(OLD_PROMPT_NAME) or {}).get("content", ""))
                    label = OLD_PROMPT_NAME
                print(f"=== {PROMPT_NAME} (nouvelle ligne, {OLD_PROMPT_NAME} non modifiée) ===")
                print(_diff(before, content, label))
                current = rows.get(PROMPT_NAME)
                if current and current["content"] == content:
                    print("Contenu identique ; métadonnées ou état actif à réparer.")

            if apply and not row_up_to_date:
                cur.execute(
                    """INSERT INTO system_prompts
                           (name, content, description, prompt_type, is_active, updated_by, updated_at)
                       VALUES (%s, %s, %s, %s, TRUE, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (name) DO UPDATE SET content = EXCLUDED.content,
                       description = EXCLUDED.description, prompt_type = EXCLUDED.prompt_type,
                       is_active = TRUE, updated_by = EXCLUDED.updated_by,
                       updated_at = CURRENT_TIMESTAMP""",
                    (PROMPT_NAME, content, DESCRIPTION, PROMPT_TYPE, args.updated_by),
                )
                dirty = True
                print(f"Ligne {PROMPT_NAME} écrite.")

            print(f"Pointeur runtime {POINTER_KEY} = {pointer!r}")
            if args.activate:
                if pointer == PROMPT_NAME:
                    print("Pointeur déjà basculé — rien à faire.")
                else:
                    cur.execute(
                        """UPDATE rag_config
                           SET config = config || jsonb_build_object(
                                   %s::text, %s::text,
                                   'updated_at', CURRENT_TIMESTAMP,
                                   'updated_by', %s::text
                               ),
                               updated_at = CURRENT_TIMESTAMP,
                               updated_by = %s
                           WHERE id = 1
                           RETURNING config ->> %s::text""",
                        (POINTER_KEY, PROMPT_NAME, args.updated_by, args.updated_by, POINTER_KEY),
                    )
                    updated_pointer = cur.fetchone()
                    if updated_pointer is None or updated_pointer[0] != PROMPT_NAME:
                        raise SeedError("échec de la bascule atomique du pointeur runtime.")
                    dirty = True
                    print(f"Pointeur basculé sur {PROMPT_NAME}.")
            elif pointer != PROMPT_NAME:
                print(f"Bascule non demandée : le runtime reste sur {pointer!r} (utiliser --activate).")

            if dirty:
                conn.commit()
            if not apply:
                print("Dry-run only — re-run with --apply (ou --activate) to persist.")
        return 0
    except (SeedError, psycopg.Error) as exc:
        conn.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
