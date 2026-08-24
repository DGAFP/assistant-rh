from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from ..service_public.db import ServicePublicDbWriter
from .config import GoldConfig

LEGACY_TARGET_COLUMNS: dict[str, str] = {
    "chunk_id": "VARCHAR(64)",
    "cid": "TEXT",
    "chunk_text": "TEXT",
    "text": "TEXT",
    "number": "TEXT",
    "title": "TEXT",
    "full_title": "TEXT",
    "subtitles": "TEXT",
    "nota": "TEXT",
    "status": "TEXT",
    "category": "TEXT",
    "source_name": "TEXT",
    "ministry": "TEXT",
    "url": "TEXT",
    "section_parent_cid": "TEXT",
    "section_parent_titre": "TEXT",
    "lien_citations": "TEXT",
    "lien_citations_count": "INTEGER",
    "lien_modifications": "TEXT",
    "lien_modifications_count": "INTEGER",
    "lien_concordes": "TEXT",
    "lien_concordes_count": "INTEGER",
    "comporte_liens_sp": "BOOLEAN",
    "chunk_number": "INTEGER",
    # Marqueur des lignes d'index ADDITIVES (R2, résumés d'article) : NULL =
    # chunk normal ; "r2_summary/{version}/{sha16}" = ligne dont l'embedding
    # encode le résumé métier mais dont chunk_text reste le texte authentique
    # (cf. legifrance/summary_rows.py). Colonne ajoutée par _ensure_table au
    # premier run (mécanisme de migration natif de cette table).
    "index_variant": "TEXT",
    "start_date": "DATE",
    "end_date": "DATE",
    "created_at": "TIMESTAMPTZ",
    "updated_at": "TIMESTAMPTZ",
    "chunk_text_tsv": "tsvector GENERATED ALWAYS AS (to_tsvector('french', COALESCE(chunk_text, ''))) STORED",
    "embedding_m3": "vector(1024)",
    "embedding_bge_scw": "vector(3584)",
    "embedding_qwen3": "vector(4096)",
}

MODERN_TARGET_COLUMNS: dict[str, str] = {
    "hash_id": "VARCHAR(64)",
    "qa_id": "TEXT",
    "parent_qa_id": "TEXT",
    "source_name": "TEXT",
    "section_path": "TEXT",
    "role": "TEXT",
    "chunk_index": "INTEGER",
    "text": "TEXT",
    "chunk_text": "TEXT",
    "lang": "TEXT",
    "thematique": "TEXT",
    "source": "TEXT",
    "short_id": "VARCHAR(64)",
    "references_juridiques": "JSONB",
    "section_id": "UUID",
    "source_document_id": "UUID",
    "chunk_id": "VARCHAR(64)",
    "chunk_number": "INTEGER",
    "title": "TEXT",
    "full_title": "TEXT",
    "number": "TEXT",
    "category": "TEXT",
    "status": "TEXT",
    "subtitles": "TEXT",
    "nota": "TEXT",
    "ministry": "TEXT",
    "url": "TEXT",
    "cid": "TEXT",
    "section_parent_cid": "TEXT",
    "section_parent_titre": "TEXT",
    "lien_citations": "JSONB",
    "lien_citations_count": "INTEGER",
    "lien_modifications": "JSONB",
    "lien_modifications_count": "INTEGER",
    "lien_concordes": "JSONB",
    "lien_concordes_count": "INTEGER",
    "comporte_liens_sp": "BOOLEAN",
    "start_date": "DATE",
    "end_date": "DATE",
    "created_at": "TIMESTAMPTZ",
    "updated_at": "TIMESTAMPTZ",
    "text_tsv": "tsvector GENERATED ALWAYS AS (to_tsvector('french', COALESCE(section_path, '') || ' ' || COALESCE(chunk_text, ''))) STORED",
    "embedding_m3": "vector(1024)",
    "embedding_bge_scw": "vector(3584)",
}


class LegifranceDbWriter(ServicePublicDbWriter):
    def __init__(
        self,
        schema: str = "public",
        dsn: str | None = None,
        legacy_table_name: str = "rag_chunks_dgafp",
        modern_table_name: str = "rag_chunks_legifrance",
    ):
        super().__init__(schema=schema, dsn=dsn)
        self.legacy_table_name = legacy_table_name
        self.modern_table_name = modern_table_name

    def _ensure_table(
        self,
        table_name: str,
        create_sql: str,
        desired_columns: dict[str, str],
        extra_indexes: list[sql.Composed],
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except psycopg.Error:
                conn.rollback()

            cur.execute(
                sql.SQL(create_sql).format(
                    sql.Identifier(self.schema),
                    sql.Identifier(table_name),
                )
            )

            existing_columns = self._column_types(conn, table_name)
            for column_name, column_sql_type in desired_columns.items():
                if column_name in existing_columns:
                    continue
                cur.execute(
                    sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} {}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(table_name),
                        sql.Identifier(column_name),
                        sql.SQL(column_sql_type),
                    )
                )

            for index_sql in extra_indexes:
                cur.execute(index_sql)
            conn.commit()

    def ensure_legacy_target_table(self) -> None:
        self._ensure_table(
            self.legacy_table_name,
            """
            CREATE TABLE IF NOT EXISTS {}.{} (
                id BIGSERIAL PRIMARY KEY,
                chunk_id VARCHAR(64) UNIQUE,
                cid TEXT,
                chunk_text TEXT,
                text TEXT,
                number TEXT,
                title TEXT,
                full_title TEXT,
                subtitles TEXT,
                nota TEXT,
                status TEXT,
                category TEXT,
                source_name TEXT,
                ministry TEXT,
                url TEXT,
                section_parent_cid TEXT,
                section_parent_titre TEXT,
                lien_citations TEXT,
                lien_citations_count INTEGER,
                lien_modifications TEXT,
                lien_modifications_count INTEGER,
                lien_concordes TEXT,
                lien_concordes_count INTEGER,
                comporte_liens_sp BOOLEAN,
                chunk_number INTEGER,
                index_variant TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                chunk_text_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector('french', COALESCE(chunk_text, ''))
                ) STORED,
                start_date DATE,
                end_date DATE,
                embedding_m3 vector(1024),
                embedding_bge_scw vector(3584),
                embedding_qwen3 vector(4096)
            )
            """,
            LEGACY_TARGET_COLUMNS,
            [
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (number)").format(
                    sql.Identifier(f"idx_{self.legacy_table_name}_number"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.legacy_table_name),
                ),
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (cid)").format(
                    sql.Identifier(f"idx_{self.legacy_table_name}_cid"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.legacy_table_name),
                ),
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} USING GIN (chunk_text_tsv)").format(
                    sql.Identifier(f"idx_{self.legacy_table_name}_chunk_text_tsv"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.legacy_table_name),
                ),
                sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {}.{} (chunk_id)").format(
                    sql.Identifier(f"idx_{self.legacy_table_name}_chunk_id_unique"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.legacy_table_name),
                ),
            ],
        )

    def ensure_modern_target_table(self) -> None:
        self._ensure_table(
            self.modern_table_name,
            """
            CREATE TABLE IF NOT EXISTS {}.{} (
                hash_id VARCHAR(64) PRIMARY KEY,
                qa_id TEXT,
                parent_qa_id TEXT,
                source_name TEXT,
                section_path TEXT,
                role TEXT,
                chunk_index INTEGER,
                text TEXT,
                chunk_text TEXT,
                lang TEXT DEFAULT 'fr',
                thematique TEXT,
                source TEXT,
                short_id VARCHAR(64),
                references_juridiques JSONB,
                section_id UUID,
                source_document_id UUID,
                chunk_id VARCHAR(64),
                chunk_number INTEGER,
                title TEXT,
                full_title TEXT,
                number TEXT,
                category TEXT,
                status TEXT,
                subtitles TEXT,
                nota TEXT,
                ministry TEXT,
                url TEXT,
                cid TEXT,
                section_parent_cid TEXT,
                section_parent_titre TEXT,
                lien_citations JSONB,
                lien_citations_count INTEGER,
                lien_modifications JSONB,
                lien_modifications_count INTEGER,
                lien_concordes JSONB,
                lien_concordes_count INTEGER,
                comporte_liens_sp BOOLEAN,
                start_date DATE,
                end_date DATE,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                text_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector(
                        'french',
                        COALESCE(section_path, '') || ' ' || COALESCE(chunk_text, '')
                    )
                ) STORED,
                embedding_m3 vector(1024),
                embedding_bge_scw vector(3584)
            )
            """,
            MODERN_TARGET_COLUMNS,
            [
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (short_id)").format(
                    sql.Identifier(f"idx_{self.modern_table_name}_short_id"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.modern_table_name),
                ),
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (cid)").format(
                    sql.Identifier(f"idx_{self.modern_table_name}_cid"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.modern_table_name),
                ),
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (section_id)").format(
                    sql.Identifier(f"idx_{self.modern_table_name}_section_id"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.modern_table_name),
                ),
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} USING GIN (text_tsv)").format(
                    sql.Identifier(f"idx_{self.modern_table_name}_text_tsv"),
                    sql.Identifier(self.schema),
                    sql.Identifier(self.modern_table_name),
                ),
            ],
        )

    @staticmethod
    def project_legacy_chunk(chunk: dict) -> dict:
        return {column: chunk.get(column) for column in LEGACY_TARGET_COLUMNS if not column.endswith("_tsv")}

    @staticmethod
    def project_modern_chunk(chunk: dict) -> dict:
        return {column: chunk.get(column) for column in MODERN_TARGET_COLUMNS if not column.endswith("_tsv")}

    @classmethod
    def project_legacy_chunks(cls, chunks: list[dict]) -> list[dict]:
        return [cls.project_legacy_chunk(chunk) for chunk in chunks if "legacy" in (chunk.get("_targets") or ["legacy", "modern"])]

    @classmethod
    def project_modern_chunks(cls, chunks: list[dict]) -> list[dict]:
        return [cls.project_modern_chunk(chunk) for chunk in chunks if "modern" in (chunk.get("_targets") or ["legacy", "modern"])]

    def upsert_legacy_chunks(self, chunks: list[dict], conn=None) -> int:
        # ``conn`` fourni = le caller porte la transaction (revalidation de
        # fraîcheur + upsert atomiques, cf. jobs/r2_article_summaries) et
        # committe lui-même. Le caller DOIT avoir appelé
        # ensure_legacy_target_table() AVANT d'ouvrir sa transaction : le DDL
        # passe par une seconde connexion et son ACCESS EXCLUSIVE se met en
        # file derrière les verrous de ``conn`` — auto-deadlock qui a gelé le
        # retrieval staging le 23/07 (apply R2 bloqué sur l'ADD COLUMN).
        if conn is not None:
            return self._upsert(
                conn,
                self.legacy_table_name,
                self.project_legacy_chunks(chunks),
                ["chunk_id"],
                preserve_on_null_cols=["embedding_m3", "embedding_bge_scw", "embedding_qwen3"],
            )
        with self._connect() as own_conn:
            self.ensure_legacy_target_table()
            count = self._upsert(
                own_conn,
                self.legacy_table_name,
                self.project_legacy_chunks(chunks),
                ["chunk_id"],
                preserve_on_null_cols=["embedding_m3", "embedding_bge_scw", "embedding_qwen3"],
            )
            own_conn.commit()
            return count

    def upsert_modern_chunks(self, chunks: list[dict]) -> int:
        with self._connect() as conn:
            self.ensure_modern_target_table()
            count = self._upsert(
                conn,
                self.modern_table_name,
                self.project_modern_chunks(chunks),
                ["hash_id"],
                preserve_on_null_cols=["embedding_m3", "embedding_bge_scw"],
            )
            conn.commit()
            return count

    def upsert_chunks(self, chunks: list[dict]) -> int:
        return self.upsert_legacy_chunks(chunks)

    # --- Réconciliation delta (E2.3-b, #289) ---------------------------------
    # La table legacy (articles, rag_chunks_dgafp) n'a NI short_id NI
    # source_document_id : le rattachement au document se fait par `cid`
    # (= short_id du document article). Les helpers du socle (bundle, cascade,
    # list_short_ids_with_checksum) supposent ces colonnes — d'où les variantes
    # ci-dessous. La table moderne (textes legacy, rag_chunks_legifrance) est,
    # elle, pleinement compatible socle.

    _EMBEDDING_COLUMNS_LEGACY = ["embedding_m3", "embedding_bge_scw", "embedding_qwen3"]
    _EMBEDDING_COLUMNS_MODERN = ["embedding_m3", "embedding_bge_scw"]

    def list_legifrance_corpus(self, source: str = "legifrance") -> dict[str, dict[str, Any]]:
        """État corpus Légifrance : short_id -> {doc_id, checksum, nb_chunks}.

        ``nb_chunks`` additionne les chunks legacy (rattachés par ``cid``) et
        modernes (par ``short_id``) — un uid ne vit que dans une des deux
        tables, l'addition est donc un simple merge. Cette inspection est
        strictement read-only afin que ``--dry-run`` ne crée ni table ni index.
        """
        with self._connect() as conn, conn.cursor() as cur:

            def table_exists(table: str) -> bool:
                cur.execute("SELECT to_regclass(%s)", (f"{self.schema}.{table}",))
                row = cur.fetchone()
                return bool(row and row[0])

            if not table_exists("rag_documents"):
                return {}
            document_columns = self._column_types(conn, "rag_documents")
            checksum_expr = sql.Identifier("checksum") if "checksum" in document_columns else sql.SQL("NULL")
            cur.execute(
                sql.SQL(
                    """
                    SELECT short_id, doc_id, {} AS checksum
                    FROM {}.{}
                    WHERE LOWER(TRIM(source)) = %s AND short_id IS NOT NULL
                    """
                ).format(checksum_expr, sql.Identifier(self.schema), sql.Identifier("rag_documents")),
                (source.strip().lower(),),
            )
            corpus = {
                str(row[0]).strip().upper(): {
                    "doc_id": str(row[1]),
                    "checksum": (str(row[2]) if row[2] is not None else None),
                    "nb_chunks": 0,
                }
                for row in cur.fetchall()
            }

            for table, join_column in ((self.legacy_table_name, "cid"), (self.modern_table_name, "short_id")):
                if not table_exists(table):
                    continue
                # Les lignes-résumé R2 (index_variant renseigné) partagent le
                # cid de leur article : les compter doublerait nb_chunks et
                # fausserait la réconciliation delta (revue #332, round 2).
                summary_filter = sql.SQL("")
                if table == self.legacy_table_name and "index_variant" in self._column_types(conn, table):
                    summary_filter = sql.SQL(" AND index_variant IS NULL")
                cur.execute(
                    sql.SQL(
                        """
                        SELECT UPPER(TRIM({})), COUNT(*)
                        FROM {}.{}
                        WHERE {} IS NOT NULL{}
                        GROUP BY 1
                        """
                    ).format(
                        sql.Identifier(join_column),
                        sql.Identifier(self.schema),
                        sql.Identifier(table),
                        sql.Identifier(join_column),
                        summary_filter,
                    )
                )
                for uid, count in cur.fetchall():
                    if uid in corpus:
                        corpus[uid]["nb_chunks"] += int(count or 0)
            return corpus

    def _ingest_bundle_tx(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
        projected_chunks: list[dict[str, Any]],
        *,
        table: str,
        chunk_join_column: str,
        chunk_conflict_column: str,
        preserve_embedding_columns: list[str],
        realign_source_document_id: bool,
        cascade_cids: list[str] | None = None,
        cascade_source: str = "legifrance",
    ) -> dict[str, int]:
        """Document + sections + remplacement des chunks en UNE transaction.

        Même invariant que ``ingest_document_bundle`` du socle (jamais un
        checksum à jour avec des chunks périmés), mais adapté aux tables Legi :
        upsert des chunks sur leur clé propre en **préservant les embeddings**
        (backfillés par un job séparé, un delete+insert aveugle les perdrait),
        puis suppression des seuls chunks orphelins de la nouvelle version.

        ``cascade_cids`` (migration d'identité version→chronique, fix swap #307) :
        cascade ces cids (jumeaux version) DANS LA MÊME TRANSACTION, avant les
        upserts. Atomique : si l'INSERT de la chronique échoue, le rollback
        restaure le jumeau version — jamais de trou (ni version ni chronique).
        Le résultat porte les comptes cascadés sous ``migrated``.
        """
        short_id = str(document.get("short_id") or "").strip().upper()
        doc_id = str(document.get("doc_id") or "")
        with self._connect() as conn:
            migrated = {"chunks": 0, "sections": 0, "documents": 0}
            if cascade_cids:
                # Ne jamais s'auto-cascader (le short_id du bundle lui-même).
                twins = [c for c in self._normalize_short_ids(cascade_cids) if c != short_id]
                migrated = self._cascade_articles_ops(conn, twins, cascade_source)
            documents_count = self._upsert_documents(conn, [document])
            canonical_doc_id = self._canonical_doc_id(conn, short_id) if short_id else None
            if canonical_doc_id and doc_id and canonical_doc_id != doc_id:
                doc_id = canonical_doc_id
                for section in sections:
                    section["doc_id"] = canonical_doc_id
                if realign_source_document_id:
                    for chunk in projected_chunks:
                        if "source_document_id" in chunk:
                            chunk["source_document_id"] = canonical_doc_id
            if doc_id:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DELETE FROM {}.{} WHERE doc_id = %s::uuid").format(sql.Identifier(self.schema), sql.Identifier("rag_sections")),
                        (doc_id,),
                    )
            sections_count = self._upsert_sections(conn, sections) if sections else 0

            new_ids = [str(chunk.get(chunk_conflict_column) or "") for chunk in projected_chunks if chunk.get(chunk_conflict_column)]
            deleted = 0
            if short_id:
                with conn.cursor() as cur:
                    if new_ids:
                        query = sql.SQL(
                            """
                            DELETE FROM {}.{}
                            WHERE UPPER(TRIM({})) = %s
                              AND ({} <> ALL(%s::text[]) OR {} IS NULL)
                            """
                        ).format(
                            sql.Identifier(self.schema),
                            sql.Identifier(table),
                            sql.Identifier(chunk_join_column),
                            sql.Identifier(chunk_conflict_column),
                            sql.Identifier(chunk_conflict_column),
                        )
                        cur.execute(query, (short_id, new_ids))
                    else:
                        query = sql.SQL("DELETE FROM {}.{} WHERE UPPER(TRIM({})) = %s").format(
                            sql.Identifier(self.schema),
                            sql.Identifier(table),
                            sql.Identifier(chunk_join_column),
                        )
                        cur.execute(query, (short_id,))
                    deleted = int(cur.rowcount or 0)
                deleted += self._purge_summary_rows_fresh_snapshot(conn, table=table, join_column=chunk_join_column, uids=[short_id])
            inserted = 0
            for batch in self._batched_rows(projected_chunks, 1000):
                inserted += self._upsert(
                    conn,
                    table,
                    batch,
                    [chunk_conflict_column],
                    preserve_on_null_cols=preserve_embedding_columns,
                )
            conn.commit()
            return {
                "documents": documents_count,
                "sections": sections_count,
                "chunks_deleted": deleted,
                "chunks": inserted,
                "migrated": migrated,
            }

    def ingest_article_bundle(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        *,
        cascade_cids: list[str] | None = None,
        cascade_source: str = "legifrance",
    ) -> dict[str, int]:
        """Ré-ingestion atomique d'un article du code (table legacy, clé cid/chunk_id).

        ``cascade_cids`` : jumeaux version à cascader dans la même transaction
        avant l'ingest (migration d'identité, fix swap #307).
        """
        projected_chunks = self.project_legacy_chunks(chunks)
        if not projected_chunks:
            raise ValueError("bundle article sans chunk legacy: remplacement destructif refusé")
        self.ensure_legacy_target_table()
        return self._ingest_bundle_tx(
            document,
            sections,
            projected_chunks,
            table=self.legacy_table_name,
            chunk_join_column="cid",
            chunk_conflict_column="chunk_id",
            preserve_embedding_columns=self._EMBEDDING_COLUMNS_LEGACY,
            realign_source_document_id=False,
            cascade_cids=cascade_cids,
            cascade_source=cascade_source,
        )

    def ingest_texte_bundle(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Ré-ingestion atomique d'un texte legacy (table moderne, clé short_id/hash_id)."""
        projected_chunks = self.project_modern_chunks(chunks)
        if not projected_chunks:
            raise ValueError("bundle texte sans chunk moderne: remplacement destructif refusé")
        self.ensure_modern_target_table()
        return self._ingest_bundle_tx(
            document,
            sections,
            projected_chunks,
            table=self.modern_table_name,
            chunk_join_column="short_id",
            chunk_conflict_column="hash_id",
            preserve_embedding_columns=self._EMBEDDING_COLUMNS_MODERN,
            realign_source_document_id=True,
        )

    def delete_articles_cascade(self, cids: list[str], *, source: str = "legifrance") -> dict[str, int]:
        """Cascade chunks legacy (par cid) + sections + documents, en une transaction.

        Équivalent de ``delete_documents_cascade`` du socle pour la table
        legacy sans ``source_document_id`` : les chunks sont supprimés par
        ``cid`` (couvre aussi les chunks legacy sans ligne document).
        """
        normalized = self._normalize_short_ids(cids)
        if not normalized:
            return {"chunks": 0, "sections": 0, "documents": 0}
        self.ensure_legacy_target_table()
        with self._connect() as conn:
            counts = self._cascade_articles_ops(conn, normalized, source)
            conn.commit()
            return counts

    def _purge_summary_rows_fresh_snapshot(self, conn: Any, *, table: str, join_column: str, uids: list[str]) -> int:
        """2e passe de purge des lignes-résumé R2, à SNAPSHOT FRAIS.

        Appliquée à TOUS les chemins de suppression legacy par cid (revue
        #332 rounds 3-4 : ``_ingest_bundle_tx`` ET ``_cascade_articles_ops``) :
        en READ COMMITTED, une ligne R2 insérée PENDANT l'attente de verrou du
        DELETE principal lui est invisible (EvalPlanQual) et survivrait
        orpheline avec l'ancien texte ; chaque statement ayant son propre
        snapshot, ce DELETE séparé la voit. No-op hors table legacy (seule
        porteuse de lignes R2 — le probe est inutile sur la table moderne) et
        sur les bases sans colonne ``index_variant``."""
        if table != self.legacy_table_name or not uids:
            return 0
        # Pas de rattrapage silencieux (revue #332 round 5) : les chemins
        # publics legacy passent par ensure_legacy_target_table, donc la
        # colonne index_variant existe — une erreur de schéma/DB ici doit
        # FAIRE ÉCHOUER la transaction, jamais désactiver le garde
        # d'intégrité en retournant 0. Seule l'absence de la colonne (base
        # pas encore migrée, cas légitime) est un no-op.
        if "index_variant" not in self._column_types(conn, table):
            return 0
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {}.{} WHERE UPPER(TRIM({})) = ANY(%s) AND index_variant IS NOT NULL").format(
                    sql.Identifier(self.schema), sql.Identifier(table), sql.Identifier(join_column)
                ),
                (uids,),
            )
            return int(cur.rowcount or 0)

    def _cascade_articles_ops(self, conn: Any, normalized: list[str], source: str) -> dict[str, int]:
        """Cascade chunks legacy (par cid) + sections + documents sur une connexion
        DONNÉE, SANS commit — réutilisable dans une transaction partagée (ex. la
        migration d'identité, qui cascade le jumeau version puis ingère la
        chronique dans une seule transaction atomique)."""
        if not normalized:
            return {"chunks": 0, "sections": 0, "documents": 0}
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT doc_id FROM {}.{} WHERE UPPER(TRIM(short_id)) = ANY(%s) AND short_id IS NOT NULL AND LOWER(TRIM(source)) = %s"
                ).format(sql.Identifier(self.schema), sql.Identifier("rag_documents")),
                (normalized, source.strip().lower()),
            )
            doc_ids = [str(row[0]) for row in cur.fetchall()]

            cur.execute(
                sql.SQL("DELETE FROM {}.{} WHERE UPPER(TRIM(cid)) = ANY(%s)").format(
                    sql.Identifier(self.schema), sql.Identifier(self.legacy_table_name)
                ),
                (normalized,),
            )
            deleted_chunks = int(cur.rowcount or 0)
        deleted_chunks += self._purge_summary_rows_fresh_snapshot(conn, table=self.legacy_table_name, join_column="cid", uids=normalized)
        with conn.cursor() as cur:
            deleted_sections = 0
            deleted_documents = 0
            if doc_ids:
                cur.execute(
                    sql.SQL("DELETE FROM {}.{} WHERE doc_id = ANY(%s::uuid[])").format(sql.Identifier(self.schema), sql.Identifier("rag_sections")),
                    (doc_ids,),
                )
                deleted_sections = int(cur.rowcount or 0)
                cur.execute(
                    sql.SQL("DELETE FROM {}.{} WHERE doc_id = ANY(%s::uuid[])").format(sql.Identifier(self.schema), sql.Identifier("rag_documents")),
                    (doc_ids,),
                )
                deleted_documents = int(cur.rowcount or 0)
        return {"chunks": deleted_chunks, "sections": deleted_sections, "documents": deleted_documents}

    def delete_textes_cascade(self, short_ids: list[str], *, source: str = "legifrance") -> dict[str, int]:
        """Cascade d'un texte legacy — socle standard sur la table moderne."""
        return self.delete_documents_cascade(short_ids, table=self.modern_table_name, source=source)


def writer_from_gold_config(
    gold: GoldConfig,
    schema: str = "public",
    dsn: str | None = None,
) -> LegifranceDbWriter:
    return LegifranceDbWriter(
        schema=schema,
        dsn=dsn,
        legacy_table_name=gold.legacy_table_name,
        modern_table_name=gold.modern_table_name,
    )
