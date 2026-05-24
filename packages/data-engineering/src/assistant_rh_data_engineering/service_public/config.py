from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class BronzeConfig:
    dataset_api_root: str = "https://www.data.gouv.fr/api/1"
    dataset_slug: str = "service-public-fr-guide-vos-droits-et-demarches-particuliers"
    fallback_zip_url: str = "https://lecomarquage.service-public.gouv.fr/vdd/3.4/part/zip/vosdroits-latest.zip"
    timeout_seconds: int = 60


@dataclass
class SilverConfig:
    min_section_chars: int = 50
    situation_filter: Optional[str] = None


@dataclass
class EmbeddingConfig:
    enable_m3: bool = True
    m3_backend: str = "sentence_transformers"
    m3_model_name: str = "BAAI/bge-m3"
    enable_bge_scaleway: bool = False
    scaleway_model_name: str = "bge-multilingual-gemma2"
    batch_size: int = 32
    normalize: bool = True


@dataclass
class GoldConfig:
    export_parquet: bool = True
    export_npy: bool = True
    table_name: str = "rag_chunks_service_public"


@dataclass
class LakePaths:
    root_dir: Path = Path("data/lake/service_public")

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
class ServicePublicPipelineConfig:
    paths: LakePaths = field(default_factory=LakePaths)
    bronze: BronzeConfig = field(default_factory=BronzeConfig)
    silver: SilverConfig = field(default_factory=SilverConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    gold: GoldConfig = field(default_factory=GoldConfig)
    fiche_ids: Optional[Sequence[str]] = None
