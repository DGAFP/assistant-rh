"""Pure, immutable reconstruction of the historical nested RAG configuration.

Defaults and serialization match rag_pipeline.config; the admin mapping lives in
core.rag_configuration. No environment reads, database helpers or legacy imports.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum


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


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    search_mode: SearchMode = SearchMode.SEMANTIC
    embedding_model: EmbeddingModel = EmbeddingModel.ALBERT
    initial_top_k: int = 30
    ivfflat_probes: int = 5
    alpha: float = 0.5
    tables: tuple[str, ...] = ("matte", "service_public", "dgafp", "rgrh")
    enable_chunk_reranker: bool = False
    chunk_rerank_top_k: int = 30
    enable_selector_retry: bool = True
    selector_retry_search_mode: SearchMode = SearchMode.HYBRID
    selector_retry_top_k: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "search_mode": self.search_mode.value,
            "embedding_model": self.embedding_model.value,
            "tables": list(self.tables),
            "selector_retry_search_mode": self.selector_retry_search_mode.value,
        }


@dataclass(frozen=True, slots=True)
class SectionAggregationConfig:
    weight_max_score: float = 0.5
    weight_mean_score: float = 0.3
    weight_chunk_count: float = 0.2
    enable_section_reranker: bool = True
    section_rerank_top_k: int = 20
    rerank_input_k: int = 20

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContextBuildConfig:
    context_mode: ContextMode = ContextMode.STANDARD
    token_budget: int = 8000
    max_full_docs: int = 1
    doc_entire_threshold: int = 3500
    max_sections: int = 12
    triangulation_sections: int = 2
    legal_refs_budget: int = 1000
    token_budget_wide: int = 12000
    max_full_docs_wide: int = 2
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


@dataclass(frozen=True, slots=True)
class SelectorConfig:
    """Optional LLM-based source filter (toggle)."""

    enabled: bool = False
    provider: LLMProvider = LLMProvider.ALBERT
    model: str = "openweight-large"
    temperature: float = 0.0
    prompt_name: str = "v3_selector_business.md"
    min_kept_sections: int = 0

    def to_dict(self) -> dict:
        return {**asdict(self), "provider": self.provider.value}


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    provider: LLMProvider = LLMProvider.ALBERT
    model: str = "openweight-large"
    temperature: float = 0.0
    system_prompt_name: str = "system_prompt_V6_optimized.md"
    fallback_provider: LLMProvider = LLMProvider.SCALEWAY
    fallback_model: str = "llama-3.1-70b-instruct"

    def to_dict(self) -> dict:
        return {**asdict(self), "provider": self.provider.value, "fallback_provider": self.fallback_provider.value}


@dataclass(frozen=True, slots=True)
class QueryProcessorConfig:
    enable_acronym_expansion: bool = True
    enable_intent_gating: bool = True
    intent_model: str = "openweight-medium"
    intent_prompt_name: str = "intent_unified.md"
    enable_hyde: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
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
