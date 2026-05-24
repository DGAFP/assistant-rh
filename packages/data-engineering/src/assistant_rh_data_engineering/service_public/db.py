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
    ) -> int:
        if not rows:
            return 0

        column_types = self._column_types(conn, table)
        rows = self._prepare_rows(rows, column_types)
        if not rows:
            return 0

        cols = list(rows[0].keys())
        vector_cols = {col for col in cols if column_types.get(col, ("", None))[0] == "vector"}
        assignments = [col for col in cols if col not in conflict_cols]

        placeholders = []
        for col in cols:
            if col in vector_cols:
                placeholders.append(
                    sql.SQL("%({})s::vector").format(sql.SQL(col))
                )
            else:
                placeholders.append(sql.SQL("%({})s").format(sql.SQL(col)))

        query = sql.SQL(
            "INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}"
        ).format(
            sql.Identifier(self.schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(col) for col in cols),
            sql.SQL(", ").join(placeholders),
            sql.SQL(", ").join(sql.Identifier(col) for col in conflict_cols),
            sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(
                    sql.Identifier(col),
                    sql.Identifier(col),
                )
                for col in assignments
            ),
        )

        with conn.cursor() as cur:
            cur.executemany(query, rows)
        return len(rows)

    def upsert_documents(self, documents: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            count = self._upsert(conn, "rag_documents", documents, ["doc_id"])
            conn.commit()
            return count

    def upsert_sections(self, sections: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            count = self._upsert(conn, "rag_sections", sections, ["section_id"])
            conn.commit()
            return count

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            count = self._upsert(conn, "rag_chunks_service_public", chunks, ["hash_id"])
            conn.commit()
            return count

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
                raise RuntimeError(
                    f"Column {id_column!r} not found in "
                    f"{self.schema}.{table}."
                )

            where_clauses = [
                sql.SQL("{} IS NOT NULL").format(sql.Identifier(id_column)),
                sql.SQL("{} ~ %s").format(sql.Identifier(id_column)),
            ]
            params: list[Any] = [rf"^{prefix}[0-9]+$"]

            if source_value and "source" in column_types:
                where_clauses.append(
                    sql.SQL("LOWER({}) = %s").format(
                        sql.Identifier("source")
                    )
                )
                params.append(source_value.lower())

            query = sql.SQL(
                "SELECT DISTINCT {} FROM {}.{} WHERE {} ORDER BY {}"
            ).format(
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
