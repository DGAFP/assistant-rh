"""Immutable inference values, independent of provider SDKs and HTTP transports."""

from dataclasses import dataclass
from typing import Literal

from assistant_rh_api.core.errors import ApplicationError

Provider = Literal["albert", "scaleway"]
FailureKind = Literal["timeout", "unavailable", "rate_limited", "rejected", "invalid_response", "circuit_open"]
EmbeddingModel = Literal["albert", "bge_scaleway"]


@dataclass(frozen=True, slots=True)
class Attempt:
    provider: Provider
    model: str
    error: FailureKind | None = None
    status: int | None = None


class InferenceFailure(ApplicationError):
    """Safe diagnostics only; no URL, credentials, prompt or response body."""

    code = "inference_failure"

    def __init__(self, attempts: tuple[Attempt, ...], *, partial: bool = False) -> None:
        super().__init__()
        self.attempts = attempts
        self.partial = partial


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[Message, ...]
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    provider: Provider
    model: str
    attempts: tuple[Attempt, ...]
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class StreamCompleted:
    provider: Provider
    model: str
    attempts: tuple[Attempt, ...]
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Embedding:
    """The logical model and dimensions travel with the normalized vector."""

    vector: tuple[float, ...]
    model: EmbeddingModel
    provider: Provider
    attempts: tuple[Attempt, ...]


@dataclass(frozen=True, slots=True)
class RankedDocument:
    index: int
    score: float


@dataclass(frozen=True, slots=True)
class Reranking:
    documents: tuple[RankedDocument, ...]
    attempts: tuple[Attempt, ...]
    fallback: bool = False
