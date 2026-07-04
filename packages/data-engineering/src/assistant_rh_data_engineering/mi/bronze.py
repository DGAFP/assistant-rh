from __future__ import annotations

from ..pdf_ministry.bronze import BronzeAsset, BronzeFetcher, BronzeRepository
from .config import IDENTITY

__all__ = ["MiBronzeAsset", "MiBronzeFetcher", "MiBronzeRepository"]

# Alias d\'API stable: le bronze est entièrement partagé (pdf_ministry/bronze.py).
MiBronzeAsset = BronzeAsset
MiBronzeRepository = BronzeRepository


class MiBronzeFetcher(BronzeFetcher):
    def __init__(self, store, ocr_provider, repository, **kwargs):
        super().__init__(IDENTITY, store, ocr_provider, repository, **kwargs)
