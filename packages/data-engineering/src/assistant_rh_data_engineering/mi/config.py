from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..pdf_ministry.config import (
    EmbeddingConfig,
    GoldConfig,
    ImageEnrichmentConfig,
    LakePaths,
    MinistryPipelineConfig,
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
    "MI_NAMESPACE",
    "MINISTERE",
    "MiPipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "PUBLISHER",
    "SilverConfig",
]

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

IDENTITY = MinistryIdentity(
    ministere=MINISTERE,
    corpus=CORPUS,
    chunk_source=CHUNK_SOURCE,
    doc_source=DOC_SOURCE,
    publisher=PUBLISHER,
    chunk_table=CHUNK_TABLE,
    namespace=MI_NAMESPACE,
)

# Nom de source pour les préfixes Object Storage (medallion_prefix/sync).
OBJECT_STORAGE_SOURCE_NAME = IDENTITY.object_storage_source_name


@dataclass
class MiPipelineConfig(MinistryPipelineConfig):
    # MI = configuration de référence du socle: aucun réglage divergent (pas
    # de filtre payload, pas d\'enrichissement d\'images — son portage
    # impliquerait le bump du namespace de cache OCR donc un ré-OCR payant du
    # corpus —, règle « zéro chunk => retraiter » conservée).
    paths: LakePaths = field(default_factory=lambda: lake_paths_for(MINISTERE))
    gold: GoldConfig = field(default_factory=lambda: GoldConfig(table_name=CHUNK_TABLE))
