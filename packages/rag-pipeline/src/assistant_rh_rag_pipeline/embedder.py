"""
Embedding client with automatic fallback for the RAG V3 Clean pipeline.

Supports two providers:
  - Albert (DINUM): openweight-embeddings, 1024 dims
  - Scaleway BGE: bge-multilingual-gemma2, 3584 dims

Default chain: Albert → Scaleway BGE → None (triggers lexical search).
A lightweight circuit breaker avoids repeated timeouts when Albert is down.

Environment variables:
  ALBERT_API_KEY, ALBERT_BASE_URL (optional)
  SCALEWAY_API_KEY, SCALEWAY_BASE_URL (optional)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_MODELS: Dict[str, Dict] = {
    "albert": {
        "model_id": "openweight-embeddings",
        "provider": "albert",
        "dimensions": 1024,
    },
    "bge_scaleway": {
        "model_id": "bge-multilingual-gemma2",
        "provider": "scaleway",
        "dimensions": 3584,
    },
}

EMBEDDING_COLUMN_MAP: Dict[str, Dict[str, str]] = {
    "rag_chunks_matte": {"albert": "embedding_m3", "bge_scaleway": "embedding_bge_scw"},
    "rag_chunks_mso": {"albert": "embedding_m3", "bge_scaleway": "embedding_bge_scw"},
    "rag_chunks_service_public": {"albert": "embedding_m3", "bge_scaleway": "embedding_bge_scw"},
    "rag_chunks_dgafp": {"albert": "embedding_m3", "bge_scaleway": "embedding_bge_scw"},
    "rag_chunks_rgrh": {"albert": "embedding_m3", "bge_scaleway": "embedding_bge_scw"},
}


def get_embedding_column(table: str, model_key: str) -> str:
    """Return the DB column storing embeddings for a given table and model."""
    return EMBEDDING_COLUMN_MAP.get(table, {}).get(model_key, "embedding")


def _normalize(vec: List[float]) -> List[float]:
    a = np.array(vec)
    norm = np.linalg.norm(a)
    return (a / norm).tolist() if norm > 0 else vec


# ---------------------------------------------------------------------------
# Low-level embedders
# ---------------------------------------------------------------------------

class _AlbertEmbedder:
    def __init__(self, timeout: int = 10):
        import requests
        self._post = requests.post
        self.base = (os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.key = os.getenv("ALBERT_API_KEY", "")
        self.model = os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings")
        self.timeout = timeout

    def embed(self, text: str) -> Optional[List[float]]:
        try:
            r = self._post(
                f"{self.base}/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "input": text},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return _normalize(r.json()["data"][0]["embedding"])
        except Exception as exc:
            logger.warning("Albert embedding failed: %s", exc)
            return None


class _ScalewayEmbedder:
    def __init__(self, model: str = "bge-multilingual-gemma2", timeout: int = 20):
        import requests
        self._post = requests.post
        self.base = (os.getenv("SCALEWAY_BASE_URL") or "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1").rstrip("/")
        self.key = os.getenv("SCALEWAY_API_KEY", "")
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> Optional[List[float]]:
        try:
            r = self._post(
                f"{self.base}/embeddings",
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                json={"model": self.model, "input": text},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return _normalize(r.json()["data"][0]["embedding"])
        except Exception as exc:
            logger.warning("Scaleway embedding (%s) failed: %s", self.model, exc)
            return None


# ---------------------------------------------------------------------------
# Circuit breaker (module-level singleton)
# ---------------------------------------------------------------------------

class _CircuitBreaker:
    """Skip Albert entirely for *cooldown* seconds after a failure."""

    def __init__(self, cooldown: int = 60):
        self._open = False
        self._last_failure = 0.0
        self._cooldown = cooldown

    def record_failure(self):
        self._open = True
        self._last_failure = time.time()
        logger.info("Circuit breaker OPEN – Albert will be skipped for %ds", self._cooldown)

    def record_success(self):
        if self._open:
            logger.info("Circuit breaker CLOSED – Albert recovered")
        self._open = False

    def should_skip(self) -> bool:
        if not self._open:
            return False
        if time.time() - self._last_failure > self._cooldown:
            self._open = False
            return False
        return True


_cb = _CircuitBreaker()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class FallbackEmbedder:
    """
    Embed a query with automatic fallback: Albert → Scaleway BGE → None.

    Attributes:
        last_model_used: key of the model that produced the last embedding.
    """

    def __init__(self, primary: str = "albert", fallback: str = "bge_scaleway", timeout: int = 5):
        self.primary_key = primary
        self.fallback_key = fallback
        self.last_model_used: Optional[str] = None

        self._primary = (
            _AlbertEmbedder(timeout=timeout)
            if primary == "albert"
            else _ScalewayEmbedder(model=EMBEDDING_MODELS[primary]["model_id"], timeout=timeout + 10)
        )
        self._fallback = (
            _ScalewayEmbedder(model=EMBEDDING_MODELS[fallback]["model_id"], timeout=timeout + 10)
            if fallback
            else None
        )

    def embed_query(self, text: str) -> Optional[List[float]]:
        if self.primary_key == "albert" and _cb.should_skip():
            return self._try_fallback(text)

        vec = self._primary.embed(text)
        if vec is not None:
            self.last_model_used = self.primary_key
            if self.primary_key == "albert":
                _cb.record_success()
            return vec

        if self.primary_key == "albert":
            _cb.record_failure()

        return self._try_fallback(text)

    def _try_fallback(self, text: str) -> Optional[List[float]]:
        if self._fallback is None:
            self.last_model_used = None
            return None
        vec = self._fallback.embed(text)
        if vec is not None:
            self.last_model_used = self.fallback_key
            return vec
        self.last_model_used = None
        return None

    @property
    def dimensions(self) -> int:
        key = self.last_model_used or self.primary_key
        return EMBEDDING_MODELS.get(key, {}).get("dimensions", 1024)

    def get_embedding_column(self, table: str) -> str:
        key = self.last_model_used or self.primary_key
        return get_embedding_column(table, key)
