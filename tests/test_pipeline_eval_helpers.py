"""Tests for side-effect-free Pipeline Evaluation helpers."""

from src.pipeline_eval_helpers import final_items_for_metrics, selector_variant_label


def test_selector_variant_label_distinguishes_v1_and_v2() -> None:
    v1 = selector_variant_label("openweight-large", "v3_selector_business.md")
    v2 = selector_variant_label("openweight-large", "v3_selector_business_v2.md")

    assert v1 == "Selector(lg,business-v1)"
    assert v2 == "Selector(lg,business-v2)"
    assert v1 != v2


def test_selector_variant_label_distinguishes_future_prompt_names() -> None:
    first = selector_variant_label("custom-model", "v3_selector_candidate_alpha.md")
    second = selector_variant_label("custom-model", "v3_selector_candidate_beta.md")

    assert first == "Selector(custom-model,candidate-alpha)"
    assert second == "Selector(custom-model,candidate-beta)"


def test_final_items_preserves_explicit_total_rejection() -> None:
    result = {
        "sections": [{"doc_short_id": "gold"}],
        "selected_sections": [],
    }

    assert final_items_for_metrics(result) == []


def test_final_items_returns_post_selector_sections() -> None:
    selected = [{"doc_short_id": "kept"}]

    assert final_items_for_metrics({"sections": [], "selected_sections": selected}) is selected
