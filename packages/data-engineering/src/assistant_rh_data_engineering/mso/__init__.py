"""Corpus MSO (Ministères sociaux) — reconstruction via le pipeline PDF (Phase D, #248).

Infra du socle pdf_ministry/, chunking QNA porté du notebook legacy
(scripts/extract_pdf_MSO.ipynb, supprimé au profit de pdf_ministry/qna/).
"""

from .config import CHUNK_TABLE, CORPUS, MINISTERE, OBJECT_STORAGE_SOURCE_NAME, LakePaths, MsoPipelineConfig
from .pipeline import MsoPipeline

# Alias uniformes pour le dispatch de jobs (jobs/pdf_sources_medallion.py).
Pipeline = MsoPipeline
PipelineConfig = MsoPipelineConfig

__all__ = [
    "CHUNK_TABLE",
    "CORPUS",
    "LakePaths",
    "MINISTERE",
    "MsoPipeline",
    "MsoPipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "Pipeline",
    "PipelineConfig",
]
