"""Albert rerank batches and an explicit, deterministic input-order fallback."""

import math
from collections.abc import Callable
from typing import Any

import httpx

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.inference import Attempt, RankedDocument, Reranking
from assistant_rh_api.gateways.http import InferenceHTTP, invalid
from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy


def _scores(size: int, offset: int) -> Callable[[Any], tuple[RankedDocument, ...]]:
    def decode(data: Any) -> tuple[RankedDocument, ...]:
        if not isinstance(data, dict) or "error" in data:
            raise invalid()
        rows = data.get("data", data.get("results"))
        if not isinstance(rows, list) or len(rows) != size:
            raise invalid()
        seen: set[int] = set()
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise invalid()
            index = row.get("index")
            score = row.get("relevance_score", row.get("score"))
            if type(index) is not int or not 0 <= index < size or index in seen or not isinstance(score, (int, float)) or isinstance(score, bool):
                raise invalid()
            try:
                score = float(score)
            except OverflowError:
                raise invalid() from None
            if not math.isfinite(score):
                raise invalid()
            seen.add(index)
            result.append(RankedDocument(offset + index, score))
        return tuple(result)

    return decode


class RerankerGateway:
    def __init__(
        self, client: httpx.AsyncClient, endpoint: Endpoint, *, policy: RequestPolicy = RequestPolicy(), fallback_on_error: bool = True
    ) -> None:
        if endpoint.provider != "albert":
            raise ValueError("reranker requires an Albert endpoint")
        self._endpoint = endpoint
        self._http = InferenceHTTP(client, policy)
        self._fallback_on_error = fallback_on_error

    async def rerank(self, query: str, documents: tuple[str, ...], *, top_k: int | None = None) -> Reranking:
        if (top_k is not None and (type(top_k) is not int or top_k < 0)) or not isinstance(query, str):
            raise ValueError("invalid rerank request")
        if any(not isinstance(document, str) for document in documents):
            raise ValueError("invalid rerank document")
        if not documents or top_k == 0:
            return Reranking((), ())
        attempts: list[Attempt] = []
        ranked: list[RankedDocument] = []
        deadline = self._http.deadline()
        try:
            for offset in range(0, len(documents), 40):
                batch = documents[offset : offset + 40]
                ranked.extend(
                    await self._http.json(
                        self._endpoint,
                        "/rerank",
                        {"model": self._endpoint.model, "query": query, "documents": list(batch), "top_n": len(batch)},
                        _scores(len(batch), offset),
                        attempts,
                        deadline,
                    )
                )
        except InferenceFailure:
            if not self._fallback_on_error:
                raise
            fallback = tuple(RankedDocument(i, 1.0 - i * 0.001) for i in range(len(documents)))
            return Reranking(fallback[:top_k], tuple(attempts), fallback=True)
        ranked.sort(key=lambda document: (-document.score, document.index))
        return Reranking(tuple(ranked[:top_k]), tuple(attempts))
