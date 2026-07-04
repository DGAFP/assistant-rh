"""Request-scoped ministry routing for retrieval.

The ministry catalog is code-owned: admins can grant access only to ministry
IDs declared here, and the RAG pipeline resolves those IDs to retriever table
keys before each request.
"""

from __future__ import annotations

from dataclasses import dataclass


class MinistryScopeError(ValueError):
    """Raised when a ministry retrieval scope is invalid."""


@dataclass(frozen=True)
class MinistrySource:
    """Catalog entry for one ministry-specific source table."""

    id: str
    label: str
    table_key: str
    chunk_table: str
    publisher: str


@dataclass(frozen=True)
class RetrievalScope:
    """Resolved request scope passed to the RAG pipeline."""

    selected_ministry: str
    table_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_ministry": self.selected_ministry,
            "table_keys": list(self.table_keys),
        }


MINISTRY_CATALOG: dict[str, MinistrySource] = {
    "matte": MinistrySource(
        id="matte",
        label="Aménagement du territoire et Transition écologique",
        table_key="matte",
        chunk_table="rag_chunks_matte",
        publisher="MATTE",
    ),
    "mso": MinistrySource(
        id="mso",
        label="MSO",
        table_key="mso",
        chunk_table="rag_chunks_mso",
        publisher="MSO",
    ),
    "mi": MinistrySource(
        id="mi",
        label="Intérieur",
        table_key="mi",
        chunk_table="rag_chunks_mi",
        publisher="MI",
    ),
}

SHARED_TABLE_KEYS: tuple[str, ...] = ("service_public", "dgafp")


def known_ministry_ids() -> set[str]:
    """Return the known ministry IDs from the code catalog."""

    return set(MINISTRY_CATALOG)


def get_ministry(ministry_id: str) -> MinistrySource:
    """Return a catalog entry or fail closed for unknown ministry IDs."""

    key = (ministry_id or "").strip().lower()
    try:
        return MINISTRY_CATALOG[key]
    except KeyError as exc:
        raise MinistryScopeError(f"Unknown ministry: {ministry_id!r}") from exc


def build_retrieval_scope(selected_ministry: str) -> RetrievalScope:
    """Resolve a selected ministry to the request-scoped retriever table keys."""

    ministry = get_ministry(selected_ministry)
    return RetrievalScope(
        selected_ministry=ministry.id,
        table_keys=(ministry.table_key, *SHARED_TABLE_KEYS),
    )
