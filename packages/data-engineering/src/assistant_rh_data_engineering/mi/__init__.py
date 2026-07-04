"""Corpus MI (Intérieur) — pipeline PDF ministériel (manifest Grist + OCR).

Premier module du pattern per-ministère (issue #246): bronze (dropzone +
conversion PDF + OCR cachée), silver (sectionnement heading-based), gold
(chunks + embeddings API), pipeline (réconciliation manifest <-> base).
"""

from .config import CHUNK_TABLE, CORPUS, MINISTERE, MiPipelineConfig
from .pipeline import MiPipeline

__all__ = ["CHUNK_TABLE", "CORPUS", "MINISTERE", "MiPipeline", "MiPipelineConfig"]
