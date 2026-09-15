"""Immutable selector evidence, isolated to one request (including bypasses)."""

from dataclasses import dataclass, field
from typing import Literal

from assistant_rh_api.core.models.configuration import ConfigValues, JsonValue, Prompt, Snapshot
from assistant_rh_api.core.models.context import AggregatedSection, freeze_value
from assistant_rh_api.core.models.inference import Attempt, Completion
from assistant_rh_api.core.models.retrieval import freeze_metadata


@dataclass(frozen=True, slots=True)
class SelectionDiagnostics:
    status: Literal["disabled", "empty", "selected", "all_rejected", "parse_failure", "provider_failure", "invalid_response"]
    decisions: ConfigValues = field(default_factory=dict)
    raw_response: str = ""
    reason: JsonValue = ""
    prompt: Snapshot[Prompt] | None = None
    user_prompt: str = ""
    completion: Completion | None = None
    failed_attempts: tuple[Attempt, ...] = ()
    store_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", freeze_metadata(self.decisions))
        object.__setattr__(self, "reason", freeze_value(self.reason))

    @property
    def prompt_chars(self) -> int:
        return len(self.user_prompt)


@dataclass(frozen=True, slots=True)
class SelectionResult:
    sections: tuple[AggregatedSection, ...]
    diagnostics: SelectionDiagnostics

    @property
    def all_rejected(self) -> bool:
        return self.diagnostics.status == "all_rejected"
