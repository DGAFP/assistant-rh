"""Stateless C5 selection through injected prompt and LLM ports.

Bind llm to config.provider/config.model without provider fallback, matching the
historical selector. Expected inference outages keep all candidates; programmer
errors, cancellation and rejected/partial calls propagate instead of hiding bugs.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.configuration import ConfigValues
from assistant_rh_api.core.models.context import AggregatedSection
from assistant_rh_api.core.models.inference import ChatRequest, Message
from assistant_rh_api.core.models.rag_configuration import SelectorConfig
from assistant_rh_api.core.models.selection import SelectionDiagnostics, SelectionResult
from assistant_rh_api.core.pipeline.steps.context_formatting import _DOC_ID_METADATA_KEYS, _first_metadata_value
from assistant_rh_api.core.ports.configuration import PromptStorePort
from assistant_rh_api.core.ports.inference import LLMPort
from assistant_rh_api.core.prompt_policy import DEFAULT_SELECTOR_PROMPT, load_prompt, selector_prompt


@dataclass(frozen=True, slots=True)
class _ParseResult:
    ids: tuple[int, ...] = ()
    is_explicit_empty: bool = False


def _extract_json(raw: str) -> Any:
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    return json.loads((match.group(1) if match else text).strip())


def _parse_response(raw: str, n_items: int) -> _ParseResult:
    try:
        data = _extract_json(raw)
        raw_ids = data.get("selected_ids") or data.get("selected_indices") or data.get("selected_ordered")
        if raw_ids is None or (isinstance(raw_ids, list) and len(raw_ids) == 0):
            return _ParseResult(is_explicit_empty=True)
        out = []
        for value in raw_ids:
            if isinstance(value, int):
                out.append(value)
            elif isinstance(value, str):
                digits = re.sub(r"[^0-9]", "", value)
                if digits:
                    out.append(int(digits))
        return _ParseResult(tuple(i for i in out if 0 <= i < n_items))
    except (ValueError, KeyError):
        return _ParseResult()


def _parse_reason(raw: str) -> Any:
    try:
        return _extract_json(raw).get("reason", "")
    except (ValueError, KeyError):
        return ""


def _decision_entry(idx: int, section: AggregatedSection) -> ConfigValues:
    chunk_meta = next((chunk.metadata for chunk in section.chunks if chunk.metadata), {})
    document_id = section.document_id or _first_metadata_value(section.metadata, chunk_meta, keys=_DOC_ID_METADATA_KEYS)
    return {
        "idx": idx,
        "heading": (section.heading or "")[:80],
        "publisher": section.publisher or "",
        "section_id": str(section.section_id) if section.section_id else "",
        "document_id": str(document_id or ""),
    }


def _top_up_ids(selected: list[int], total: int, floor: int) -> list[int]:
    served = list(selected)
    seen = set(served)
    for i in range(total):
        if not selected or len(served) >= floor:
            break
        if i not in seen:
            served.append(i)
            seen.add(i)
    return served


class ContextSelector:
    def __init__(self, config: SelectorConfig, prompts: PromptStorePort, packaged_prompts: PromptStorePort, llm: LLMPort) -> None:
        self._config = config
        self._prompts = prompts
        self._packaged_prompts = packaged_prompts
        self._llm = llm

    async def select(self, query: str, sections: Sequence[AggregatedSection], ministry: str | None = None, *, today: str) -> SelectionResult:
        candidates = tuple(sections)
        if not self._config.enabled or not candidates:
            return SelectionResult(candidates, SelectionDiagnostics("disabled" if not self._config.enabled else "empty"))
        loaded = await load_prompt(self._prompts, self._packaged_prompts, self._config.prompt_name, "selector.md", DEFAULT_SELECTOR_PROMPT)
        prompt = selector_prompt(loaded.snapshot.value.content, query, candidates, ministry, today=today)
        diagnostics = SelectionDiagnostics("selected", prompt=loaded.snapshot, user_prompt=prompt, store_errors=loaded.store_errors)
        try:
            completion = await self._llm.complete(ChatRequest((Message("user", prompt),), self._config.temperature))
        except InferenceFailure as exc:
            if exc.partial or any(attempt.error == "rejected" for attempt in exc.attempts):
                raise
            return SelectionResult(candidates, replace(diagnostics, status="provider_failure", failed_attempts=exc.attempts))
        raw = completion.text
        diagnostics = replace(diagnostics, completion=completion, raw_response=raw)
        try:
            parsed = _parse_response(raw, len(candidates))
            reason = _parse_reason(raw)
            diagnostics = replace(diagnostics, reason=reason)
            # The legacy rejection path slices the reason for logging; retain
            # its keep-all fallback for unusable reason shapes, without logging it.
            if parsed.is_explicit_empty:
                reason[:120]
        except (AttributeError, TypeError, RecursionError):
            return SelectionResult(candidates, replace(diagnostics, status="invalid_response"))
        if parsed.is_explicit_empty:
            return SelectionResult(
                (),
                replace(
                    diagnostics,
                    status="all_rejected",
                    decisions={
                        "kept": (),
                        "removed": tuple(_decision_entry(i, sec) for i, sec in enumerate(candidates)),
                        "reason": reason,
                        "all_rejected": True,
                    },
                ),
            )
        if not parsed.ids:
            return SelectionResult(candidates[:5], replace(diagnostics, status="parse_failure"))
        selected = list(dict.fromkeys(parsed.ids))
        served = _top_up_ids(selected, len(candidates), self._config.min_kept_sections or 0)
        served_set = set(served)
        decisions: dict[str, Any] = {
            "kept": tuple(_decision_entry(i, candidates[i]) for i in served),
            "removed": tuple(_decision_entry(i, sec) for i, sec in enumerate(candidates) if i not in served_set),
            "reason": reason,
        }
        if len(served) > len(selected):
            decisions["topped_up_to_min"] = {"floor": self._config.min_kept_sections, "selected_by_llm": len(selected), "served": len(served)}
        return SelectionResult(tuple(candidates[i] for i in served), replace(diagnostics, decisions=decisions))
