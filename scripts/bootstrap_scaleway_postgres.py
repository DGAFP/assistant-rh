from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def resolve_dsn(explicit_dsn: str | None, env_var: str) -> str:
    if explicit_dsn:
        return explicit_dsn
    dsn = os.getenv(env_var, "")
    if dsn:
        return dsn
    raise RuntimeError(
        f"Aucun DSN trouvé. Passe --dsn ou définis {env_var} dans l'environnement."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap le schéma PostgreSQL cible pour assistant-rh sur Scaleway."
    )
    parser.add_argument(
        "--sql-path",
        default="config/sql/scaleway_postgres_core_schema.sql",
        help="Fichier SQL à appliquer.",
    )
    parser.add_argument(
        "--dsn-env",
        default="SCW_POSTGRES_DSN",
        help="Nom de la variable d'environnement contenant le DSN cible.",
    )
    parser.add_argument(
        "--dsn",
        help="DSN Postgres cible. Prioritaire sur --dsn-env.",
    )
    return parser


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    sql_path = REPO_ROOT / args.sql_path
    sql_text = sql_path.read_text(encoding="utf-8")
    dsn = resolve_dsn(args.dsn, args.dsn_env)

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)

    print(
        json.dumps(
            {
                "status": "ok",
                "sql_path": str(sql_path),
                "dsn_env": args.dsn_env,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
