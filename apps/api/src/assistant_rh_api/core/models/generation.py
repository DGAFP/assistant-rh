"""Request-local generation result; the same value terminates a core stream."""

from dataclasses import dataclass
from typing import Literal

from assistant_rh_api.core.models.configuration import Prompt, Snapshot
from assistant_rh_api.core.models.inference import ChatRequest, Completion, StreamCompleted


@dataclass(frozen=True, slots=True)
class GenerationDiagnostics:
    status: Literal["completed", "no_answer"]
    prompt: Snapshot[Prompt] | None = None
    request: ChatRequest | None = None
    outcome: Completion | StreamCompleted | None = None
    store_errors: tuple[str, ...] = ()

    @property
    def fallback_count(self) -> int:
        # Several attempts on the same provider are retries, not fallbacks.
        return int(self.outcome is not None and any(a.provider != self.outcome.provider for a in self.outcome.attempts))


@dataclass(frozen=True, slots=True)
class GenerationResult:
    answer: str
    diagnostics: GenerationDiagnostics
