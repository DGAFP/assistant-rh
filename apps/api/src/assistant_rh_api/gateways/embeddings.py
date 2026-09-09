"""Embedding vectors and model identity are returned together, never in last_* state."""

import math
from collections.abc import Callable
from threading import Lock
from typing import Any

import httpx

from assistant_rh_api.core.inference import Attempt, Embedding, EmbeddingModel, InferenceFailure
from assistant_rh_api.core.ports.system import ClockPort
from assistant_rh_api.gateways.http import InferenceHTTP, invalid
from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy, validate_chain


class EmbeddingCircuit:
    """Synchronized, adapter-owned cooldown; no request results or wall-clock time.

    An older successful request cannot clear a newer failure's cooldown.
    No lock is held over network I/O. Expiry permits concurrent primary calls,
    matching the historical cooldown (this is not a single-flight half-open gate).
    """

    def __init__(self, clock: ClockPort, *, cooldown: float = 60) -> None:
        if not math.isfinite(cooldown) or cooldown <= 0:
            raise ValueError("invalid circuit cooldown")
        self._clock = clock
        self._cooldown = cooldown
        self._until = 0.0
        self._generation = 0
        self._lock = Lock()

    def acquire(self) -> int | None:
        with self._lock:
            if self._clock.monotonic() < self._until:
                return None
            return self._generation

    def finish(self, generation: int, *, failed: bool) -> None:
        with self._lock:
            if failed:
                self._generation += 1
                self._until = self._clock.monotonic() + self._cooldown
            elif generation == self._generation:
                self._until = 0.0


def _vector(dimensions: int) -> Callable[[Any], tuple[float, ...]]:
    def decode(data: Any) -> tuple[float, ...]:
        rows = data.get("data") if isinstance(data, dict) and "error" not in data else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise invalid()
        values = rows[0].get("embedding")
        if not isinstance(values, list) or len(values) != dimensions or any(type(value) not in (float, int) for value in values):
            raise invalid()
        try:
            vector = tuple(float(value) for value in values)
        except (ValueError, OverflowError):
            raise invalid() from None
        if not all(math.isfinite(value) for value in vector):
            raise invalid()
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise invalid()
        return tuple(value / norm for value in vector)

    return decode


class EmbeddingGateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        primary: Endpoint,
        fallback: Endpoint | None = None,
        *,
        circuit: EmbeddingCircuit | None = None,
        policy: RequestPolicy = RequestPolicy(),
    ) -> None:
        validate_chain(primary, fallback)
        if circuit is not None and primary.provider != "albert":
            raise ValueError("embedding cooldown applies only to Albert")
        self._endpoints = (primary,) if fallback is None else (primary, fallback)
        self._circuit = circuit
        self._http = InferenceHTTP(client, policy)

    async def embed(self, text: str) -> Embedding:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("embedding text is empty")
        attempts: list[Attempt] = []
        for endpoint in self._endpoints:
            circuit = self._circuit if endpoint.provider == "albert" else None
            generation = circuit.acquire() if circuit is not None else 0
            if generation is None:
                attempts.append(Attempt(endpoint.provider, endpoint.model, "circuit_open"))
                continue
            model: EmbeddingModel = "albert" if endpoint.provider == "albert" else "bge_scaleway"
            dimensions = 1024 if model == "albert" else 3584
            try:
                vector = await self._http.json(
                    endpoint, "/embeddings", {"model": endpoint.model, "input": text}, _vector(dimensions), attempts, self._http.deadline()
                )
            except InferenceFailure:
                if circuit is not None:
                    circuit.finish(generation, failed=True)
                continue
            if circuit is not None:
                circuit.finish(generation, failed=False)
            return Embedding(vector, model, endpoint.provider, tuple(attempts))
        raise InferenceFailure(tuple(attempts)) from None
