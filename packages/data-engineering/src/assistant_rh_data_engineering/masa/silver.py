from __future__ import annotations

from ..pdf_ministry.silver import HeadingSilverBuilder, SilverBundle, SilverRepository, normalize_ocr_markdown
from .config import IDENTITY, SilverConfig

__all__ = ["MasaSilverBuilder", "SilverBundle", "SilverRepository", "normalize_ocr_markdown"]


class MasaSilverBuilder(HeadingSilverBuilder):
    def __init__(self, config: SilverConfig):
        super().__init__(IDENTITY, config)
