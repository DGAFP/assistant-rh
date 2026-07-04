from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Identité du corpus MASA (Agriculture et Souveraineté alimentaire) — pipeline
# PDF ministères, Phase C (#247). Module copié du template MI (mi@44fb22e):
# la logique de parsing/sectionnement est volontairement par-ministère et peut
# diverger; l'infra Grist/OCR/DB reste partagée (utils/).
# Divergences de parsing par rapport au template MI:
# - gold: filtre des chunks à payload utile < MIN_CHUNK_PAYLOAD_CHARS (le
#   corpus MASA est riche en supports type slides — images seules, titres de
#   slide sans corps, pages de garde d'annexes; voir masa/gold.py).
# - bronze: enrichissement des images OCR (206 images sur le lot initial, dont
#   les copies d'écran RenoiRH des supports de formation): OCR avec
#   include_image_base64 (namespace de cache dédié -img) + annotation VLM
#   Albert cachée en bronze; les descriptions remplacent les références
#   d'images avant sectionnement (voir utils/image_annotation.py).
# Chaque ministère porte sa propre identité: ne PAS réutiliser ces constantes
# depuis un autre module (le hardcode SERVICE PUBLIC qui a fui dans MATTE est
# exactement le bug que cette structure évite).
MINISTERE = "masa"
CORPUS = "MASA"  # valeur source_corpus dans le référentiel Grist
CHUNK_SOURCE = "MASA"  # colonne source des chunks (convention MAJUSCULES des tables existantes)
DOC_SOURCE = "masa"  # colonne source de rag_documents (filtre de réconciliation)
PUBLISHER = "Ministère de l'Agriculture et de la Souveraineté alimentaire"
CHUNK_TABLE = "rag_chunks_masa"

# Namespace uuid5 dédié: doc_id/section_id stables entre runs pour un même uid.
MASA_NAMESPACE = uuid.UUID("bf808189-0d1a-499e-aa13-a53196c362e3")

# Nom de source pour les préfixes Object Storage (medallion_prefix/sync).
OBJECT_STORAGE_SOURCE_NAME = f"pdf_sources/{MINISTERE}"


@dataclass
class LakePaths:
    root_dir: Path = Path(f"data/lake/pdf_sources/{MINISTERE}")

    @property
    def bronze_dir(self) -> Path:
        return self.root_dir / "bronze"

    @property
    def silver_dir(self) -> Path:
        return self.root_dir / "silver"

    @property
    def gold_dir(self) -> Path:
        return self.root_dir / "gold"


@dataclass
class SilverConfig:
    min_section_chars: int = 50


@dataclass
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


@dataclass
class GoldConfig:
    export_parquet: bool = True
    export_npy: bool = True
    table_name: str = CHUNK_TABLE
    chunk_max_chars: int = 1200
    chunk_overlap: int = 200


@dataclass
class ImageEnrichmentConfig:
    # Annotation VLM des crops d'images OCR (divergence MASA). L'annotation
    # native du contrat Mistral OCR (bbox_annotation_format) est cassée côté
    # Albert (validations de schéma incompatibles en cascade, smoke test
    # 2026-07-04): on passe par include_image_base64 + /chat/completions.
    enabled: bool = True
    vlm_model: str = field(default_factory=lambda: os.getenv("ALBERT_VISION_MODEL", "openweight-medium"))
    # Garde-fou coût: au-delà, les références d'images restent telles quelles.
    max_images_per_doc: int = 150


@dataclass
class MasaPipelineConfig:
    paths: LakePaths = field(default_factory=LakePaths)
    silver: SilverConfig = field(default_factory=SilverConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    gold: GoldConfig = field(default_factory=GoldConfig)
    images: ImageEnrichmentConfig = field(default_factory=ImageEnrichmentConfig)
    target_env: str = "staging"
    ocr_provider_name: Optional[str] = None  # None => OCR_PROVIDER env ou albert
