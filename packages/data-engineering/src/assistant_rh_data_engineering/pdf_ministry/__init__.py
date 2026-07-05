"""Socle medallion partagé des corpus PDF ministériels (Phase D, #248).

Extrait à comportement constant des modules mi/ et masa/ (PR 0 de la Phase D):
l'infra est commune — dropzone, OCR cachée, annotations d'images, réconciliation
manifest Grist <-> base, orchestration bronze/silver/gold, writeback — et chaque
ministère ne porte plus que son identité (MinistryIdentity), ses réglages et ses
divergences de parsing déclarées (config), plus d'éventuels builders spécifiques
(le moteur QNA matte/mso de la PR 1 s'y branche comme silver/gold alternatifs).
"""

from .config import (
    EmbeddingConfig,
    GoldConfig,
    ImageEnrichmentConfig,
    LakePaths,
    MinistryPipelineConfig,
    SilverConfig,
)
from .identity import MinistryIdentity
from .pipeline import MedallionPipeline, plan_reconciliation

__all__ = [
    "EmbeddingConfig",
    "GoldConfig",
    "ImageEnrichmentConfig",
    "LakePaths",
    "MedallionPipeline",
    "MinistryIdentity",
    "MinistryPipelineConfig",
    "SilverConfig",
    "plan_reconciliation",
]
