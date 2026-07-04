from __future__ import annotations

from ..pdf_ministry.gold import (
    SECTION_CHUNK_ROLE,
    GoldBundle,
    GoldRepository,
    SectionAtomicGoldBuilder,
    _chunk_payload,  # noqa: F401 — API de test (filtre payload, divergence MASA)
    _table_header_and_body,  # noqa: F401 — API de test (audit chunking table-aware)
    split_section_markdown,
)
from .config import IDENTITY, MIN_CHUNK_PAYLOAD_CHARS, EmbeddingConfig, GoldConfig

__all__ = ["GoldBundle", "GoldRepository", "MIN_CHUNK_PAYLOAD_CHARS", "MasaGoldBuilder", "SECTION_CHUNK_ROLE", "split_section_markdown"]


class MasaGoldBuilder(SectionAtomicGoldBuilder):
    def __init__(self, embedding_config: EmbeddingConfig, gold_config: GoldConfig):
        super().__init__(IDENTITY, embedding_config, gold_config)
