"""Configuration partagée des pipelines PDF ministériels.

Les divergences par ministère sont des réglages déclarés ici — pas des forks de
code: min_chunk_payload_chars (filtre MASA anti-bruit slides), images.enabled
(enrichissement VLM), retry_zero_chunk (règle de réconciliation).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def lake_paths_for(ministere: str) -> "LakePaths":
    return LakePaths(root_dir=Path(f"data/lake/pdf_sources/{ministere}"))


@dataclass
class LakePaths:
    root_dir: Path

    @property
    def bronze_dir(self) -> Path:
        return self.root_dir / "bronze"

    @property
    def silver_dir(self) -> Path:
        return self.root_dir / "silver"

    @property
    def gold_dir(self) -> Path:
        return self.root_dir / "gold"


@dataclass(kw_only=True)
class SilverConfig:
    min_section_chars: int = 50


@dataclass(kw_only=True)
class EmbeddingConfig:
    # BGE-M3 via l'API Albert: mêmes vecteurs que le retrieval, sans
    # sentence-transformers dans l'image du job (image légère, décision #246).
    enable_m3: bool = True
    m3_backend: str = "albert_api"
    # Impérativement le MEME modèle que l'embedding des requêtes au retrieval
    # (rag-pipeline/embedder.py, env ALBERT_EMBED_MODEL): deux modèles
    # différents = deux espaces vectoriels incomparables dans embedding_m3.
    m3_model_name: str = field(default_factory=lambda: os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings"))
    # Colonne de secours embedding_bge_scw via l'API Scaleway existante.
    enable_bge_scaleway: bool = True
    scaleway_model_name: str = "bge-multilingual-gemma2"
    batch_size: int = 32
    normalize: bool = True


@dataclass(kw_only=True)
class GoldConfig:
    table_name: str
    export_parquet: bool = True
    export_npy: bool = True
    chunk_max_chars: int = 1200
    chunk_overlap: int = 200
    # Divergence MASA (audit lot réel 2026-07-04): filtre des chunks sans
    # payload utile (images seules, titres de slide sans corps). 0 = inactif.
    min_chunk_payload_chars: int = 0


@dataclass(kw_only=True)
class ImageEnrichmentConfig:
    # Annotation VLM des crops d'images OCR. L'annotation native du contrat
    # Mistral OCR (bbox_annotation_format) est cassée côté Albert (validations
    # de schéma incompatibles en cascade, smoke test 2026-07-04): on passe par
    # include_image_base64 + /chat/completions (utils/image_annotation.py).
    enabled: bool = False
    vlm_model: str = field(default_factory=lambda: os.getenv("ALBERT_VISION_MODEL", "openweight-medium"))
    # Garde-fou coût: au-delà, les références d'images restent telles quelles.
    max_images_per_doc: int = 150


@dataclass(kw_only=True)
class MinistryPipelineConfig:
    paths: LakePaths
    gold: GoldConfig
    silver: SilverConfig = field(default_factory=SilverConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    images: ImageEnrichmentConfig = field(default_factory=ImageEnrichmentConfig)
    target_env: str = "staging"
    ocr_provider_name: Optional[str] = None  # None => OCR_PROVIDER env ou albert
    # Règle de réconciliation « zéro chunk => retraiter » (leçon de l'audit
    # MATTE, pour des ingestions legacy non atomiques). Les corpus dont le
    # filtre payload rend légitime un doc à zéro chunk la désactivent pour
    # converger (divergence MASA).
    retry_zero_chunk: bool = True
