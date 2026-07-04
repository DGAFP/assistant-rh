"""Corpus MASA (Agriculture et Souveraineté alimentaire) — pipeline PDF
ministériel (manifest Grist + OCR).

Deuxième module du pattern per-ministère (issue #247), copié du template MI:
bronze (dropzone + conversion PDF + OCR cachée), silver (sectionnement
heading-based), gold (chunks + embeddings API), pipeline (réconciliation
manifest <-> base). Les divergences de parsing propres à MASA se documentent
dans config.py au fil des audits de corpus.
"""

from .config import CHUNK_TABLE, CORPUS, MINISTERE, MasaPipelineConfig
from .pipeline import MasaPipeline

__all__ = ["CHUNK_TABLE", "CORPUS", "MINISTERE", "MasaPipeline", "MasaPipelineConfig"]
