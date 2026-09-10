"""Inference boundaries; adapters own I/O and lifecycle state."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from assistant_rh_api.core.models.inference import ChatRequest, Completion, Embedding, Reranking, StreamCompleted, TextDelta


class LLMPort(Protocol):
    async def complete(self, request: ChatRequest) -> Completion: ...

    def stream(self, request: ChatRequest) -> AbstractAsyncContextManager[AsyncIterator[TextDelta | StreamCompleted]]:
        """Own the stream context, including on an early break or cancellation."""
        ...


class EmbeddingPort(Protocol):
    async def embed(self, text: str) -> Embedding:
        """Raise InferenceFailure on double failure; the core chooses lexical fallback."""
        ...


class RerankerPort(Protocol):
    async def rerank(self, query: str, documents: tuple[str, ...], *, top_k: int | None = None) -> Reranking: ...
