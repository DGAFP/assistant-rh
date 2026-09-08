"""Batched raw content reads. Ordering is explicit; no context selection here."""

from collections.abc import Mapping
from typing import cast

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from assistant_rh_api.core.models import ConfigValues
from assistant_rh_api.core.ports import ContentStorePort
from assistant_rh_api.core.runtime import Document, LegalReference, RawChunk, Section, Source
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.revisions import freeze_json

# Only logical source keys cross the core boundary. Comparison tables belong to
# evaluation wiring, not this public runtime catalogue.
TABLES = {
    "matte": ("rag_chunks_matte", "hash_id", "text_tsv"),
    "mso": ("rag_chunks_mso", "hash_id", "text_tsv"),
    "mi": ("rag_chunks_mi", "hash_id", "text_tsv"),
    "masa": ("rag_chunks_masa", "hash_id", "text_tsv"),
    "service_public": ("rag_chunks_service_public", "hash_id", "text_tsv"),
    "dgafp": ("rag_chunks_dgafp", "chunk_id", "chunk_text_tsv"),
    "rgrh": ("rag_chunks_rgrh", "hash_id", "text_tsv"),
}


def table_spec(source: str) -> tuple[str, str, str]:
    if source not in TABLES:
        raise ValueError("unknown logical source")
    return TABLES[source]


async def columns(connection: AsyncConnection, table: str) -> set[str]:
    rows = await (
        await connection.execute(
            """
        SELECT attname FROM pg_catalog.pg_attribute
        WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
    """,
            (f"public.{table}",),
        )
    ).fetchall()
    return {row[0] for row in rows}


def section_expression(existing: set[str]) -> sql.Composable:
    direct = sql.SQL("t.section_id") if "section_id" in existing else sql.SQL("NULL::uuid")
    if not {"short_id", "section_path"}.issubset(existing):
        return direct
    return sql.SQL(r"""COALESCE({}, (
        SELECT s.section_id FROM public.rag_sections s
        JOIN public.rag_documents d ON d.doc_id = s.doc_id
        WHERE d.short_id = t.short_id AND (
            s.heading_path = t.section_path OR s.heading = btrim(regexp_replace(t.section_path, '^.*>\s*', ''))
        ) ORDER BY CASE WHEN s.heading_path = t.section_path THEN 0 ELSE 1 END, s.section_id
        LIMIT 1
    ))""").format(direct)


def metadata_expression(existing: set[str]) -> sql.Composable:
    keys = sorted(
        existing
        & {
            "source_name",
            "source_document_id",
            "short_id",
            "section_path",
            "url",
            "number",
            "cid",
            "full_title",
            "publisher",
            "page",
            "title",
            "heading",
            "heading_path",
            "chunk_role",
            "question",
            "answer",
            "role",
            "thematique",
            "references_juridiques",
            "category",
            "source",
        }
    )
    args = [item for key in keys for item in (sql.Literal(key), sql.Identifier("t", key))]
    metadata = sql.SQL("jsonb_build_object({})").format(sql.SQL(", ").join(args))
    if "short_id" in existing:
        # A legacy chunk can have a known document but no resolvable section.
        # Keep canonical source metadata available on every raw search lane.
        metadata += sql.SQL(""" || jsonb_build_object('doc_short_id', t.short_id) || COALESCE((
            SELECT jsonb_build_object('doc_id', d.doc_id, 'doc_title', d.title, 'doc_url', d.source_url)
            FROM public.rag_documents d WHERE d.short_id = t.short_id ORDER BY d.doc_id LIMIT 1
        ), '{}'::jsonb)""")
    return metadata


def immutable_object(value: object) -> ConfigValues:
    result = freeze_json(value)
    if not isinstance(result, Mapping):
        raise ValueError("expected a JSON object")
    return result


def document(row: dict) -> Document:
    return Document(
        str(row["doc_id"]),
        row.get("short_id"),
        row.get("title") or "",
        row.get("source_url") or "",
        row.get("publisher") or "",
        row.get("doc_markdown") or "",
        row.get("token_count") or 0,
        str(row["last_updated_date"]) if row.get("last_updated_date") is not None else None,
    )


class ContentStore(ContentStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def documents(self, ids: tuple[str, ...]) -> tuple[Document, ...]:
        if not ids:
            return ()
        async with self._database.transaction(read_only=True) as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT doc_id, short_id, title, source_url, publisher, doc_markdown, token_count,
                           to_jsonb(d)->>'last_updated_date' AS last_updated_date
                    FROM public.rag_documents d WHERE doc_id = ANY(%s::uuid[]) ORDER BY doc_id
                """,
                    (list(ids),),
                )
                return tuple(document(row) for row in await cursor.fetchall())

    async def sections(self, ids: tuple[str, ...]) -> tuple[Section, ...]:
        if not ids:
            return ()
        async with self._database.transaction(read_only=True) as connection:
            rows = await (
                await connection.execute(
                    """
                SELECT s.section_id, s.doc_id, s.heading, s.heading_path,
                       COALESCE(to_jsonb(s)->>'section_markdown', to_jsonb(s)->>'markdown_content', ''),
                       to_jsonb(s)->'references_juridiques', to_jsonb(d)
                FROM public.rag_sections s LEFT JOIN public.rag_documents d ON d.doc_id = s.doc_id
                WHERE s.section_id = ANY(%s::uuid[]) ORDER BY s.section_id
            """,
                    (list(ids),),
                )
            ).fetchall()
        return tuple(Section(str(r[0]), str(r[1]), r[2] or "", r[3] or "", r[4], freeze_json(r[5]), document(r[6]) if r[6] else None) for r in rows)

    async def references(self, numbers: tuple[str, ...]) -> tuple[LegalReference, ...]:
        if not numbers:
            return ()
        async with self._database.transaction(read_only=True) as connection:
            rows = await (
                await connection.execute(
                    """
                SELECT DISTINCT number, COALESCE(cid, ''), COALESCE(url, ''), COALESCE(full_title, '')
                FROM public.rag_chunks_dgafp WHERE number = ANY(%s)
                ORDER BY number, COALESCE(cid, ''), COALESCE(url, ''), COALESCE(full_title, '')
            """,
                    (list(numbers),),
                )
            ).fetchall()
        return tuple(LegalReference(*r) for r in rows)

    async def chunks(self, source: str, ids: tuple[str, ...]) -> tuple[RawChunk, ...]:
        table, id_column, _ = table_spec(source)
        if not ids:
            return ()
        async with self._database.transaction(read_only=True) as connection:
            existing = await columns(connection, table)
            statement = sql.SQL("""
                SELECT t.{id}::text, t.chunk_text, {section}, {metadata}
                FROM {table} t WHERE t.{id}::text = ANY(%s) ORDER BY t.{id}
            """).format(
                id=sql.Identifier(id_column),
                section=section_expression(existing),
                metadata=metadata_expression(existing),
                table=sql.Identifier("public", table),
            )
            rows = await (await connection.execute(statement, (list(ids),))).fetchall()
        return tuple(
            RawChunk(cast(Source, source), r[0], r[1] or "", str(r[2]) if r[2] else None, 0.0, i, immutable_object(r[3]))
            for i, r in enumerate(rows, 1)
        )
