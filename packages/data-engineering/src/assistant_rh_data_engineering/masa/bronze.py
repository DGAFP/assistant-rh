from __future__ import annotations

from ..pdf_ministry.bronze import BronzeAsset, BronzeFetcher, BronzeRepository
from .config import IDENTITY

__all__ = ["MasaBronzeAsset", "MasaBronzeFetcher", "MasaBronzeRepository"]

# Alias d\'API stable: le bronze est entièrement partagé (pdf_ministry/bronze.py),
# y compris l\'enrichissement VLM des images (divergence MASA activée en config).
MasaBronzeAsset = BronzeAsset
MasaBronzeRepository = BronzeRepository


class MasaBronzeFetcher(BronzeFetcher):
    def __init__(self, store, ocr_provider, repository, **kwargs):
        super().__init__(IDENTITY, store, ocr_provider, repository, **kwargs)
