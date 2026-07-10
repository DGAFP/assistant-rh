"""Pure helpers shared by the Streamlit pipeline evaluation page and tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_SELECTOR_MODEL_LABELS = {
    "openweight-medium": "med",
    "openweight-large": "lg",
    "openweight-small": "sm",
}

_SELECTOR_PROMPT_LABELS = {
    "v3_selector_business.md": "business-v1",
    "v3_selector_business_v2.md": "business-v2",
    "v3_selector_default.md": "default",
}


def selector_variant_label(model: str, prompt_name: str) -> str:
    """Return a stable display/key fragment that distinguishes prompt A/B runs."""
    model_label = _SELECTOR_MODEL_LABELS.get(model, model)
    filename = (prompt_name or "unknown").rsplit("/", 1)[-1]
    prompt_label = _SELECTOR_PROMPT_LABELS.get(filename)
    if prompt_label is None:
        prompt_label = filename.removesuffix(".md").removeprefix("v3_selector_").replace("_", "-")
    return f"Selector({model_label},{prompt_label})"


def final_items_for_metrics(result: Mapping[str, Sequence[Mapping[str, Any]]]) -> Sequence[Mapping[str, Any]]:
    """Return the actual post-selector result, preserving an explicit empty list."""
    return result["selected_sections"]
