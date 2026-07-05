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
from ..pdf_ministry.qna.engine import MATTE_EXTRA_HEADING_PATTERNS, QnaEngineConfig

__all__ = [
    "CHUNK_SOURCE",
    "CHUNK_TABLE",
    "CORPUS",
    "DOC_SOURCE",
    "IDENTITY",
    "LakePaths",
    "MATTE_NAMESPACE",
    "MINISTERE",
    "MattePipelineConfig",
    "OBJECT_STORAGE_SOURCE_NAME",
    "PUBLISHER",
    "QNA_ENGINE_CONFIG",
]

# Identité du corpus MATTE — reconstruction Phase D (#248): le corpus legacy
# avait été ingéré par notebook one-shot (sans OCR, champ source erroné,
# 17/44 docs à zéro chunk — audit #103). CHUNK_SOURCE corrige le champ source.
MINISTERE = "matte"
CORPUS = "MATTE"  # valeur source_corpus dans le référentiel Grist
CHUNK_SOURCE = "MATTE"  # AC #248: champ source correct sur tous les chunks
DOC_SOURCE = "matte"
PUBLISHER = "Ministère de l'Aménagement du territoire et de la Transition écologique"
CHUNK_TABLE = "rag_chunks_matte"

# Namespace uuid5 dédié: doc_id/section_id stables entre runs pour un même uid.
MATTE_NAMESPACE = uuid.UUID("0123ab22-5d2e-4427-bd6e-1b422512cf1a")

IDENTITY = MinistryIdentity(
    ministere=MINISTERE,
    corpus=CORPUS,
    chunk_source=CHUNK_SOURCE,
    doc_source=DOC_SOURCE,
    publisher=PUBLISHER,
    chunk_table=CHUNK_TABLE,
    namespace=MATTE_NAMESPACE,
)

OBJECT_STORAGE_SOURCE_NAME = IDENTITY.object_storage_source_name

# Moteur QNA au comportement legacy MATTE (extract_pdf.ipynb): marqueurs
# explicites Q:/R: d'abord, chunks au format 'Q: .../R: ...', composite
# tronqué à 1500, rôle TABLE sur les paragraphes tabulaires, headings
# FICHE n / ANNEXE n. Les modes MSO (table_matrix/faq/process) s'appliquent
# ensuite — les nouveaux docs (FAQ mobilité 2026, logigrammes, calendriers)
# en ont besoin (audit du contenu réel du 05/07/2026).
QNA_ENGINE_CONFIG = QnaEngineConfig(
    modes=("qna_markers", "table_matrix", "faq", "process", "guide"),
    chunk_format="qr",
    composite_max_chars=1500,
    emit_table_chunks=True,
    extra_heading_patterns=MATTE_EXTRA_HEADING_PATTERNS,
)


@dataclass(kw_only=True)
class MattePipelineConfig(MinistryPipelineConfig):
    # Divergences MATTE: moteur QNA legacy (voir QNA_ENGINE_CONFIG),
    # enrichissement d'images actif dès le jour 1 (corpus neuf, pas de cache
    # à invalider), réconciliation convergente (zéro chunk quasi impossible
    # avec le bloc fallback; ingest transactionnel).
    paths: LakePaths = field(default_factory=lambda: lake_paths_for(MINISTERE))
    gold: GoldConfig = field(default_factory=lambda: GoldConfig(table_name=CHUNK_TABLE))
    images: ImageEnrichmentConfig = field(default_factory=lambda: ImageEnrichmentConfig(enabled=True))
    retry_zero_chunk: bool = False
