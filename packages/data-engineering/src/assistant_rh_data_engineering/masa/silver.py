from __future__ import annotations

from ..pdf_ministry.silver import (
    HeadingSilverBuilder,
    SilverBundle,
    SilverRepository,
    doc_uuid,
    normalize_ocr_markdown,
    section_uuid,
)
from .config import IDENTITY, SilverConfig

__all__ = ["MasaSilverBuilder", "SilverBundle", "SilverRepository", "masa_doc_uuid", "masa_section_uuid", "normalize_ocr_markdown"]


def masa_doc_uuid(short_id: str) -> str:
    return doc_uuid(IDENTITY, short_id)


def masa_section_uuid(doc_id: str, section_index: int) -> str:
    return section_uuid(IDENTITY, doc_id, section_index)


class MasaSilverBuilder(HeadingSilverBuilder):
    def __init__(self, config: SilverConfig):
        super().__init__(IDENTITY, config)
