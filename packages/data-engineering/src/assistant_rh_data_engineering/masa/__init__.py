"""Corpus MASA (Agriculture et Souveraineté alimentaire) — pipeline PDF
ministériel (manifest Grist + OCR).

Deuxième module du pattern per-ministère (issue #247), réduit à son identité
et ses divergences déclarées depuis la factorisation Phase D (#248): l\'infra
vit dans pdf_ministry/.
"""

from .config import CHUNK_TABLE, CORPUS, MINISTERE, OBJECT_STORAGE_SOURCE_NAME, LakePaths, MasaPipelineConfig
from .pipeline import MasaPipeline

# Alias uniformes pour le dispatch de jobs (jobs/pdf_sources_medallion.py).
Pipeline = MasaPipeline
PipelineConfig = MasaPipelineConfig

__all__ = [
    "CHUNK_TABLE",
    "CORPUS",
    "LakePaths",
    "MINISTERE",
    "MasaPipeline",
    "MasaPipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "Pipeline",
    "PipelineConfig",
]
