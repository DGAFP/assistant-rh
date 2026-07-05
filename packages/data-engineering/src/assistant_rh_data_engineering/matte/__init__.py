"""Corpus MATTE — reconstruction via le pipeline PDF (Phase D, #248).

Infra du socle pdf_ministry/, chunking QNA porté du notebook legacy
(scripts/extract_pdf.ipynb, supprimé au profit de pdf_ministry/qna/).
"""

from .config import CHUNK_TABLE, CORPUS, MINISTERE, OBJECT_STORAGE_SOURCE_NAME, LakePaths, MattePipelineConfig
from .pipeline import MattePipeline

# Alias uniformes pour le dispatch de jobs (jobs/pdf_sources_medallion.py).
Pipeline = MattePipeline
PipelineConfig = MattePipelineConfig

__all__ = [
    "CHUNK_TABLE",
    "CORPUS",
    "LakePaths",
    "MINISTERE",
    "MattePipeline",
    "MattePipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "Pipeline",
    "PipelineConfig",
]
