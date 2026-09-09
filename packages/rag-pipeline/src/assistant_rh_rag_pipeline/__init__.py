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

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .config import RAGConfig, get_default_config

if TYPE_CHECKING:
    from .pipeline import Pipeline

# Keep public imports compatible without loading adapters for a config import.
_LAZY_EXPORTS = {
    "ContextSelector": "context_selector",
    "Pipeline": "pipeline",
    "PipelineResult": "models",
    "ContextItem": "models",
    "Chunk": "models",
    "QueryProcessResult": "query_processor",
    "RetrievalScope": "ministry_scope",
    "MINISTRY_CATALOG": "ministry_scope",
    "build_retrieval_scope": "ministry_scope",
    "ChatLLM": "llm_client",
    "LLMClient": "llm_client",
    "FallbackLLMClient": "llm_client",
    "get_dsn": "db_helpers",
}


def __getattr__(name: str):
    if name in _LAZY_EXPORTS:
        return getattr(import_module(f".{_LAZY_EXPORTS[name]}", __name__), name)
    raise AttributeError(name)


__all__ = [
    "create_pipeline",
    "ContextSelector",
    "Pipeline",
    "RAGConfig",
    "PipelineResult",
    "ContextItem",
    "Chunk",
    "QueryProcessResult",
    "RetrievalScope",
    "MINISTRY_CATALOG",
    "build_retrieval_scope",
    "get_default_config",
    "ChatLLM",
    "LLMClient",
    "FallbackLLMClient",
    "get_dsn",
]


def create_pipeline(config: RAGConfig | None = None, dsn: str | None = None, *, chunk_tables=None) -> Pipeline:
    """Create a ready-to-use pipeline with sensible defaults."""
    from .pipeline import Pipeline

    return Pipeline(config=config or get_default_config(), dsn=dsn, chunk_tables=chunk_tables)
