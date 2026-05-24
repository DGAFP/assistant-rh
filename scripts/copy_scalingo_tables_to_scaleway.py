from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


STREAMLIT_CORE_TABLES = [
    "acronyms",
    "acronyms_missing",
    "chat_feedbacks",
    "chat_reviews",
    "chat_runs",
    "documents",
    "rag_chunks_matte",
    "rag_chunks_rgrh",
    "rag_config",
    "rag_documents",
    "rag_sections",
    "system_prompts",
]

STREAMLIT_EVAL_TABLES = [
    "goldset_questions_v2",
    "goldset_runs",
    "intent_eval_experiments",
    "intent_eval_goldset",
    "pipeline_eval_experiments",
    "rag_chunk_embeddings",
    "rag_chunks_test",
    "retrieval_eval_runs",
]

ALL_NON_SCW_TABLES = [
    "acronyms",
    "acronyms_missing",
    "chat_feedbacks",
    "chat_reviews",
    "chat_runs",
    "documents",
    "goldset_questions_v2",
    "goldset_runs",
    "intent_eval_experiments",
    "intent_eval_goldset",
    "pipeline_eval_experiments",
    "rag_chunk_embeddings",
    "rag_chunks_matte",
    "rag_chunks_rgrh",
    "rag_chunks_test",
    "rag_config",
    "rag_documents",
    "rag_sections",
    "retrieval_eval_runs",
    "system_prompts",
]

TABLE_PROFILES = {
    "none": [],
    "streamlit-core": STREAMLIT_CORE_TABLES,
    "streamlit-eval": STREAMLIT_EVAL_TABLES,
    "streamlit-full": STREAMLIT_CORE_TABLES + STREAMLIT_EVAL_TABLES,
    "all-non-scw": ALL_NON_SCW_TABLES,
}

UPSERT_IN_PLACE_TABLES = {
    "documents",
    "rag_config",
    "rag_documents",
    "rag_sections",
}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type_sql: str
    not_null: bool
    default_expr: str | None
    is_generated: bool
    is_identity: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copie des tables PostgreSQL de Scalingo vers Scaleway pour rendre "
            "la Streamlit exploitable directement sur la DB Scaleway."
        )
    )
    parser.add_argument(
        "--profile",
        choices=sorted(TABLE_PROFILES),
        default="streamlit-core",
        help="Profil de tables a migrer.",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=[],
        help="Liste additionnelle de tables a migrer.",
    )
    parser.add_argument(
        "--source-dsn-env",
        default="SCALINGO_TUNNEL_DSN",
        help="Variable d'environnement DSN source si deja resolue.",
    )
    parser.add_argument(
        "--target-dsn-env",
        default="SCW_POSTGRES_DSN",
        help="Variable d'environnement DSN cible.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Nombre de lignes ecrites par lot.",
    )
    parser.add_argument(
        "--truncate-first",
        action="store_true",
        help="Vide les tables cibles avant recharge.",
    )
    parser.add_argument(
        "--skip-indexes",
        action="store_true",
        help="N'aligne pas les index non-PK de la table source.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Affiche le plan sans copier de donnees.",
    )
    return parser


def resolve_scalingo_tunnel_dsn(explicit: str | None, env: dict[str, str]) -> str:
    if explicit:
        return explicit
    value = env.get("SCALINGO_TUNNEL_DSN", "").strip()
    if value:
        return value

    required = ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"]
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(
            "Impossible de resoudre le DSN Scalingo via le tunnel local. "
            f"Variables manquantes: {', '.join(missing)}"
        )

    return (
        f"postgresql://{quote(env['PGUSER'])}:{quote(env['PGPASSWORD'])}"
        f"@{env['PGHOST']}:{env['PGPORT']}/{env['PGDATABASE']}?sslmode=prefer"
    )


def resolve_dsn(env_name: str, env: dict[str, str]) -> str:
    value = env.get(env_name, "").strip()
    if not value:
        raise RuntimeError(f"Variable d'environnement manquante: {env_name}")
    return value


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_ref(table_name: str) -> str:
    return f'"public".{quote_ident(table_name)}'


def table_name_text(table_name: str) -> str:
    return f"public.{table_name}"


def resolve_target_table_name(source_table_name: str, target_tables: set[str]) -> str:
    if source_table_name in UPSERT_IN_PLACE_TABLES:
        return source_table_name
    suffixed = f"{source_table_name}_scalingo"
    if suffixed in target_tables:
        return suffixed
    if source_table_name in target_tables:
        return suffixed
    return source_table_name


def get_public_tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
        """
    ).fetchall()
    return {row["table_name"] for row in rows}


def get_row_count(conn: psycopg.Connection, table_name: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS row_count FROM {table_ref(table_name)}"
    ).fetchone()
    return int(row["row_count"])


def get_primary_key_columns(conn: psycopg.Connection, table_name: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(c.conkey) WITH ORDINALITY AS cols(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = cols.attnum
        WHERE c.contype = 'p'
          AND n.nspname = 'public'
          AND t.relname = %s
        ORDER BY cols.ord
        """,
        (table_name,),
    ).fetchall()
    return [row["attname"] for row in rows]


def get_columns(conn: psycopg.Connection, table_name: str) -> list[ColumnSpec]:
    rows = conn.execute(
        """
        SELECT
            a.attname,
            format_type(a.atttypid, a.atttypmod) AS type_sql,
            a.attnotnull,
            pg_get_expr(ad.adbin, ad.adrelid) AS default_expr,
            a.attgenerated = 's' AS is_generated,
            a.attidentity IN ('a', 'd') AS is_identity
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (table_name,),
    ).fetchall()
    return [
        ColumnSpec(
            name=row["attname"],
            type_sql=row["type_sql"],
            not_null=bool(row["attnotnull"]),
            default_expr=row["default_expr"],
            is_generated=bool(row["is_generated"]),
            is_identity=bool(row["is_identity"]),
        )
        for row in rows
    ]


def get_indexes(conn: psycopg.Connection, table_name: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
          AND indexname NOT LIKE %s
        ORDER BY indexname
        """,
        (table_name, f"{table_name}_pkey"),
    ).fetchall()
    return [(row["indexname"], row["indexdef"]) for row in rows]


def build_doc_id_remap(
    source_conn: psycopg.Connection,
    target_conn: psycopg.Connection,
) -> dict[str, str]:
    """Map Scalingo doc_id -> existing Scaleway doc_id when short_id already exists."""
    source_tables = get_public_tables(source_conn)
    target_tables = get_public_tables(target_conn)
    if "rag_documents" not in source_tables or "rag_documents" not in target_tables:
        return {}

    source_rows = source_conn.execute(
        """
        SELECT doc_id::text AS doc_id, short_id
        FROM public.rag_documents
        WHERE short_id IS NOT NULL
        """
    ).fetchall()
    target_rows = target_conn.execute(
        """
        SELECT doc_id::text AS doc_id, short_id
        FROM public.rag_documents
        WHERE short_id IS NOT NULL
        """
    ).fetchall()

    target_by_short = {row["short_id"]: row["doc_id"] for row in target_rows}
    remap: dict[str, str] = {}
    for row in source_rows:
        target_doc_id = target_by_short.get(row["short_id"])
        if target_doc_id and target_doc_id != row["doc_id"]:
            remap[row["doc_id"]] = target_doc_id
    return remap


def column_definition(column: ColumnSpec, *, include_not_null: bool = True) -> str:
    if column.is_generated and column.default_expr:
        return (
            f"{quote_ident(column.name)} {column.type_sql} GENERATED ALWAYS AS "
            f"({column.default_expr}) STORED"
        )

    parts = [quote_ident(column.name), column.type_sql]
    default_is_sequence = bool(column.default_expr and column.default_expr.startswith("nextval("))
    if default_is_sequence and column.type_sql in {"integer", "bigint", "smallint"}:
        parts.append("GENERATED BY DEFAULT AS IDENTITY")
    elif column.default_expr and not column.is_identity:
        parts.append(f"DEFAULT {column.default_expr}")
    if include_not_null and column.not_null:
        parts.append("NOT NULL")
    return " ".join(parts)


def ensure_table_schema(
    source_conn: psycopg.Connection,
    target_conn: psycopg.Connection,
    source_table_name: str,
    target_table_name: str,
    *,
    sync_indexes: bool,
) -> dict[str, Any]:
    source_columns = get_columns(source_conn, source_table_name)
    pk_columns = get_primary_key_columns(source_conn, source_table_name)
    target_tables = get_public_tables(target_conn)
    created = False
    added_columns: list[str] = []

    if target_table_name not in target_tables:
        column_sql = ",\n                ".join(column_definition(col) for col in source_columns)
        pk_sql = (
            ",\n                PRIMARY KEY ("
            + ", ".join(quote_ident(col) for col in pk_columns)
            + ")"
            if pk_columns
            else ""
        )
        target_conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_ref(target_table_name)} (
                {column_sql}{pk_sql}
            )
            """
        )
        created = True
    else:
        target_columns = {col.name for col in get_columns(target_conn, target_table_name)}
        for column in source_columns:
            if column.name in target_columns:
                continue
            target_conn.execute(
                f"ALTER TABLE {table_ref(target_table_name)} "
                f"ADD COLUMN {column_definition(column, include_not_null=False)}"
            )
            added_columns.append(column.name)

        target_pk = get_primary_key_columns(target_conn, target_table_name)
        if pk_columns and not target_pk:
            target_conn.execute(
                f"ALTER TABLE {table_ref(target_table_name)} "
                f"ADD PRIMARY KEY ({', '.join(quote_ident(col) for col in pk_columns)})"
            )

    created_indexes: list[str] = []
    if sync_indexes:
        target_index_names = {
            row["indexname"]
            for row in target_conn.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = %s
                """,
                (target_table_name,),
            ).fetchall()
        }
        for index_name, index_def in get_indexes(source_conn, source_table_name):
            target_index_name = index_name.replace(source_table_name, target_table_name, 1)
            if target_index_name in target_index_names:
                continue
            try:
                target_conn.execute(
                    index_def.replace(index_name, target_index_name, 1).replace(
                        f" ON public.{source_table_name} ",
                        f" ON public.{target_table_name} ",
                    )
                )
                created_indexes.append(target_index_name)
            except (psycopg.errors.DuplicateTable, psycopg.errors.DuplicateObject):
                continue

    return {
        "created": created,
        "added_columns": added_columns,
        "created_indexes": created_indexes,
        "primary_key": pk_columns,
        "columns": source_columns,
    }


def serialize_value(value: Any, type_sql: str) -> Any:
    if value is None:
        return None
    lowered = type_sql.lower()
    if lowered in {"json", "jsonb"}:
        return json.dumps(value, ensure_ascii=False)
    return value


def build_select_sql(table_name: str, columns: list[ColumnSpec]) -> str:
    names = ", ".join(quote_ident(col.name) for col in columns if not col.is_generated)
    sql = f"SELECT {names} FROM {table_ref(table_name)}"
    if table_name == "rag_sections":
        return (
            sql
            + " ORDER BY "
            + quote_ident("parent_section_id")
            + " NULLS FIRST, "
            + quote_ident("section_index")
            + ", "
            + quote_ident("section_id")
        )
    return sql


def build_insert_sql(table_name: str, columns: list[ColumnSpec], pk_columns: list[str]) -> str:
    insertable = [col for col in columns if not col.is_generated]
    names = [col.name for col in insertable]
    values = []
    for col in insertable:
        lowered = col.type_sql.lower()
        if lowered.startswith("vector"):
            values.append(f"%({col.name})s::vector")
        elif lowered in {"json", "jsonb"}:
            values.append(f"%({col.name})s::{col.type_sql}")
        else:
            values.append(f"%({col.name})s")

    sql = (
        f"INSERT INTO {table_ref(table_name)} "
        f"({', '.join(quote_ident(name) for name in names)}) "
        f"VALUES ({', '.join(values)})"
    )
    if not pk_columns:
        return sql

    update_cols = [name for name in names if name not in pk_columns]
    if not update_cols:
        return (
            sql
            + " ON CONFLICT ("
            + ", ".join(quote_ident(col) for col in pk_columns)
            + ") DO NOTHING"
        )

    assignments = ", ".join(
        f"{quote_ident(name)} = EXCLUDED.{quote_ident(name)}"
        for name in update_cols
    )
    return (
        sql
        + " ON CONFLICT ("
        + ", ".join(quote_ident(col) for col in pk_columns)
        + f") DO UPDATE SET {assignments}"
    )


def copy_table_data(
    source_conn: psycopg.Connection,
    target_conn: psycopg.Connection,
    source_table_name: str,
    target_table_name: str,
    columns: list[ColumnSpec],
    pk_columns: list[str],
    doc_id_remap: dict[str, str],
    *,
    batch_size: int,
    truncate_first: bool,
) -> dict[str, Any]:
    if truncate_first:
        target_conn.execute(f"TRUNCATE TABLE {table_ref(target_table_name)}")

    rows = source_conn.execute(build_select_sql(source_table_name, columns)).fetchall()
    insert_sql = build_insert_sql(target_table_name, columns, pk_columns)
    prepared_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = {}
        for column in columns:
            if column.is_generated:
                continue
            payload[column.name] = serialize_value(row[column.name], column.type_sql)

        original_doc_id = payload.get("doc_id")
        original_doc_id_str = str(original_doc_id) if original_doc_id is not None else None
        if source_table_name == "rag_sections" and original_doc_id_str in doc_id_remap:
            continue
        if source_table_name == "rag_sections":
            payload["parent_section_id"] = None

        if source_table_name == "rag_documents" and payload.get("doc_id") in doc_id_remap:
            payload["doc_id"] = doc_id_remap[payload["doc_id"]]

        for key in ("doc_id", "source_document_id"):
            value = payload.get(key)
            if value is None:
                continue
            normalized = str(value)
            if normalized in doc_id_remap:
                payload[key] = doc_id_remap[normalized]
        prepared_rows.append(payload)

    for batch in chunked(prepared_rows, batch_size):
        if batch:
            with target_conn.cursor() as cur:
                for row_payload in batch:
                    cur.execute(insert_sql, row_payload)

    # Reset serial/identity sequences after explicit id copy.
    for column in columns:
        if not column.default_expr or "nextval(" not in column.default_expr:
            continue
        target_conn.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence(%s, %s),
                COALESCE(
                    (SELECT MAX({quote_ident(column.name)}) FROM {table_ref(target_table_name)}),
                    1
                ),
                true
            )
            """,
            (table_name_text(target_table_name), column.name),
        )

    return {
        "source_rows": len(rows),
        "target_rows": get_row_count(target_conn, target_table_name),
    }


def list_tables_for_run(profile: str, extra_tables: list[str]) -> list[str]:
    tables = list(TABLE_PROFILES[profile])
    for table in extra_tables:
        if table.endswith("_scw"):
            continue
        if table not in tables:
            tables.append(table)
    return tables


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    env = {**os.environ}

    source_dsn = resolve_scalingo_tunnel_dsn(env.get(args.source_dsn_env), env)
    target_dsn = resolve_dsn(args.target_dsn_env, env)
    tables = list_tables_for_run(args.profile, args.tables)

    summary: dict[str, Any] = {
        "profile": args.profile,
        "tables": tables,
        "plan_only": args.plan_only,
        "truncate_first": args.truncate_first,
    }

    with psycopg.connect(source_dsn, row_factory=dict_row) as source_conn, psycopg.connect(
        target_dsn,
        row_factory=dict_row,
        autocommit=True,
    ) as target_conn:
        source_tables = get_public_tables(source_conn)
        target_tables = get_public_tables(target_conn)
        doc_id_remap = build_doc_id_remap(source_conn, target_conn)
        summary["source_table_count"] = len(source_tables)
        summary["target_table_count_before"] = len(target_tables)
        summary["doc_id_remap_count"] = len(doc_id_remap)
        table_results: dict[str, Any] = {}

        for table_name in tables:
            if table_name not in source_tables:
                table_results[table_name] = {"status": "missing_on_source"}
                continue

            target_table_name = resolve_target_table_name(table_name, target_tables)
            source_rows = get_row_count(source_conn, table_name)
            target_rows_before = (
                get_row_count(target_conn, target_table_name)
                if target_table_name in target_tables
                else 0
            )

            if args.plan_only:
                table_results[table_name] = {
                    "status": "planned",
                    "target_table": target_table_name,
                    "source_rows": source_rows,
                    "target_rows_before": target_rows_before,
                    "target_exists": target_table_name in target_tables,
                }
                continue

            schema_result = ensure_table_schema(
                source_conn,
                target_conn,
                table_name,
                target_table_name,
                sync_indexes=not args.skip_indexes,
            )
            data_result = copy_table_data(
                source_conn,
                target_conn,
                table_name,
                target_table_name,
                schema_result["columns"],
                schema_result["primary_key"],
                doc_id_remap,
                batch_size=args.batch_size,
                truncate_first=args.truncate_first,
            )
            target_tables.add(target_table_name)

            table_results[table_name] = {
                "status": "copied",
                "target_table": target_table_name,
                "source_rows": source_rows,
                "target_rows_before": target_rows_before,
                "target_rows_after": data_result["target_rows"],
                "created": schema_result["created"],
                "added_columns": schema_result["added_columns"],
                "created_indexes": schema_result["created_indexes"],
                "primary_key": schema_result["primary_key"],
            }

    summary["tables_result"] = table_results
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
