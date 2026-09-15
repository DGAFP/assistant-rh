"""Explicit immutable search catalogue, resolved once by composition/eval wiring."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from assistant_rh_api.core.models.retrieval import RetrievalSource, Source

GUIDE_FIELDS = ("source_name", "section_path", "role", "thematique", "references_juridiques", "source_document_id")
LEGAL_FIELDS = ("title", "full_title", "number", "category", "url", "cid")


@dataclass(frozen=True, slots=True)
class SearchTable:
    source: RetrievalSource
    id_column: str = "hash_id"
    tsv_column: str = "text_tsv"


def search_catalog(environ: Mapping[str, str] | None = None) -> tuple[SearchTable, ...]:
    """Never read os.environ at import; comparison overrides are wiring-only."""
    tables = []
    for key, publisher, headings, fields in (
        ("matte", "MATTE", True, GUIDE_FIELDS),
        ("mso", "MSO", True, GUIDE_FIELDS),
        ("mi", "MI", True, GUIDE_FIELDS),
        # The historical metadata allowlist has no MASA entry.
        ("masa", "MASA", True, ()),
        ("service_public", "Service-Public", True, GUIDE_FIELDS),
        ("dgafp", "DGAFP", False, LEGAL_FIELDS),
        ("rgrh", "RGRH", False, GUIDE_FIELDS),
    ):
        source = RetrievalSource(cast(Source, key), f"rag_chunks_{key}", publisher, headings, key == "dgafp", fields)
        tables.append(SearchTable(source, "chunk_id" if key == "dgafp" else "hash_id", "chunk_text_tsv" if key == "dgafp" else "text_tsv"))
    # Public composition omits comparison sources. Evaluation opts in explicitly.
    if environ is not None:
        for key, variable, publisher, fields in (
            (
                "service_public_scw",
                "SERVICE_PUBLIC_COMPARE_TABLE",
                "Service-Public (Scaleway)",
                ("source_name", "section_path", "role", "thematique", "short_id", "source"),
            ),
            ("dgafp_scw", "DGAFP_COMPARE_TABLE", "DGAFP (Scaleway)", LEGAL_FIELDS),
        ):
            name = environ.get(variable, f"rag_chunks_{key}")
            if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name) is None:
                raise ValueError("invalid comparison table identifier")
            legal = key == "dgafp_scw"
            source = RetrievalSource(cast(Source, key), name, publisher, False, name == "rag_chunks_dgafp", fields)
            tables.append(SearchTable(source, "chunk_id" if legal else "hash_id", "chunk_text_tsv" if legal else "text_tsv"))
    if len({table.source.name for table in tables}) != len(tables):
        raise ValueError("comparison table duplicates a retrieval source")
    return tuple(tables)
