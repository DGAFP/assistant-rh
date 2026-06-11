from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional, Sequence

from .bronze import BronzeRepository, ServicePublicXmlFetcher
from .config import ServicePublicPipelineConfig
from .db import ServicePublicDbWriter
from .gold import GoldRepository, ServicePublicGoldBuilder
from .silver import ServicePublicSilverBuilder, SilverRepository


class ServicePublicPipeline:
    """
    Medallion pipeline for Service-Public.

    Bronze:
      official DILA ZIP + raw XML files
    Silver:
      rag_documents / rag_sections shaped records
    Gold:
      retrieval chunks + embeddings + export files
    """

    def __init__(self, config: Optional[ServicePublicPipelineConfig] = None):
        self.config = config or ServicePublicPipelineConfig()
        self.fetcher = ServicePublicXmlFetcher(self.config.bronze)
        self.bronze_repo = BronzeRepository(self.config.paths.bronze_dir)
        self.silver_builder = ServicePublicSilverBuilder(self.config.silver)
        self.silver_repo = SilverRepository(self.config.paths.silver_dir)
        self.gold_builder = ServicePublicGoldBuilder(self.config.embeddings, self.config.gold)
        self.gold_repo = GoldRepository(self.config.paths.gold_dir)

    def run_bronze(self, fiche_ids: Optional[Sequence[str]] = None):
        return self.fetcher.fetch_to_repository(self.bronze_repo, fiche_ids or self.config.fiche_ids)

    def run_silver(self, bronze_assets: list[Any]):
        return self.silver_builder.persist_bundles(self.silver_repo, bronze_assets)

    def run_gold(self, silver_bundles: list[Any]):
        return self.gold_builder.persist_bundles(self.gold_repo, silver_bundles)

    def run_all(self, fiche_ids: Optional[Sequence[str]] = None) -> dict[str, Any]:
        bronze_assets = self.run_bronze(fiche_ids=fiche_ids)
        silver_bundles = self.run_silver(bronze_assets)
        gold_bundles = self.run_gold(silver_bundles)
        return {
            "config": asdict(self.config),
            "bronze_assets": len(bronze_assets),
            "silver_documents": len(silver_bundles),
            "gold_documents": len(gold_bundles),
            "gold_chunks": sum(len(bundle.chunks) for bundle in gold_bundles),
        }

    def ingest_from_silver_and_gold(self, silver_bundles: list[Any], gold_bundles: list[Any], schema: str = "public") -> dict[str, int]:
        from assistant_rh_data_engineering.jobs.service_public_ingestion import remap_existing_document_ids

        writer = ServicePublicDbWriter(schema=schema)
        documents = [bundle.document for bundle in silver_bundles]
        sections = [section for bundle in silver_bundles for section in bundle.sections]
        chunks = [chunk for bundle in gold_bundles for chunk in bundle.chunks]
        short_ids = [str(document.get("short_id", "")).strip().upper() for document in documents if document.get("short_id")]
        existing_doc_ids_by_short_id = writer.list_document_ids_by_short_id(short_ids)
        remap_existing_document_ids(
            documents,
            sections,
            chunks,
            existing_doc_ids_by_short_id,
            writer.list_section_ids_by_doc_id_and_index(list(existing_doc_ids_by_short_id.values())),
        )
        return {
            "documents": writer.upsert_documents(documents),
            "sections": writer.upsert_sections(sections),
            "chunks": writer.upsert_chunks(chunks),
        }
