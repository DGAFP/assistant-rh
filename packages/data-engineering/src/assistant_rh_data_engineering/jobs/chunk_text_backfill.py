"""Backfill a chunk table's text column from a sibling column where empty.

Context (issue #177): the MATTE ingestion populated ``rag_chunks_matte.text`` but
left ``chunk_text`` empty on 822/959 rows, while the retriever (and
``feedback_analyzer``) read ``chunk_text``. Every other source populates
``chunk_text`` (service_public/dgafp even generate their lexical tsv from it), so
MATTE is the single divergent table. This job copies ``text`` -> ``chunk_text``
where ``chunk_text`` is empty, aligning MATTE with the canonical schema without
any retriever code change.

It is a pure-SQL job (no models, no APIs). The ``--check-only`` mode is read-only
and never writes. Modelled on ``embeddings_backfill.py`` (argparse + load_dotenv
+ psycopg connection + JSON summary + exit code).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql

logger = logging.getLogger(__name__)

DEFAULT_TABLES = ("rag_chunks_matte",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill chunk_text depuis une colonne texte sœur là où chunk_text est vide.",
    )
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--table",
        action="append",
        dest="tables",
        help="Table à traiter (répétable). Défaut: rag_chunks_matte.",
    )
    parser.add_argument("--source-col", default="text", help="Colonne source du texte (défaut: text).")
    parser.add_argument("--target-col", default="chunk_text", help="Colonne cible à remplir (défaut: chunk_text).")
    parser.add_argument("--limit", type=int, help="Borne le nombre de lignes mises à jour par table.")
    parser.add_argument(
        "--check-only",
        "--dry-run",
        dest="check_only",
        action="store_true",
        help=(
            "Audit read-only: ouvre une connexion (autocommit), exécute des SELECT agrégés "
            "et reporte la couverture de la colonne cible. N'écrit jamais en base."
        ),
    )
    parser.add_argument(
        "--coverage-min-pct",
        type=float,
        default=None,
        help="Seuil de couverture pour --check-only (comparé au ratio brut non_vide/total). Défaut: 100.",
    )
    return parser


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        return cur.fetchone() is not None


def column_exists(conn: psycopg.Connection, schema: str, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
            """,
            (schema, table, column),
        )
        return cur.fetchone() is not None


def _empty_target_predicate(target_col: str) -> sql.Composed:
    # "Cible vide" = NULL ou uniquement des espaces. Doit rester identique entre
    # l'audit (--check-only) et le UPDATE pour que les deux comptent les mêmes lignes.
    return sql.SQL("LENGTH(TRIM(COALESCE({target}, ''))) = 0").format(target=sql.Identifier(target_col))


def _source_present_predicate(source_col: str) -> sql.Composed:
    return sql.SQL("LENGTH(TRIM(COALESCE({source}, ''))) > 0").format(source=sql.Identifier(source_col))


def build_coverage_query(schema: str, table: str, source_col: str, target_col: str) -> sql.Composed:
    """Read-only aggregate used by --check-only. SELECT-only by construction."""
    return sql.SQL(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE {empty_target}) AS empty_target,
            COUNT(*) FILTER (WHERE {empty_target} AND {source_present}) AS backfillable
        FROM {schema}.{table}
        """
    ).format(
        empty_target=_empty_target_predicate(target_col),
        source_present=_source_present_predicate(source_col),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
    )


def build_update_query(
    schema: str,
    table: str,
    source_col: str,
    target_col: str,
    *,
    with_updated_at: bool,
    limit: int | None,
) -> sql.Composed:
    """UPDATE that only fills empty targets from a non-empty source (idempotent)."""
    set_clauses = [
        sql.SQL("{target} = {source}").format(
            target=sql.Identifier(target_col),
            source=sql.Identifier(source_col),
        )
    ]
    if with_updated_at:
        set_clauses.append(sql.SQL("updated_at = CURRENT_TIMESTAMP"))

    where = sql.SQL("{empty_target} AND {source_present}").format(
        empty_target=_empty_target_predicate(target_col),
        source_present=_source_present_predicate(source_col),
    )

    if limit:
        # UPDATE n'accepte pas LIMIT directement: borner via les ctid des lignes éligibles.
        scope = sql.SQL("ctid IN (SELECT ctid FROM {schema}.{table} WHERE {where} LIMIT {limit})").format(
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
            where=where,
            limit=sql.Literal(int(limit)),
        )
    else:
        scope = where

    return sql.SQL("UPDATE {schema}.{table} SET {set_clause} WHERE {scope}").format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        set_clause=sql.SQL(", ").join(set_clauses),
        scope=scope,
    )


def audit_text_coverage(
    conn: psycopg.Connection,
    schema: str,
    tables: list[str],
    source_col: str,
    target_col: str,
) -> dict[str, Any]:
    """Return read-only coverage stats for the target column per table."""

    report: dict[str, Any] = {
        "schema": schema,
        "source_col": source_col,
        "target_col": target_col,
        "tables": {},
        "missing_tables": [],
        "missing_columns": {},
    }
    for table in tables:
        if not table_exists(conn, schema, table):
            report["missing_tables"].append(table)
            continue
        missing = [c for c in (source_col, target_col) if not column_exists(conn, schema, table, c)]
        if missing:
            report["missing_columns"][table] = missing
            continue
        query = build_coverage_query(schema, table, source_col, target_col)
        with conn.cursor() as cur:
            cur.execute(query)
            total, empty_target, backfillable = (int(value or 0) for value in cur.fetchone())
        non_empty = total - empty_target
        report["tables"][table] = {
            "total": total,
            "empty_target": empty_target,
            "non_empty_target": non_empty,
            "backfillable": backfillable,
            "is_empty": total == 0,
            "coverage_pct": round(100.0 * non_empty / total, 2) if total else 0.0,
        }
    return report


def evaluate_coverage_report(report: dict[str, Any], *, coverage_min_pct: float | None) -> tuple[int, list[str]]:
    threshold = coverage_min_pct if coverage_min_pct is not None else 100.0
    problems = [f"Table absente: {table}" for table in report.get("missing_tables", [])]
    for table, missing in (report.get("missing_columns") or {}).items():
        problems.append(f"{table}: colonne(s) absente(s): {', '.join(missing)}")
    for table, stats in (report.get("tables") or {}).items():
        total = int(stats.get("total") or 0)
        if stats.get("is_empty") or total == 0:
            problems.append(f"{table}: table vide (0 ligne), aucune couverture possible")
            continue
        non_empty = int(stats.get("non_empty_target") or 0)
        raw_pct = 100.0 * non_empty / total
        if raw_pct < threshold:
            problems.append(
                f"{table}.{report.get('target_col')}: couverture {round(raw_pct, 2)}% < seuil {threshold}% "
                f"({stats.get('backfillable')} ligne(s) à backfiller)"
            )
    return (1 if problems else 0), problems


def _has_updated_at(conn: psycopg.Connection, schema: str, table: str) -> bool:
    return column_exists(conn, schema, table, "updated_at")


def backfill_table(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    source_col: str,
    target_col: str,
    limit: int | None,
) -> int:
    """Copy source_col -> target_col where target is empty and source present. Returns rows updated."""

    query = build_update_query(
        schema,
        table,
        source_col,
        target_col,
        with_updated_at=_has_updated_at(conn, schema, table),
        limit=limit,
    )
    with conn.cursor() as cur:
        cur.execute(query)
        updated = cur.rowcount
    conn.commit()
    return int(updated or 0)


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(Path(args.env_file))
    dsn = os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"{args.dsn_env} manquant.")

    tables = list(args.tables) if args.tables else list(DEFAULT_TABLES)

    summary: dict[str, Any] = {
        "schema": args.schema,
        "source_col": args.source_col,
        "target_col": args.target_col,
        "tables": tables,
        "check_only": bool(args.check_only),
    }

    with psycopg.connect(dsn) as conn:
        if args.check_only:
            conn.autocommit = True
            report = audit_text_coverage(conn, args.schema, tables, args.source_col, args.target_col)
            exit_code, problems = evaluate_coverage_report(report, coverage_min_pct=args.coverage_min_pct)
            summary["coverage"] = report
            summary["problems"] = problems
            summary["exit_code"] = exit_code
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            for problem in problems:
                logger.warning(problem)
            return exit_code

        updated: dict[str, Any] = {}
        for table in tables:
            if not table_exists(conn, args.schema, table):
                logger.warning("Table absente, ignorée: %s.%s", args.schema, table)
                updated[table] = {"skipped": "missing_table"}
                continue
            missing = [c for c in (args.source_col, args.target_col) if not column_exists(conn, args.schema, table, c)]
            if missing:
                logger.warning("Colonnes absentes sur %s, table ignorée: %s", table, ", ".join(missing))
                updated[table] = {"skipped": "missing_columns", "missing_columns": missing}
                continue
            updated[table] = {
                "updated": backfill_table(
                    conn,
                    args.schema,
                    table,
                    args.source_col,
                    args.target_col,
                    args.limit,
                )
            }
        summary["result"] = updated

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
