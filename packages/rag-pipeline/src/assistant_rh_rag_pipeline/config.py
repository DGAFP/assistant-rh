"""
Configuration dataclasses for the RAG V3 Clean pipeline.

Pure configuration – no DB access, no I/O.  All database helpers live in
``db_helpers.py`` and are re-exported here for backward compatibility.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List

DEFAULT_GENERATOR_PROMPT_NAME = "system_prompt_V7_gestionnaires_rh.md"

# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class SearchMode(str, Enum):
    SEMANTIC = "semantic"
    LEXICAL = "lexical"
    HYBRID = "hybrid"


class EmbeddingModel(str, Enum):
    ALBERT = "albert"
    BGE_SCALEWAY = "bge_scaleway"


class LLMProvider(str, Enum):
    ALBERT = "albert"
    SCALEWAY = "scaleway"
    MISTRAL = "mistral"


class ContextMode(str, Enum):
    STANDARD = "standard"
    WIDE = "wide"


# ─────────────────────────────────────────────────────────────────────────────
# Table definitions for the 4 DE chunk tables
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChunkTable:
    """Schema descriptor for one of the Data Engineer's chunk tables."""

    name: str
    id_col: str = "hash_id"
    text_col: str = "chunk_text"
    embed_col_albert: str = "embedding"
    embed_col_bge: str = "embedding_bge_scw"
    tsv_col: str = ""
    publisher: str = ""
    has_sections: bool = False


CHUNK_TABLES: Dict[str, ChunkTable] = {
    "matte": ChunkTable(
        "rag_chunks_matte",
        embed_col_albert="embedding_m3",
        tsv_col="text_tsv",
        publisher="MATTE",
        has_sections=True,
    ),
    "mso": ChunkTable(
        "rag_chunks_mso",
        embed_col_albert="embedding_m3",
        tsv_col="text_tsv",
        publisher="MSO",
        has_sections=True,
    ),
    "mi": ChunkTable(
        "rag_chunks_mi",
        embed_col_albert="embedding_m3",
        tsv_col="text_tsv",
        publisher="MI",
        has_sections=True,
    ),
    "masa": ChunkTable(
        "rag_chunks_masa",
        embed_col_albert="embedding_m3",
        tsv_col="text_tsv",
        publisher="MASA",
        has_sections=True,
    ),
    "service_public": ChunkTable(
        "rag_chunks_service_public",
        embed_col_albert="embedding_m3",
        tsv_col="text_tsv",
        publisher="Service-Public",
        has_sections=True,
    ),
    "service_public_scw": ChunkTable(
        os.getenv("SERVICE_PUBLIC_COMPARE_TABLE", "rag_chunks_service_public_scw"),
        embed_col_albert="embedding_m3",
        tsv_col="text_tsv",
        publisher="Service-Public (Scaleway)",
        has_sections=False,
    ),
    "dgafp": ChunkTable(
        "rag_chunks_dgafp",
        id_col="chunk_id",
        embed_col_albert="embedding_m3",
        tsv_col="chunk_text_tsv",
        publisher="DGAFP",
        has_sections=False,
    ),
    "dgafp_scw": ChunkTable(
        os.getenv("DGAFP_COMPARE_TABLE", "rag_chunks_dgafp_scw"),
        id_col="chunk_id",
        embed_col_albert="embedding_m3",
        tsv_col="chunk_text_tsv",
        publisher="DGAFP (Scaleway)",
        has_sections=False,
    ),
    "rgrh": ChunkTable("rag_chunks_rgrh", embed_col_albert="embedding_m3", tsv_col="text_tsv", publisher="RGRH", has_sections=False),
}

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline config dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RetrievalConfig:
    search_mode: SearchMode = SearchMode.SEMANTIC
    embedding_model: EmbeddingModel = EmbeddingModel.ALBERT
    initial_top_k: int = 30
    # Nombre de listes IVFFLAT sondées par requête vectorielle. Sans SET
    # explicite, PostgreSQL utilise probes=1: sur nos index lists=100, chaque
    # recherche ne scanne qu'1 % des listes — recall silencieusement amputé.
    # 15 -> 5 (06/07/2026): sweep offline du goldset — probes=5 est le point
    # d'équilibre (recall MATTE 0,92 comme probes=15, mais bruit Alan 0,25 vs
    # 0,58 ; probes=1 perd du recall à 0,83). Ablation appariée: probes 15->5
    # remonte MATTE de 0,56 à 0,78 (moins de bruit dans le pool du sélecteur).
    # 0 = laisser le défaut serveur.
    ivfflat_probes: int = 5
    alpha: float = 0.5
    tables: List[str] = field(default_factory=lambda: ["matte", "service_public", "dgafp", "rgrh"])
    enable_chunk_reranker: bool = False
    chunk_rerank_top_k: int = 30
    enable_selector_retry: bool = True
    selector_retry_search_mode: SearchMode = SearchMode.HYBRID
    selector_retry_top_k: int = 30

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "search_mode": self.search_mode.value,
            "embedding_model": self.embedding_model.value,
            "selector_retry_search_mode": self.selector_retry_search_mode.value,
        }


@dataclass
class SectionAggregationConfig:
    weight_max_score: float = 0.5
    weight_mean_score: float = 0.3
    weight_chunk_count: float = 0.2
    enable_section_reranker: bool = True
    # 10 -> 16 (06/07/2026): c'était l'étage rigide de l'entonnoir — le run 54
    # a élargi l'amont (chunks bruts 110->160 via probes+initial_top_k) mais
    # les sections offertes au sélecteur restaient plafonnées à 10.0 pile,
    # étranglant la conversion des gains de retrieval (hit_avg 0.36->0.43 sans
    # effet sur le pass). L'entrée du sélecteur ne pèse que ~6 800 tokens pour
    # 10 sections (fenêtre gpt-oss-120b: 131k).
    # 16 -> 20 (06/07/2026): alignement sur la config MESURÉE — l'A/B (runs
    # 58/59) et le run de référence candidate_v2 (run 115) ont validé 20 via
    # --section-rerank-top-k; 16 ne correspondait à aucun run. NB: la valeur
    # runtime v3_rerank_top_k écrase ce défaut à l'exécution — la ligne
    # rag_config partagée doit être alignée à 20 pour que la prod serve la
    # config validée (sinon l'entonnoir reste étranglé à 10).
    section_rerank_top_k: int = 20
    # ENTRÉE du reranker, découplée de sa sortie (funnel 17/07 : le pré-filtre
    # à 20 = cause directe de 3 échecs — q17/q192/q218, sections-réponse aux
    # pré-rangs 32-45 jamais VUES par le reranker ; contrefactuel Albert :
    # q17 → rangs 2-4 à 0.9996, q192 → rang 1). Défaut 20 = comportement prod
    # historique ; la cible A/B (vague 1) est 40 via v3_rerank_input_k.
    # Prérequis plomberie de R2 : un article remonté par résumé au rang ~25
    # doit pouvoir franchir cette entrée.
    rerank_input_k: int = 20

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContextBuildConfig:
    context_mode: ContextMode = ContextMode.STANDARD

    # Standard mode defaults
    token_budget: int = 8000
    max_full_docs: int = 1
    doc_entire_threshold: int = 3500
    max_sections: int = 12
    triangulation_sections: int = 2
    legal_refs_budget: int = 1000

    # Wide mode overrides
    token_budget_wide: int = 12000
    max_full_docs_wide: int = 2
    # 5000 -> 9000 (05/07/2026): les doc_markdown des fiches reconstruites par
    # OCR (Phase D) sont ~3-5 % plus longs que les legacy — les fiches clés
    # (ex: fiche MATTE 6 = 5144 tokens) passaient juste au-dessus du seuil et
    # perdaient l'injection en doc entier (le générateur recevait une section
    # de ~300 tokens au lieu de la fiche complète => « je n'ai pas trouvé »
    # sur du contenu présent). 9000 récupère la bande 5000-9000 (8 docs MATTE
    # dont les fiches) en restant sous le token_budget_wide.
    doc_entire_threshold_wide: int = 9000
    max_sections_wide: int = 20
    legal_refs_budget_wide: int = 2000

    def get_token_budget(self) -> int:
        return self.token_budget_wide if self.context_mode == ContextMode.WIDE else self.token_budget

    def get_max_full_docs(self) -> int:
        return self.max_full_docs_wide if self.context_mode == ContextMode.WIDE else self.max_full_docs

    def get_doc_entire_threshold(self) -> int:
        return self.doc_entire_threshold_wide if self.context_mode == ContextMode.WIDE else self.doc_entire_threshold

    def get_max_sections(self) -> int:
        return self.max_sections_wide if self.context_mode == ContextMode.WIDE else self.max_sections

    def get_legal_refs_budget(self) -> int:
        return self.legal_refs_budget_wide if self.context_mode == ContextMode.WIDE else self.legal_refs_budget

    def to_dict(self) -> dict:
        d = asdict(self)
        d["context_mode"] = self.context_mode.value
        return d


@dataclass
class SelectorConfig:
    """Optional LLM-based source filter (toggle)."""

    enabled: bool = False
    provider: LLMProvider = LLMProvider.ALBERT
    model: str = "openweight-large"
    temperature: float = 0.0
    prompt_name: str = "v3_selector_business.md"
    # Plancher de sections servies au générateur quand le sélecteur a gardé
    # quelque chose (complément au rang d'agrégation).
    # 4 -> 0 (06/07/2026, DÉSACTIVÉ): forcer des sections que le sélecteur a
    # écartées est risqué en corpus RÉGLEMENTAIRE — quand le pool est bruité et
    # que le sélecteur rejette, servir les top-N par score peut pousser le
    # générateur à INVENTER depuis un contexte marginal (mieux vaut un refus
    # honnête). L'ablation appariée du goldset le confirme au pass: min_kept 4->0
    # remonte MATTE et Service-Public. Tension connue: MSO bénéficiait du plancher
    # (0,60 -> 0,40) — à traiter par un override config PAR MINISTÈRE (backlog),
    # pas par un plancher global. 0 = désactivé.
    min_kept_sections: int = 0

    def to_dict(self) -> dict:
        return {**asdict(self), "provider": self.provider.value}


@dataclass
class GenerationConfig:
    provider: LLMProvider = LLMProvider.ALBERT
    model: str = "openweight-large"
    temperature: float = 0.0
    system_prompt_name: str = DEFAULT_GENERATOR_PROMPT_NAME
    fallback_provider: LLMProvider = LLMProvider.SCALEWAY
    fallback_model: str = "llama-3.1-70b-instruct"

    def to_dict(self) -> dict:
        return {**asdict(self), "provider": self.provider.value, "fallback_provider": self.fallback_provider.value}


@dataclass
class QueryProcessorConfig:
    enable_acronym_expansion: bool = True
    enable_intent_gating: bool = True
    intent_model: str = "openweight-medium"
    intent_prompt_name: str = "intent_unified.md"
    enable_hyde: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RAGConfig:
    """Complete pipeline configuration for RAG V3 Clean."""

    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    aggregation: SectionAggregationConfig = field(default_factory=SectionAggregationConfig)
    context: ContextBuildConfig = field(default_factory=ContextBuildConfig)
    selector: SelectorConfig = field(default_factory=SelectorConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    query_processor: QueryProcessorConfig = field(default_factory=QueryProcessorConfig)
    verbose: bool = False

    def to_dict(self) -> dict:
        return {
            "retrieval": self.retrieval.to_dict(),
            "aggregation": self.aggregation.to_dict(),
            "context": self.context.to_dict(),
            "selector": self.selector.to_dict(),
            "generation": self.generation.to_dict(),
            "query_processor": self.query_processor.to_dict(),
            "verbose": self.verbose,
        }


def get_default_config() -> RAGConfig:
    return RAGConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Re-exports from db_helpers for backward compatibility
# ─────────────────────────────────────────────────────────────────────────────
# Modules that import e.g. `from .config import get_dsn` will keep working.

from .db_helpers import (  # noqa: E402, F401
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_TYPES,
    _db_conn,
    get_acronym_dict,
    get_dsn,
    get_prompt_content,
    get_runtime_config,
    list_prompts,
    list_system_prompts,
    load_prompt,
    save_prompt,
    today_fr,
    update_runtime_config,
)
