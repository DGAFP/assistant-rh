"""Immutable public model identity; provider model names are never exposed."""

from dataclasses import dataclass

# Stable catalogue epoch matching the published v1 contract (2025-08-21 UTC).
MODEL_CREATED = 1755734400


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    ministry: str
    created: int = MODEL_CREATED
