"""D2: author + current ministry + final source, on every content request."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.errors import IndividualIdentityRequired
from assistant_rh_api.core.errors.documents import DocumentNotFound
from assistant_rh_api.core.ministry_policy import MINISTRIES
from assistant_rh_api.core.ports.conversations import ChatRunStorePort
from assistant_rh_api.core.ports.documents import AuthorizedDocument, DocumentContent, DocumentStorePort, ObjectDocumentPort


class DocumentService:
    def __init__(self, runs: ChatRunStorePort, documents: DocumentStorePort, objects: ObjectDocumentPort) -> None:
        self.runs = runs
        self.documents = documents
        self.objects = objects

    async def authorize(self, completion_id: str, doc_ref: str, auth: AuthContext) -> AuthorizedDocument:
        if auth.user_id is None:
            raise IndividualIdentityRequired()
        turn_id = completion_id.removeprefix("chatcmpl-")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", turn_id) or not 1 <= len(doc_ref) <= 256:
            raise DocumentNotFound()
        source = await self.runs.document_source(turn_id, doc_ref, ministries=auth.allowed_ministries, user_id=auth.user_id)
        if source is None or source[1] not in MINISTRIES:
            raise DocumentNotFound()
        document_id, ministry = source
        try:
            return AuthorizedDocument(UUID(document_id), ministry)
        except ValueError:
            raise DocumentNotFound() from None

    @asynccontextmanager
    async def open(self, document: AuthorizedDocument) -> AsyncIterator[DocumentContent]:
        async with self.documents.open(document) as stored:
            if stored.legacy is not None:
                yield stored.legacy
                return
            storage_path = stored.storage_path
        # No database lease is retained during an S3 transfer. Never fetch source_url.
        if not storage_path.startswith(f"{document.ministry}/"):
            raise DocumentNotFound()
        async with self.objects.open(storage_path) as content:
            yield content
