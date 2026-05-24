from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from ..utils.gold import GoldRepository
from .bronze import BronzeRepository, LegifranceBronzeBuilder
from .config import LegifrancePipelineConfig
from .db import writer_from_gold_config
from .gold import LegifranceGoldBuilder
from .silver import LegifranceSilverBuilder, SilverRepository


class LegifrancePipeline:
    """
    Local medallion pipeline for Legifrance corpora.

    Bronze:
      local raw articles JSON/XML and local legacy text files
    Silver:
      normalized rag_documents / rag_sections documents
    Gold:
      stable retrieval chunks projected to rag_chunks_dgafp and rag_chunks_legifrance
    """

    def __init__(self, config: Optional[LegifrancePipelineConfig] = None):
        self.config = config or LegifrancePipelineConfig()
        self.bronze_builder = LegifranceBronzeBuilder(self.config.bronze)
        self.bronze_repo = BronzeRepository(self.config.paths.bronze_dir)
        self.silver_builder = LegifranceSilverBuilder(self.config.silver)
        self.silver_repo = SilverRepository(self.config.paths.silver_dir)
        self.gold_builder = LegifranceGoldBuilder(self.config.embeddings, self.config.gold)
        self.gold_repo = GoldRepository(self.config.paths.gold_dir)

    def run_bronze(self):
        return self.bronze_builder.fetch_to_repository(self.bronze_repo)

    def run_silver(self, bronze_assets: list[Any]):
        return self.silver_builder.persist_bundles(self.silver_repo, bronze_assets)

    def run_gold(self, silver_bundles: list[Any]):
        return self.gold_builder.persist_bundles(self.gold_repo, silver_bundles)

    def run_all(self) -> dict[str, Any]:
        bronze_assets = self.run_bronze()
        silver_bundles = self.run_silver(bronze_assets)
        gold_bundles = self.run_gold(silver_bundles)
        return {
            "config": asdict(self.config),
            "bronze_assets": len(bronze_assets),
            "silver_documents": len(silver_bundles),
            "gold_documents": len(gold_bundles),
            "gold_chunks": sum(len(bundle.chunks) for bundle in gold_bundles),
        }

    def ingest_from_silver_and_gold(
        self,
        silver_bundles: list[Any],
        gold_bundles: list[Any],
        schema: str = "public",
        dsn: str | None = None,
    ) -> dict[str, int]:
        writer = writer_from_gold_config(self.config.gold, schema=schema, dsn=dsn)
        documents = [bundle.document for bundle in silver_bundles]
        sections = [section for bundle in silver_bundles for section in bundle.sections]
        chunks = [chunk for bundle in gold_bundles for chunk in bundle.chunks]
        return {
            "documents": writer.upsert_documents(documents),
            "sections": writer.upsert_sections(sections),
            "legacy_chunks": writer.upsert_legacy_chunks(chunks),
            "modern_chunks": writer.upsert_modern_chunks(chunks),
        }
