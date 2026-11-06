"""Private document I/O; targets are resolved only from persisted source IDs."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DocumentContent:
    size: int
    chunks: AsyncIterator[bytes] = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredDocument:
    title: str
    storage_path: str = field(default="", repr=False)
    legacy: DocumentContent | None = field(default=None, repr=False)


class DocumentStorePort(Protocol):
    def open(self, document_id: UUID) -> AbstractAsyncContextManager[StoredDocument]: ...


class ObjectDocumentPort(Protocol):
    def open(self, storage_path: str) -> AbstractAsyncContextManager[DocumentContent]: ...
