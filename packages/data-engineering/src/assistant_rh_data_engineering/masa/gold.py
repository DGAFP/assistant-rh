from __future__ import annotations

from ..pdf_ministry.gold import (
    SECTION_CHUNK_ROLE,
    GoldBundle,
    GoldRepository,
    SectionAtomicGoldBuilder,
    _table_header_and_body,  # noqa: F401 — API de test (audit chunking table-aware)
    split_section_markdown,
)
from .config import IDENTITY, EmbeddingConfig, GoldConfig

__all__ = ["GoldBundle", "GoldRepository", "MasaGoldBuilder", "SECTION_CHUNK_ROLE", "split_section_markdown"]


class MasaGoldBuilder(SectionAtomicGoldBuilder):
    def __init__(self, embedding_config: EmbeddingConfig, gold_config: GoldConfig):
        super().__init__(IDENTITY, embedding_config, gold_config)
