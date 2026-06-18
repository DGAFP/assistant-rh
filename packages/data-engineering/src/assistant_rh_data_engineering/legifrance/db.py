from __future__ import annotations

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

    def upsert_legacy_chunks(self, chunks: list[dict]) -> int:
        with self._connect() as conn:
            self.ensure_legacy_target_table()
            count = self._upsert(
                conn,
                self.legacy_table_name,
                self.project_legacy_chunks(chunks),
                ["chunk_id"],
                preserve_on_null_cols=["embedding_m3", "embedding_bge_scw", "embedding_qwen3"],
            )
            conn.commit()
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
