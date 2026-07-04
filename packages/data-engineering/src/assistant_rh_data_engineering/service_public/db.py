from __future__ import annotations

from typing import Any, Optional

from psycopg import sql

from ..utils.db import RagDbWriter


class ServicePublicDbWriter(RagDbWriter):
    """Writer Service-Public: RagDbWriter ciblé sur rag_chunks_service_public
    plus les helpers de lecture spécifiques aux fiches (Fxxxxx)."""

    def __init__(self, schema: str = "public", dsn: str | None = None):
        super().__init__(schema=schema, dsn=dsn, chunk_table="rag_chunks_service_public")

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
