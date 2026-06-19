from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

import psycopg
from assistant_rh_shared import get_dsn
from psycopg import sql
from psycopg.types.json import Jsonb

from ..utils.helpers import vector_to_pgvector


class ServicePublicDbWriter:
    def __init__(self, schema: str = "public", dsn: str | None = None):
        self.schema = schema
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn or get_dsn())

    @staticmethod
    def _normalize_short_ids(short_ids: list[str]) -> list[str]:
        return sorted({str(short_id).strip().upper() for short_id in short_ids if str(short_id).strip()})

    @staticmethod
    def _batched_rows(rows: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
        if batch_size <= 0:
            yield rows
            return
        for index in range(0, len(rows), batch_size):
            yield rows[index : index + batch_size]

    @staticmethod
    def _fit_varchar(value: str, max_length: int | None) -> str:
        if max_length is None or len(value) <= max_length:
            return value
        if max_length <= 8:
            return value[:max_length]

        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        head = value[: max_length - len(digest) - 1].rstrip("_- ")
        return f"{head}_{digest}" if head else digest[:max_length]

    def _column_types(self, conn: psycopg.Connection, table: str) -> dict[str, tuple[str, int | None]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, udt_name, character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (self.schema, table),
            )
            return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    def _index_predicate(self, conn: psycopg.Connection, index_name: str) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_get_expr(indexes.indpred, indexes.indrelid)
                FROM pg_index indexes
                JOIN pg_class index_class ON index_class.oid = indexes.indexrelid
                JOIN pg_namespace namespace ON namespace.oid = index_class.relnamespace
                WHERE index_class.relname = %s
                  AND namespace.nspname = %s
                """,
                (index_name, self.schema),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return str(row[0] or "").strip()

    def _prepare_rows(
        self,
        rows: Iterable[dict[str, Any]],
        column_types: dict[str, tuple[str, int | None]],
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for row in rows:
            out: dict[str, Any] = {}
            for column, value in row.items():
                if column not in column_types:
                    continue
                udt_name, max_length = column_types[column]
                if value is None:
                    out[column] = None
                elif udt_name in {"json", "jsonb"} and isinstance(value, (dict, list)):
                    out[column] = Jsonb(value)
                elif udt_name == "vector" and isinstance(value, list):
                    out[column] = vector_to_pgvector(value)
                elif isinstance(value, str):
                    out[column] = self._fit_varchar(value, max_length)
                elif isinstance(value, (dict, list)):
                    out[column] = json.dumps(value, ensure_ascii=False)
                else:
                    out[column] = value
            prepared.append(out)
        return prepared

    def _upsert(
        self,
        conn: psycopg.Connection,
        table: str,
        rows: list[dict[str, Any]],
        conflict_cols: list[str],
        conflict_where: str | None = None,
        update_exclude_cols: list[str] | None = None,
        preserve_on_null_cols: list[str] | None = None,
    ) -> int:
        if not rows:
            return 0

        column_types = self._column_types(conn, table)
        rows = self._prepare_rows(rows, column_types)
        if not rows:
            return 0

        cols = list(rows[0].keys())
        vector_cols = {col for col in cols if column_types.get(col, ("", None))[0] == "vector"}
        update_exclude = set(update_exclude_cols or [])
        preserve_on_null = set(preserve_on_null_cols or [])
        assignments = [col for col in cols if col not in conflict_cols and col not in update_exclude]
        preserve_on_null.intersection_update(assignments)

        placeholders = []
        for col in cols:
            if col in vector_cols:
                placeholders.append(sql.SQL("%({})s::vector").format(sql.SQL(col)))
            else:
                placeholders.append(sql.SQL("%({})s").format(sql.SQL(col)))

        conflict_where_sql = sql.SQL("")
        if conflict_where:
            # conflict_where ne doit venir que du catalogue Postgres (pg_get_expr
            # via _index_predicate), jamais d'une entrée utilisateur: il est
            # injecté tel quel dans la requête.
            conflict_where_sql = sql.SQL(" WHERE ") + sql.SQL(conflict_where)

        if assignments:
            assignment_exprs: list[sql.Composable] = []
            for col in assignments:
                col_id = sql.Identifier(col)
                if col in preserve_on_null:
                    assignment_exprs.append(
                        sql.SQL("{} = COALESCE(EXCLUDED.{}, {}.{})").format(
                            col_id,
                            col_id,
                            sql.Identifier(table),
                            col_id,
                        )
                    )
                else:
                    assignment_exprs.append(sql.SQL("{} = EXCLUDED.{}").format(col_id, col_id))
            conflict_action_sql = sql.SQL("DO UPDATE SET {}").format(sql.SQL(", ").join(assignment_exprs))
        else:
            conflict_action_sql = sql.SQL("DO NOTHING")

        query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT ({}){} {}").format(
            sql.Identifier(self.schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(col) for col in cols),
            sql.SQL(", ").join(placeholders),
            sql.SQL(", ").join(sql.Identifier(col) for col in conflict_cols),
            conflict_where_sql,
            conflict_action_sql,
        )

        with conn.cursor() as cur:
            cur.executemany(query, rows)
        return len(rows)

    def upsert_documents(self, documents: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            short_id_predicate = self._index_predicate(conn, "uq_rag_documents_short_id")
            if short_id_predicate is not None:
                # L'arbitre partiel sur short_id suppose que chaque ligne a un
                # short_id non NULL: une ligne sans short_id ne matcherait pas
                # l'index et lèverait une violation de PK sur doc_id en cas de
                # re-run au lieu d'être mise à jour.
                count = self._upsert(
                    conn,
                    "rag_documents",
                    documents,
                    ["short_id"],
                    conflict_where=short_id_predicate or None,
                    update_exclude_cols=["doc_id"],
                )
            else:
                count = self._upsert(conn, "rag_documents", documents, ["doc_id"])
            conn.commit()
            return count

    def upsert_sections(self, sections: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            doc_index_predicate = self._index_predicate(conn, "uq_rag_sections_doc_index")
            if doc_index_predicate is not None:
                # Même contrainte que pour les documents: une section avec
                # section_index NULL ne matcherait pas l'arbitre (doc_id,
                # section_index) et lèverait une violation de PK en re-run.
                count = self._upsert(
                    conn,
                    "rag_sections",
                    sections,
                    ["doc_id", "section_index"],
                    conflict_where=doc_index_predicate or None,
                    update_exclude_cols=["section_id"],
                )
            else:
                count = self._upsert(conn, "rag_sections", sections, ["section_id"])
            conn.commit()
            return count

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            count = self._upsert(conn, "rag_chunks_service_public", chunks, ["hash_id"])
            conn.commit()
            return count

    def _delete_chunks_by_short_ids(
        self,
        conn: psycopg.Connection,
        short_ids: list[str],
        table: str = "rag_chunks_service_public",
    ) -> int:
        normalized_short_ids = self._normalize_short_ids(short_ids)
        if not normalized_short_ids:
            return 0

        with conn.cursor() as cur:
            query = sql.SQL(
                """
                DELETE FROM {}.{}
                WHERE UPPER(TRIM(short_id)) = ANY(%s)
                """
            ).format(sql.Identifier(self.schema), sql.Identifier(table))
            cur.execute(query, (normalized_short_ids,))
            return int(cur.rowcount or 0)

    def delete_chunks_by_short_ids(
        self,
        short_ids: list[str],
        table: str = "rag_chunks_service_public",
    ) -> int:
        with self._connect() as conn:
            deleted = self._delete_chunks_by_short_ids(conn, short_ids, table=table)
            conn.commit()
            return deleted

    def replace_chunks_by_short_ids(
        self,
        short_ids: list[str],
        chunks: list[dict[str, Any]],
        *,
        batch_size: int = 1000,
        table: str = "rag_chunks_service_public",
    ) -> tuple[int, int]:
        """Delete targeted Service-Public chunks and insert replacements atomically."""
        with self._connect() as conn:
            deleted = self._delete_chunks_by_short_ids(conn, short_ids, table=table)
            inserted = 0
            for batch in self._batched_rows(chunks, batch_size):
                inserted += self._upsert(conn, table, batch, ["hash_id"])
            conn.commit()
            return deleted, inserted

    def list_fiche_ids(
        self,
        table: str = "rag_chunks_service_public",
        id_column: str = "short_id",
        *,
        prefix: str = "F",
        limit: Optional[int] = None,
        source_value: Optional[str] = None,
    ) -> list[str]:
        """
        Return distinct Service-Public fiche IDs already present in a DB table.

        Typical use cases:
          - ``rag_chunks_service_public.short_id``
          - ``rag_documents.short_id`` with ``source = 'service_public'``
        """
        with self._connect() as conn, conn.cursor() as cur:
            column_types = self._column_types(conn, table)
            if id_column not in column_types:
                raise RuntimeError(f"Column {id_column!r} not found in {self.schema}.{table}.")

            where_clauses = [
                sql.SQL("{} IS NOT NULL").format(sql.Identifier(id_column)),
                sql.SQL("{} ~ %s").format(sql.Identifier(id_column)),
            ]
            params: list[Any] = [rf"^{prefix}[0-9]+$"]

            if source_value and "source" in column_types:
                where_clauses.append(sql.SQL("LOWER({}) = %s").format(sql.Identifier("source")))
                params.append(source_value.lower())

            query = sql.SQL("SELECT DISTINCT {} FROM {}.{} WHERE {} ORDER BY {}").format(
                sql.Identifier(id_column),
                sql.Identifier(self.schema),
                sql.Identifier(table),
                sql.SQL(" AND ").join(where_clauses),
                sql.Identifier(id_column),
            )

            if limit:
                query += sql.SQL(" LIMIT %s")
                params.append(limit)

            cur.execute(query, params)
            return [row[0] for row in cur.fetchall()]

    def list_document_ids_by_short_id(self, short_ids: list[str]) -> dict[str, str]:
        normalized_short_ids = sorted({str(short_id).strip().upper() for short_id in short_ids if str(short_id).strip()})
        if not normalized_short_ids:
            return {}

        with self._connect() as conn, conn.cursor() as cur:
            query = sql.SQL(
                """
                SELECT short_id, doc_id
                FROM {}.{}
                WHERE UPPER(TRIM(short_id)) = ANY(%s)
                  AND short_id IS NOT NULL
                """
            ).format(sql.Identifier(self.schema), sql.Identifier("rag_documents"))
            cur.execute(query, (normalized_short_ids,))
            return {str(row[0]).strip().upper(): str(row[1]) for row in cur.fetchall()}

    def list_section_ids_by_doc_id_and_index(self, doc_ids: list[str]) -> dict[tuple[str, int], str]:
        if not doc_ids:
            return {}

        with self._connect() as conn, conn.cursor() as cur:
            query = sql.SQL(
                """
                SELECT doc_id, section_index, section_id
                FROM {}.{}
                WHERE doc_id = ANY(%s::uuid[])
                  AND section_index IS NOT NULL
                """
            ).format(sql.Identifier(self.schema), sql.Identifier("rag_sections"))
            cur.execute(query, (doc_ids,))
            return {(str(row[0]), int(row[1])): str(row[2]) for row in cur.fetchall()}

    def fetch_service_public_chunks(
        self,
        short_ids: list[str],
        table: str = "rag_chunks_service_public",
    ) -> list[dict[str, Any]]:
        """Read existing Service-Public chunks from DB for a set of short_ids."""
        if not short_ids:
            return []

        with self._connect() as conn, conn.cursor() as cur:
            query = sql.SQL(
                """
                SELECT
                    hash_id,
                    qa_id,
                    parent_qa_id,
                    source_name,
                    section_path,
                    role,
                    chunk_index,
                    text,
                    chunk_text,
                    lang,
                    thematique,
                    source,
                    short_id,
                    references_juridiques,
                    section_id,
                    source_document_id
                FROM {}.{}
                WHERE short_id = ANY(%s)
                ORDER BY short_id, qa_id NULLS FIRST, role, chunk_index
                """
            ).format(sql.Identifier(self.schema), sql.Identifier(table))
            cur.execute(query, (short_ids,))
            columns = [desc.name for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
