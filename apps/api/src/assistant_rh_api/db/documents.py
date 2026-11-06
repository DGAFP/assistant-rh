"""Legacy bytea is sliced in SQL, in one stable snapshot, never loaded whole."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from assistant_rh_api.core.errors.documents import DocumentNotFound, DocumentUnavailable
from assistant_rh_api.core.ports.documents import DocumentContent, StoredDocument
from assistant_rh_api.db.pool import Database

DOCUMENT_CHUNK_BYTES = 64 * 1024


class DocumentStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @asynccontextmanager
    async def open(self, document_id: UUID) -> AsyncIterator[StoredDocument]:
        async with self.database.transaction(read_only=True, repeatable_read=True) as connection:
            row = await (
                await connection.execute(
                    """
                SELECT title, storage_path, legacy_doc_id FROM public.rag_documents WHERE doc_id = %s
                """,
                    (document_id,),
                )
            ).fetchone()
            if row is None:
                raise DocumentNotFound()
            title, storage_path, legacy_id = row
            legacy = None
            if legacy_id is not None:
                legacy = await (
                    await connection.execute(
                        "SELECT octet_length(data), substring(data FROM 1 FOR 5) FROM public.documents WHERE id = %s",
                        (legacy_id,),
                    )
                ).fetchone()
            if legacy is None:
                yield StoredDocument(title or "Document", storage_path or "")
                return
            size, signature = legacy
            if size is None or signature != b"%PDF-":
                raise DocumentNotFound()

            async def chunks() -> AsyncIterator[bytes]:
                for offset in range(0, size, DOCUMENT_CHUNK_BYTES):
                    part = await (
                        await connection.execute(
                            "SELECT substring(data FROM %s FOR %s) FROM public.documents WHERE id = %s",
                            (offset + 1, min(DOCUMENT_CHUNK_BYTES, size - offset), legacy_id),
                        )
                    ).fetchone()
                    if part is None or not part[0]:
                        raise DocumentUnavailable()
                    yield bytes(part[0])

            yield StoredDocument(title or "Document", legacy=DocumentContent(size, chunks()))
