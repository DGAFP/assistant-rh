"""
RAG V3 Clean – self-contained RAG pipeline for Assistant RH.

Quick start::

    from assistant_rh_rag_pipeline import create_pipeline

    pipe = create_pipeline()
    result = pipe.run("Qu'est-ce que le RIFSEEP ?")
    print(result.answer)
    print(result.sources)

For streaming (Streamlit)::

    pipe = create_pipeline()
    qr = pipe.process_query("Qu'est-ce que le RIFSEEP ?")
    if qr.should_proceed:
        for token in pipe.run_stream(qr):
            print(token, end="")
"""
from .config import RAGConfig, get_default_config
from .context_selector import ContextSelector
from .db_helpers import get_dsn
from .llm_client import ChatLLM, FallbackLLMClient, LLMClient
from .models import Chunk, ContextItem, PipelineResult
from .pipeline import Pipeline
from .query_processor import QueryProcessResult

__all__ = [
    "create_pipeline",
    "ContextSelector",
    "Pipeline",
    "RAGConfig",
    "PipelineResult",
    "ContextItem",
    "Chunk",
    "QueryProcessResult",
    "get_default_config",
    "ChatLLM",
    "LLMClient",
    "FallbackLLMClient",
    "get_dsn",
]


def create_pipeline(config: RAGConfig | None = None, dsn: str | None = None) -> Pipeline:
    """Create a ready-to-use pipeline with sensible defaults."""
    return Pipeline(config=config or get_default_config(), dsn=dsn)
