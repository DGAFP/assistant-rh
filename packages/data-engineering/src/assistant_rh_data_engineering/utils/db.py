from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import psycopg
from assistant_rh_shared import get_dsn
from psycopg import sql
from psycopg.types.json import Jsonb

from .helpers import vector_to_pgvector


class RagDbWriter:
    """Writer générique pour les tables RAG (rag_documents, rag_sections, rag_chunks_*).

    Extrait de ServicePublicDbWriter : la logique d'upsert/suppression est
    identique pour toutes les sources ; seuls la table de chunks cible et les
    helpers spécifiques restent dans les sous-classes par source.
    """

    def __init__(
        self,
        schema: str = "public",
        dsn: str | None = None,
        chunk_table: str | None = None,
    ):
        self.schema = schema
        self.dsn = dsn
        self.chunk_table = chunk_table

    def _connect(self):
        return psycopg.connect(self.dsn or get_dsn())

    def _require_chunk_table(self, table: str | None) -> str:
        resolved = table or self.chunk_table
        if not resolved:
            raise ValueError("Aucune table de chunks: passer table=... ou définir chunk_table à la construction.")
        return resolved

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

        # Preserve legacy deterministic suffixes used by persisted identifiers; this is not security hashing.
        # codeql[py/weak-sensitive-data-hashing]
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
            columns = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            if not columns:
                raise RuntimeError(f"La table {self.schema}.{table} n'existe pas ou n'a aucune colonne.")
            return columns

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

    def _upsert_documents(self, conn: psycopg.Connection, documents: list[dict[str, Any]]) -> int:
        short_id_predicate = self._index_predicate(conn, "uq_rag_documents_short_id")
        if short_id_predicate is not None:
            # L'arbitre partiel sur short_id suppose que chaque ligne a un
            # short_id non NULL: une ligne sans short_id ne matcherait pas
            # l'index et lèverait une violation de PK sur doc_id en cas de
            # re-run au lieu d'être mise à jour.
            return self._upsert(
                conn,
                "rag_documents",
                documents,
                ["short_id"],
                conflict_where=short_id_predicate or None,
                update_exclude_cols=["doc_id"],
            )
        return self._upsert(conn, "rag_documents", documents, ["doc_id"])

    def upsert_documents(self, documents: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            count = self._upsert_documents(conn, documents)
            conn.commit()
            return count

    def _upsert_sections(self, conn: psycopg.Connection, sections: list[dict[str, Any]]) -> int:
        doc_index_predicate = self._index_predicate(conn, "uq_rag_sections_doc_index")
        if doc_index_predicate is not None:
            # Même contrainte que pour les documents: une section avec
            # section_index NULL ne matcherait pas l'arbitre (doc_id,
            # section_index) et lèverait une violation de PK en re-run.
            return self._upsert(
                conn,
                "rag_sections",
                sections,
                ["doc_id", "section_index"],
                conflict_where=doc_index_predicate or None,
                update_exclude_cols=["section_id"],
            )
        return self._upsert(conn, "rag_sections", sections, ["section_id"])

    def upsert_sections(self, sections: list[dict[str, Any]]) -> int:
        with self._connect() as conn:
            count = self._upsert_sections(conn, sections)
            conn.commit()
            return count

    def ingest_document_bundle(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        *,
        batch_size: int = 1000,
        table: str | None = None,
    ) -> dict[str, int]:
        """Document + sections + remplacement des chunks en UNE transaction.

        Sans cette atomicité, un échec après l'upsert du document laisse en
        base un checksum à jour avec des chunks périmés: le run suivant
        classerait le document ignore_inchange sans jamais le réparer
        (réconciliation par delta sha256 des pipelines PDF).
        """
        resolved_table = self._require_chunk_table(table)
        short_id = str(document.get("short_id") or "").strip().upper()
        doc_id = str(document.get("doc_id") or "")
        with self._connect() as conn:
            documents_count = self._upsert_documents(conn, [document])
            # L'arbitre d'upsert sur short_id PRÉSERVE le doc_id existant
            # (update_exclude_cols=["doc_id"]): si un document homonyme
            # préexistait en base (ligne legacy, autre pipeline), le doc_id
            # généré par le bundle diverge du doc_id réellement retenu — les
            # sections et chunks référenceraient alors un document fantôme.
            # On réaligne tout le bundle sur le doc_id canonique post-upsert.
            canonical_doc_id = self._canonical_doc_id(conn, short_id) if short_id else None
            if canonical_doc_id and doc_id and canonical_doc_id != doc_id:
                doc_id = canonical_doc_id
                for section in sections:
                    section["doc_id"] = canonical_doc_id
                for chunk in chunks:
                    if "source_document_id" in chunk:
                        chunk["source_document_id"] = canonical_doc_id
            # Delete-puis-insert des sections: un document raccourci laisserait
            # sinon ses sections de queue (section_index au-delà du nouveau
            # compte) avec le contenu de la version précédente.
            if doc_id:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DELETE FROM {}.{} WHERE doc_id = %s::uuid").format(sql.Identifier(self.schema), sql.Identifier("rag_sections")),
                        (doc_id,),
                    )
            sections_count = self._upsert_sections(conn, sections) if sections else 0
            deleted = self._delete_chunks_by_short_ids(conn, [short_id], table=resolved_table) if short_id else 0
            inserted = 0
            for batch in self._batched_rows(chunks, batch_size):
                inserted += self._upsert(conn, resolved_table, batch, ["hash_id"])
            conn.commit()
            return {
                "documents": documents_count,
                "sections": sections_count,
                "chunks_deleted": deleted,
                "chunks": inserted,
            }

    def upsert_chunks(self, chunks: list[dict[str, Any]], table: str | None = None) -> int:
        resolved_table = self._require_chunk_table(table)
        with self._connect() as conn:
            count = self._upsert(conn, resolved_table, chunks, ["hash_id"])
            conn.commit()
            return count

    def _delete_chunks_by_short_ids(
        self,
        conn: psycopg.Connection,
        short_ids: list[str],
        table: str | None = None,
    ) -> int:
        resolved_table = self._require_chunk_table(table)
        normalized_short_ids = self._normalize_short_ids(short_ids)
        if not normalized_short_ids:
            return 0

        with conn.cursor() as cur:
            query = sql.SQL(
                """
                DELETE FROM {}.{}
                WHERE UPPER(TRIM(short_id)) = ANY(%s)
                  AND short_id IS NOT NULL
                """
            ).format(sql.Identifier(self.schema), sql.Identifier(resolved_table))
            cur.execute(query, (normalized_short_ids,))
            return int(cur.rowcount or 0)

    def _delete_chunks_by_doc_ids(
        self,
        conn: psycopg.Connection,
        doc_ids: list[str],
        table: str | None = None,
    ) -> int:
        resolved_table = self._require_chunk_table(table)
        if not doc_ids:
            return 0

        with conn.cursor() as cur:
            query = sql.SQL(
                """
                DELETE FROM {}.{}
                WHERE source_document_id = ANY(%s::uuid[])
                """
            ).format(sql.Identifier(self.schema), sql.Identifier(resolved_table))
            cur.execute(query, (doc_ids,))
            return int(cur.rowcount or 0)

    def _canonical_doc_id(self, conn: psycopg.Connection, short_id: str) -> str | None:
        """doc_id réellement en base pour un short_id, dans la transaction courante.

        Retourne None si le short_id est absent ou ambigu (plusieurs lignes,
        schéma sans index unique): dans ces cas on ne devine pas et le bundle
        garde son doc_id généré.
        """
        with conn.cursor() as cur:
            query = sql.SQL(
                """
                SELECT doc_id
                FROM {}.{}
                WHERE UPPER(TRIM(short_id)) = %s
                  AND short_id IS NOT NULL
                """
            ).format(sql.Identifier(self.schema), sql.Identifier("rag_documents"))
            cur.execute(query, (short_id,))
            rows = cur.fetchall()
        if len(rows) == 1:
            return str(rows[0][0])
        return None

    def delete_chunks_by_short_ids(
        self,
        short_ids: list[str],
        table: str | None = None,
    ) -> int:
        with self._connect() as conn:
            deleted = self._delete_chunks_by_short_ids(conn, short_ids, table=table)
            conn.commit()
            return deleted

    def delete_chunks_not_in_short_ids(
        self,
        short_ids_to_keep: list[str],
        table: str | None = None,
    ) -> int:
        """Balaye les chunks orphelins du manifest (short_id NULL ou inconnu).

        Utilisé par la réconciliation corpus complet: les lignes legacy
        backfillées par une migration de bascule (rétention du retrieval
        pendant la reconstruction) portent des short_ids notebooks absents du
        manifest Grist et pas toujours de source_document_id — la cascade
        documentaire ne les couvre donc pas toutes.

        Garde anti-wipe: refuse une liste vide (un manifest vide balayerait
        la table entière).
        """
        resolved_table = self._require_chunk_table(table)
        keep = self._normalize_short_ids(short_ids_to_keep)
        if not keep:
            raise ValueError("liste de short_ids à conserver vide: refus de balayer la table entière (garde anti-wipe).")
        with self._connect() as conn:
            with conn.cursor() as cur:
                query = sql.SQL(
                    """
                    DELETE FROM {}.{}
                    WHERE short_id IS NULL
                       OR UPPER(TRIM(short_id)) <> ALL(%s)
                    """
                ).format(sql.Identifier(self.schema), sql.Identifier(resolved_table))
                cur.execute(query, (keep,))
                deleted = int(cur.rowcount or 0)
            conn.commit()
            return deleted

    def replace_chunks_by_short_ids(
        self,
        short_ids: list[str],
        chunks: list[dict[str, Any]],
        *,
        batch_size: int = 1000,
        table: str | None = None,
    ) -> tuple[int, int]:
        """Delete targeted chunks and insert replacements atomically."""
        resolved_table = self._require_chunk_table(table)
        with self._connect() as conn:
            deleted = self._delete_chunks_by_short_ids(conn, short_ids, table=resolved_table)
            inserted = 0
            for batch in self._batched_rows(chunks, batch_size):
                inserted += self._upsert(conn, resolved_table, batch, ["hash_id"])
            conn.commit()
            return deleted, inserted

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

    def list_short_ids_with_checksum(
        self,
        source: str,
        table: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """État courant du corpus d'une source: short_id -> {doc_id, checksum, nb_chunks}.

        Sert d'entrée à la réconciliation manifest vs corpus. La colonne
        checksum de rag_documents peut ne pas exister sur un schéma ancien:
        dans ce cas checksum vaut None et l'appelant traite le document comme
        modifié (ré-ingestion sûre).
        """
        resolved_table = self._require_chunk_table(table)
        with self._connect() as conn, conn.cursor() as cur:
            document_columns = self._column_types(conn, "rag_documents")
            has_checksum = "checksum" in document_columns
            checksum_expr = sql.Identifier("documents", "checksum") if has_checksum else sql.SQL("NULL")
            # page_vision_complete (quality_flags JSONB): 'false' force le
            # re-traitement (revue #320 finding 1). Absent/NULL -> traité comme
            # complet (ne pas re-traiter en masse les docs legacy sans le drapeau).
            has_quality_flags = "quality_flags" in document_columns
            pv_expr = sql.SQL("documents.quality_flags->>'page_vision_complete'") if has_quality_flags else sql.SQL("NULL")

            query = sql.SQL(
                """
                SELECT
                    documents.short_id,
                    documents.doc_id,
                    {} AS checksum,
                    COALESCE(chunk_counts.nb_chunks, 0) AS nb_chunks,
                    {} AS page_vision_complete
                FROM {}.{} AS documents
                LEFT JOIN (
                    SELECT short_id, COUNT(*) AS nb_chunks
                    FROM {}.{}
                    GROUP BY short_id
                ) AS chunk_counts
                  ON UPPER(TRIM(chunk_counts.short_id)) = UPPER(TRIM(documents.short_id))
                WHERE LOWER(TRIM(documents.source)) = %s
                  AND documents.short_id IS NOT NULL
                """
            ).format(
                checksum_expr,
                pv_expr,
                sql.Identifier(self.schema),
                sql.Identifier("rag_documents"),
                sql.Identifier(self.schema),
                sql.Identifier(resolved_table),
            )
            cur.execute(query, (source.strip().lower(),))
            return {
                str(row[0]).strip().upper(): {
                    "doc_id": str(row[1]),
                    "checksum": (str(row[2]) if row[2] is not None else None),
                    "nb_chunks": int(row[3] or 0),
                    # False uniquement si explicitement 'false' en base.
                    "page_vision_complete": (str(row[4]).lower() != "false") if row[4] is not None else True,
                }
                for row in cur.fetchall()
            }

    def delete_documents_cascade(
        self,
        short_ids: list[str],
        table: str | None = None,
        *,
        source: str,
    ) -> dict[str, int]:
        """Supprime chunks + sections + documents pour des short_ids, en une transaction.

        Utilisé par la réconciliation (document retiré du manifest ou abrogé).
        source est obligatoire: les short_ids ne sont pas garantis uniques
        entre corpus (lignes legacy MATTE/MSO comprises) et un appel non
        filtré supprimerait silencieusement les documents homonymes d'une
        autre source.
        """
        if not source or not source.strip():
            raise ValueError("source obligatoire pour delete_documents_cascade (garde anti-suppression inter-corpus).")
        resolved_table = self._require_chunk_table(table)
        normalized_short_ids = self._normalize_short_ids(short_ids)
        if not normalized_short_ids:
            return {"chunks": 0, "sections": 0, "documents": 0}

        with self._connect() as conn:
            with conn.cursor() as cur:
                doc_filter = sql.SQL("UPPER(TRIM(short_id)) = ANY(%s) AND short_id IS NOT NULL AND LOWER(TRIM(source)) = %s")
                params: list[Any] = [normalized_short_ids, source.strip().lower()]

                cur.execute(
                    sql.SQL("SELECT doc_id FROM {}.{} WHERE {}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier("rag_documents"),
                        doc_filter,
                    ),
                    params,
                )
                doc_ids = [str(row[0]) for row in cur.fetchall()]

                deleted_chunks = 0
                deleted_sections = 0
                deleted_documents = 0
                if doc_ids:
                    deleted_chunks = self._delete_chunks_by_doc_ids(conn, doc_ids, table=resolved_table)

                    cur.execute(
                        sql.SQL("DELETE FROM {}.{} WHERE doc_id = ANY(%s::uuid[])").format(
                            sql.Identifier(self.schema),
                            sql.Identifier("rag_sections"),
                        ),
                        (doc_ids,),
                    )
                    deleted_sections = int(cur.rowcount or 0)

                    cur.execute(
                        sql.SQL("DELETE FROM {}.{} WHERE doc_id = ANY(%s::uuid[])").format(
                            sql.Identifier(self.schema),
                            sql.Identifier("rag_documents"),
                        ),
                        (doc_ids,),
                    )
                    deleted_documents = int(cur.rowcount or 0)

            conn.commit()
            return {
                "chunks": deleted_chunks,
                "sections": deleted_sections,
                "documents": deleted_documents,
            }

    def insert_ingestion_run(self, run: dict[str, Any]) -> int:
        """Trace un run d'ingestion dans rag_ingestion_runs (idempotent sur run_id)."""
        with self._connect() as conn:
            count = self._upsert(conn, "rag_ingestion_runs", [run], ["run_id"])
            conn.commit()
            return count
