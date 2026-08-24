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

    # Taille de lot par requête /rerank. 40 couvre la config standard
    # (v3_rerank_input_k=40) en UNE SEULE requête — validé empiriquement
    # contre l'API Albert (revue #335 : un appel à 40 documents passe sans
    # 413). Au-delà, le découpage en lots est APPROXIMATIF : les scores
    # Albert dérivent entre requêtes (jusqu'à ~6e-4 mesuré, revue #335), ce
    # qui peut changer l'appartenance au top-k à la frontière — acceptable
    # uniquement pour des configs exploratoires > 40.
    _BATCH_SIZE = 40

    def rerank(self, query: str, texts: List[str], top_k: int | None = None) -> List[Tuple[int, float]]:
        """Return ``(original_index, score)`` pairs sorted by descending score."""
        if not texts:
            return []
        scored: List[Tuple[int, float]] = []
        for offset in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[offset : offset + self._BATCH_SIZE]
            resp = self._post(
                f"{self.base}/rerank",
                headers={"Authorization": f"Bearer {self.key}"},
                json={
                    "model": self.model,
                    "query": query,
                    "documents": batch,
                    # top_n = tout le lot : la troncature top_k ne peut se
                    # faire qu'APRÈS la fusion (sinon des candidats inter-lots
                    # seraient perdus). NB fusion multi-lots = approximative
                    # (dérive inter-requêtes, cf. _BATCH_SIZE).
                    "top_n": len(batch),
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            results = body.get("data") or body.get("results") or []
            scored.extend(
                (offset + int(r.get("index")), float(r.get("relevance_score") or r.get("score") or 0.0)) for r in results
            )
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        if top_k is not None:
            scored = scored[:top_k]
        return scored


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
        logger.error("Reranking failed, keeping original order: %s", exc)
        return [(i, 1.0 - i * 0.001) for i in range(min(top_k or len(texts), len(texts)))]
