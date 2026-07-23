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

    @property
    def sigle(self) -> str:
        """Short ministry tag used to name its source in prompts (e.g. ``MATTE``)."""
        return self.publisher


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
        label="MATTE",
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
        label="MI",
        table_key="mi",
        chunk_table="rag_chunks_mi",
        publisher="MI",
    ),
    "masa": MinistrySource(
        id="masa",
        label="MASA",
        table_key="masa",
        chunk_table="rag_chunks_masa",
        publisher="MASA",
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


def resolve_ministry(scope: RetrievalScope | str | None) -> MinistrySource | None:
    """Resolve a scope (or ministry id) to a catalog entry, or *None*.

    Fails soft: an unknown/empty ministry yields *None* so prompt rendering
    falls back to generic wording instead of raising in the hot path.
    """

    if isinstance(scope, RetrievalScope):
        ministry_id = scope.selected_ministry
    elif isinstance(scope, str):
        ministry_id = scope
    else:
        # None or any unexpected type → fail soft (generic wording), never raise.
        return None
    try:
        return get_ministry(ministry_id)
    except MinistryScopeError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt rendering (ministry-agnostic templates)
# ─────────────────────────────────────────────────────────────────────────────

# Generic wording used when no ministry is resolved (admin previews, eval
# harness, scripts). Keeps prompts readable and never leaks raw placeholders.
_FALLBACK_PLACEHOLDERS: dict[str, str] = {
    "ministere_label": "votre ministère",
    "ministere_sigle": "votre ministère",
}


def ministry_placeholders(ministry: MinistrySource | None) -> dict[str, str]:
    """Return the ``{ministere_*}`` substitution map for *ministry*."""

    if ministry is None:
        return dict(_FALLBACK_PLACEHOLDERS)
    return {
        "ministere_label": ministry.label,
        "ministere_sigle": ministry.sigle,
    }


def render_ministry_prompt(text: str, ministry: MinistrySource | None) -> str:
    """Substitute ``{ministere_label}`` / ``{ministere_sigle}`` in a prompt template.

    Uses plain ``str.replace`` (not ``str.format``) so it composes with prompts
    that legitimately contain other braces (JSON few-shot examples, ``{query}``
    placeholders resolved later downstream).
    """

    if not text:
        return text
    for key, value in ministry_placeholders(ministry).items():
        text = text.replace("{" + key + "}", value)
    return text
