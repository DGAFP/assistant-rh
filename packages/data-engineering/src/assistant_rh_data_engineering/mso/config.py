from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..pdf_ministry.config import (
    GoldConfig,
    ImageEnrichmentConfig,
    LakePaths,
    MinistryPipelineConfig,
    lake_paths_for,
)
from ..pdf_ministry.identity import MinistryIdentity
from ..pdf_ministry.qna.engine import QnaEngineConfig

__all__ = [
    "CHUNK_SOURCE",
    "CHUNK_TABLE",
    "CORPUS",
    "DOC_SOURCE",
    "IDENTITY",
    "LakePaths",
    "MSO_NAMESPACE",
    "MINISTERE",
    "MsoPipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "PUBLISHER",
    "QNA_ENGINE_CONFIG",
]

# Identité du corpus MSO (Ministères sociaux) — reconstruction Phase D (#248),
# corpus legacy ingéré par notebook one-shot (extract_pdf_MSO.ipynb).
MINISTERE = "mso"
CORPUS = "MSO"  # valeur source_corpus dans le référentiel Grist
CHUNK_SOURCE = "MSO"
DOC_SOURCE = "mso"
PUBLISHER = "Ministères sociaux"
CHUNK_TABLE = "rag_chunks_mso"

# Namespace du notebook legacy conservé (provenance; les doc_id changent de
# toute façon: la formule est désormais celle du socle, clé = uid Grist).
MSO_NAMESPACE = uuid.UUID("c5cdb7de-f8c4-4f8e-9b3d-c9ff7e7d4b72")

IDENTITY = MinistryIdentity(
    ministere=MINISTERE,
    corpus=CORPUS,
    chunk_source=CHUNK_SOURCE,
    doc_source=DOC_SOURCE,
    publisher=PUBLISHER,
    chunk_table=CHUNK_TABLE,
    namespace=MSO_NAMESPACE,
)

OBJECT_STORAGE_SOURCE_NAME = IDENTITY.object_storage_source_name

# Moteur QNA au comportement legacy MSO (extract_pdf_MSO.ipynb): routage
# table_matrix -> faq -> process -> guide, chunks au format
# 'Titre:/Section:/Question utilisateur probable:', composite tronqué à 3000.
QNA_ENGINE_CONFIG = QnaEngineConfig(
    modes=("table_matrix", "faq", "process", "guide"),
    chunk_format="titre_section",
    composite_max_chars=3000,
)


@dataclass(kw_only=True)
class MsoPipelineConfig(MinistryPipelineConfig):
    # Divergences MSO: moteur QNA legacy (voir QNA_ENGINE_CONFIG),
    # enrichissement d'images actif dès le jour 1, réconciliation convergente.
    paths: LakePaths = field(default_factory=lambda: lake_paths_for(MINISTERE))
    gold: GoldConfig = field(default_factory=lambda: GoldConfig(table_name=CHUNK_TABLE))
    images: ImageEnrichmentConfig = field(default_factory=lambda: ImageEnrichmentConfig(enabled=True))
    retry_zero_chunk: bool = False
