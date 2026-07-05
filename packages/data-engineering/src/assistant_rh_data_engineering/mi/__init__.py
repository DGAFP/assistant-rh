"""Corpus MI (Intérieur) — pipeline PDF ministériel (manifest Grist + OCR).

Premier module du pattern per-ministère (issue #246), réduit à son identité
depuis la factorisation Phase D (#248): l\'infra vit dans pdf_ministry/.
"""

from .config import CHUNK_TABLE, CORPUS, MINISTERE, OBJECT_STORAGE_SOURCE_NAME, LakePaths, MiPipelineConfig
from .pipeline import MiPipeline

# Alias uniformes pour le dispatch de jobs (jobs/pdf_sources_medallion.py):
# chaque package ministère expose Pipeline/PipelineConfig sous le même nom.
Pipeline = MiPipeline
PipelineConfig = MiPipelineConfig

__all__ = [
    "CHUNK_TABLE",
    "CORPUS",
    "LakePaths",
    "MINISTERE",
    "MiPipeline",
    "MiPipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "Pipeline",
    "PipelineConfig",
]
