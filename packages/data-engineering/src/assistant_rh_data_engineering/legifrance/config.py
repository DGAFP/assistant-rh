from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BronzeConfig:
    legifrance_code_name: str = "Code général de la fonction publique"
    legifrance_code_id: str = "LEGITEXT000044416551"
    default_legacy_thematique: str = "legifrance"
    prefer_raw_xml: bool = True


@dataclass
class SilverConfig:
    default_thematique: str = "Droit/CGFP"


@dataclass
class EmbeddingConfig:
    enable_m3: bool = False
    m3_backend: str = "sentence_transformers"
    m3_model_name: str = "BAAI/bge-m3"
    enable_bge_scaleway: bool = False
    scaleway_model_name: str = "bge-multilingual-gemma2"
    batch_size: int = 32
    normalize: bool = True


@dataclass
class GoldConfig:
    export_parquet: bool = True
    export_npy: bool = False
    legacy_table_name: str = "rag_chunks_dgafp"
    modern_table_name: str = "rag_chunks_legifrance"
    single_chunk_per_article: bool = True
    max_chunk_chars: int = 1200
    chunk_overlap_chars: int = 100
    min_chunk_chars: int = 350


@dataclass
class LakePaths:
    root_dir: Path = Path("data/lake/legifrance")

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
class LegifrancePipelineConfig:
    paths: LakePaths = field(default_factory=LakePaths)
    bronze: BronzeConfig = field(default_factory=BronzeConfig)
    silver: SilverConfig = field(default_factory=SilverConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    gold: GoldConfig = field(default_factory=GoldConfig)
