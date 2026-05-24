"""
Reranker for the RAG V3 Clean pipeline.

Only the Albert API reranker is supported (via DINUM's /rerank endpoint).
Falls back gracefully to score-sorted input if the API is unreachable.

Environment variables:
  ALBERT_API_KEY, ALBERT_BASE_URL (optional)
"""
from __future__ import annotations

import logging
import os
from typing import List, Tuple

logger = logging.getLogger(__name__)


class AlbertReranker:
    """Rerank texts via the Albert /rerank API (BGE-m3 backend)."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 10,
    ):
        import requests

        self._post = requests.post
        self.base = (base_url or os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.key = api_key or os.getenv("ALBERT_API_KEY", "")
        self.model = model or os.getenv("ALBERT_RERANK_MODEL", "openweight-rerank")
        self.timeout = timeout

    def rerank(self, query: str, texts: List[str], top_k: int | None = None) -> List[Tuple[int, float]]:
        """Return ``(original_index, score)`` pairs sorted by descending score."""
        if not texts:
            return []
        resp = self._post(
            f"{self.base}/rerank",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "model": self.model,
                "prompt": query,
                "input": texts,
                "top_n": top_k or len(texts),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or resp.json().get("data") or []
        return [
            (int(r.get("index")), float(r.get("relevance_score") or r.get("score") or 0.0))
            for r in results
        ]


def maybe_rerank(
    query: str,
    texts: List[str],
    enabled: bool = True,
    top_k: int | None = None,
) -> List[Tuple[int, float]]:
    """
    Rerank *texts* with Albert if *enabled*, otherwise return identity ranking.

    Returns a list of ``(original_index, score)`` tuples.  On failure the
    original order is preserved so the pipeline never crashes.
    """
    if not enabled or not texts:
        return [(i, 1.0 - i * 0.001) for i in range(len(texts))]

    try:
        return AlbertReranker().rerank(query, texts, top_k=top_k)
    except Exception as exc:
        logger.warning("Reranking failed, keeping original order: %s", exc)
        return [(i, 1.0 - i * 0.001) for i in range(min(top_k or len(texts), len(texts)))]
