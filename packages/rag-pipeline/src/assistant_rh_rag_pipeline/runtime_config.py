"""Pure historical DB configuration values and mapping, without persistence."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict

from .config import ContextMode, RAGConfig, SearchMode, SnapshotConfig, get_default_config


@dataclass
class RuntimeRAGConfig(SnapshotConfig):
    """
    Flat runtime configuration stored as JSONB in rag_config.

    This is the *admin-facing* config (all V1/V2/V3 params).  The pipeline
    only reads the V3-relevant subset via ``get_runtime_config()``.
    """

    rag_version: str = "v3"
    chunk_selection_mode: str = "llm_selector"
    llm_selector_model: str = "openweight-medium"
    llm_selector_max_chunks: int = 50
    llm_selector_prompt_name: str = "curator_default.md"
    enable_context_expansion: bool = True
    enable_regulatory_search: bool = False
    enable_fallback_v2: bool = True
    relevance_threshold: float = 0.3
    top_k: int = 7
    retrieval_mode: str = "semantic"
    hybrid_alpha: float = 0.5
    embedding_model: str = "albert"
    embedding_fallback: str = "bge_scaleway"
    use_reranker: bool = True
    reranker_name: str = "albert"
    rerank_top_k: int = 5
    enable_deduplication: bool = True
    dedup_threshold: float = 0.95
    enable_mmr: bool = True
    mmr_lambda: float = 0.5
    enable_boosting: bool = False
    enable_adaptive_boosting: bool = False
    boost_matte: float = 1.2
    boost_service_public: float = 1.0
    boost_dgafp: float = 1.0
    boost_rgrh: float = 1.0
    enable_source_diversity: bool = False
    min_sources_per_query: int = 2
    force_matte_after_rerank: bool = False
    enable_query_expansion: bool = True
    enable_query_rewriting: bool = False
    enable_hyde: bool = False
    enable_intent_gating: bool = False
    intent_model: str = "albert-small"
    intent_confidence_threshold: float = 0.7
    intent_gating_prompt_name: str = "intent_default.md"
    enable_query_reformulation: bool = False
    reformulation_model: str = "albert-small"
    reformulation_add_jurisdiction: bool = True
    reformulation_add_temporal: bool = True
    reformulation_include_acronyms: bool = True
    v3_tables: list[str] | tuple[str, ...] | None = field(default_factory=lambda: ["matte", "service_public", "dgafp", "rgrh"])
    v3_context_mode: str = "standard"
    v3_token_budget: int = 8000
    v3_doc_entire_threshold: int = 3500
    v3_search_mode: str = "semantic"
    v3_enable_escalation: bool = True
    v3_enable_selector: bool = True
    v3_triangulation_sections: int = 2
    # Defaults alignés sur la config VALIDÉE (candidate_v2, run 115): 30 chunks
    # amont / 20 sections après rerank. Ces valeurs seedent un rag_config neuf
    # (INSERT-if-empty) et servent de repli si la clé manque du JSONB — un env
    # frais est ainsi cohérent avec l'éval, au lieu de 10/5. NB: les lignes
    # rag_config existantes (staging/prod) portent des valeurs explicites et ne
    # sont PAS modifiées par ce changement — elles se règlent via update_rag_config.
    v3_initial_top_k: int = 30
    v3_enable_reranker: bool = True
    v3_rerank_top_k: int = 20
    # Entrée du reranker (candidats vus), découplée de la sortie top_k.
    v3_rerank_input_k: int = 20
    v3_alpha: float = 0.5
    v3_selector_model: str = "openweight-large"
    v3_selector_prompt_name: str = "v3_selector_business.md"
    v3_intent_prompt_name: str = "intent_unified.md"
    v3_generator_model: str = "openweight-large"
    v3_temperature: float = 0.0
    v3_system_prompt_name: str = "system_prompt_V6_optimized.md"
    verbose_mode: bool = False
    enable_cache: bool = True
    llm_provider: str = "albert"
    llm_model: str = "albert-large"
    temperature: float = 0.0
    system_prompt_name: str = "system_prompt.md"
    data_source: str = "all"
    updated_at: str = ""
    updated_by: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "v3_tables": None if self.v3_tables is None else list(self.v3_tables)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeRAGConfig":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid})


def runtime_config_to_rag_config(runtime_config: RuntimeRAGConfig | None = None) -> RAGConfig:
    """Map the admin-facing runtime config to the pipeline's nested ``RAGConfig``.

    Streamlit and offline evaluation must interpret the database-backed
    ``rag_config`` row the same way; keeping the mapping here prevents the two
    paths from silently drifting.
    """
    runtime_config = runtime_config or RuntimeRAGConfig()
    config = get_default_config()

    mode_map = {"standard": ContextMode.STANDARD, "wide": ContextMode.WIDE}
    config.context.context_mode = mode_map.get(runtime_config.v3_context_mode, ContextMode.STANDARD)
    config.context.token_budget = runtime_config.v3_token_budget
    config.context.doc_entire_threshold = runtime_config.v3_doc_entire_threshold
    config.context.triangulation_sections = runtime_config.v3_triangulation_sections

    config.selector.enabled = runtime_config.v3_enable_selector
    config.selector.model = runtime_config.v3_selector_model
    config.selector.prompt_name = runtime_config.v3_selector_prompt_name

    config.query_processor.enable_intent_gating = runtime_config.enable_intent_gating
    config.query_processor.enable_acronym_expansion = runtime_config.enable_query_expansion
    config.query_processor.intent_prompt_name = runtime_config.v3_intent_prompt_name
    config.query_processor.enable_hyde = runtime_config.enable_hyde

    config.retrieval.tables = list(runtime_config.v3_tables or ["matte", "service_public", "dgafp", "rgrh"])
    config.retrieval.initial_top_k = runtime_config.v3_initial_top_k
    config.retrieval.alpha = runtime_config.v3_alpha
    search_mode_map = {"semantic": SearchMode.SEMANTIC, "hybrid": SearchMode.HYBRID, "lexical": SearchMode.LEXICAL}
    config.retrieval.search_mode = search_mode_map.get(runtime_config.v3_search_mode, SearchMode.SEMANTIC)

    config.aggregation.enable_section_reranker = runtime_config.v3_enable_reranker
    config.aggregation.section_rerank_top_k = runtime_config.v3_rerank_top_k
    config.aggregation.rerank_input_k = runtime_config.v3_rerank_input_k

    config.generation.model = runtime_config.v3_generator_model
    config.generation.temperature = runtime_config.v3_temperature
    config.generation.system_prompt_name = runtime_config.v3_system_prompt_name
    config.verbose = runtime_config.verbose_mode
    return config
