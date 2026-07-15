from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..pdf_ministry.config import (
    EmbeddingConfig,
    GoldConfig,
    ImageEnrichmentConfig,
    LakePaths,
    MinistryPipelineConfig,
    PageVisionConfig,
    SilverConfig,
    lake_paths_for,
)
from ..pdf_ministry.identity import MinistryIdentity

__all__ = [
    "CHUNK_SOURCE",
    "CHUNK_TABLE",
    "CORPUS",
    "DOC_SOURCE",
    "EmbeddingConfig",
    "GoldConfig",
    "IDENTITY",
    "ImageEnrichmentConfig",
    "LakePaths",
    "MASA_NAMESPACE",
    "MIN_CHUNK_PAYLOAD_CHARS",
    "MINISTERE",
    "MasaPipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "PageVisionConfig",
    "PUBLISHER",
    "SilverConfig",
]

# Identité du corpus MASA (Agriculture et Souveraineté alimentaire) — pipeline
# PDF ministères, Phase C (#247).
MINISTERE = "masa"
CORPUS = "MASA"  # valeur source_corpus dans le référentiel Grist
CHUNK_SOURCE = "MASA"  # colonne source des chunks (convention MAJUSCULES des tables existantes)
DOC_SOURCE = "masa"  # colonne source de rag_documents (filtre de réconciliation)
PUBLISHER = "Ministère de l'Agriculture et de la Souveraineté alimentaire"
CHUNK_TABLE = "rag_chunks_masa"

# Namespace uuid5 dédié: doc_id/section_id stables entre runs pour un même uid.
MASA_NAMESPACE = uuid.UUID("bf808189-0d1a-499e-aa13-a53196c362e3")

IDENTITY = MinistryIdentity(
    ministere=MINISTERE,
    corpus=CORPUS,
    chunk_source=CHUNK_SOURCE,
    doc_source=DOC_SOURCE,
    publisher=PUBLISHER,
    chunk_table=CHUNK_TABLE,
    namespace=MASA_NAMESPACE,
)

# Nom de source pour les préfixes Object Storage (medallion_prefix/sync).
OBJECT_STORAGE_SOURCE_NAME = IDENTITY.object_storage_source_name

# Divergence MASA (audit du lot réel, 2026-07-04): corpus riche en supports
# type slides — chunks sans payload utile filtrés en gold.
MIN_CHUNK_PAYLOAD_CHARS = 25


@dataclass(kw_only=True)
class MasaPipelineConfig(MinistryPipelineConfig):
    # Divergences MASA par rapport à la config de référence (MI):
    # - gold: filtre des chunks à payload utile < MIN_CHUNK_PAYLOAD_CHARS
    #   (images seules, titres de slide sans corps, pages de garde d\'annexes);
    # - bronze: enrichissement VLM des images OCR (206 images sur le lot
    #   initial, dont les copies d\'écran RenoiRH) — namespace de cache OCR
    #   dédié (-img), annotations cachées en bronze;
    # - réconciliation: pas d\'exigence nb_chunks > 0 pour ignore_inchange —
    #   un doc légitimement à zéro chunk (image-only après filtre payload)
    #   converge au lieu d\'être retraité à chaque run.
    # - page_vision: re-passe vision pleine page des slides à schémas (flèches,
    #   tableaux 2 colonnes) que l'OCR aplatit — ex. mapping CONTRAT/AVENANT
    #   perdu (constat 2026-07-15). Reconstruction VLM des pages à risque.
    paths: LakePaths = field(default_factory=lambda: lake_paths_for(MINISTERE))
    gold: GoldConfig = field(default_factory=lambda: GoldConfig(table_name=CHUNK_TABLE, min_chunk_payload_chars=MIN_CHUNK_PAYLOAD_CHARS))
    images: ImageEnrichmentConfig = field(default_factory=lambda: ImageEnrichmentConfig(enabled=True))
    page_vision: PageVisionConfig = field(default_factory=lambda: PageVisionConfig(enabled=True))
    retry_zero_chunk: bool = False
