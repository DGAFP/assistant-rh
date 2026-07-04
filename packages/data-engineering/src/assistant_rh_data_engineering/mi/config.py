from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Identité du corpus MI (Intérieur) — pipeline PDF ministères, Phase B (#246).
# Chaque ministère porte sa propre identité: ne PAS réutiliser ces constantes
# depuis un autre module (le hardcode SERVICE PUBLIC qui a fui dans MATTE est
# exactement le bug que cette structure évite).
MINISTERE = "mi"
CORPUS = "MI"  # valeur source_corpus dans le référentiel Grist
CHUNK_SOURCE = "MI"  # colonne source des chunks (convention MAJUSCULES des tables existantes)
DOC_SOURCE = "mi"  # colonne source de rag_documents (filtre de réconciliation)
PUBLISHER = "Ministère de l'Intérieur"
CHUNK_TABLE = "rag_chunks_mi"

# Namespace uuid5 dédié: doc_id/section_id stables entre runs pour un même uid.
MI_NAMESPACE = uuid.UUID("8e1a94fc-5f8e-4f0a-9d31-2c6b8f0d41aa")

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
    m3_model_name: str = "BAAI/bge-m3"
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
class MiPipelineConfig:
    paths: LakePaths = field(default_factory=LakePaths)
    silver: SilverConfig = field(default_factory=SilverConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    gold: GoldConfig = field(default_factory=GoldConfig)
    target_env: str = "staging"
    ocr_provider_name: Optional[str] = None  # None => OCR_PROVIDER env ou albert
