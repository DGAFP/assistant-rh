"""Tests du plancher de sections du ContextSelector (min_kept_sections).

Constat eval du 05/07/2026 : le sélecteur LLM garde 1-2 sections en moyenne,
ce qui affamait le générateur depuis le chunking par sections fines (réponse
présente dans le corpus mais hors du contexte servi). Le plancher complète la
sélection au rang d'agrégation sans toucher au rejet explicite total, qui
pilote la logique de retry du pipeline.
"""

from __future__ import annotations

import pytest
from assistant_rh_rag_pipeline.config import SelectorConfig
from assistant_rh_rag_pipeline.context_selector import ContextSelector
from assistant_rh_rag_pipeline.models import AggregatedSection


def make_sections(n: int) -> list[AggregatedSection]:
    return [
        AggregatedSection(
            section_id=f"s{i}",
            heading=f"Section {i}",
            markdown=f"Contenu {i}",
            chunks=[],
            score=1.0 - i * 0.1,
        )
        for i in range(n)
    ]


class FakeLLM:
    def __init__(self, response: str):
        self._response = response

    def chat(self, prompt: str, system_prompt: str = "") -> str:
        return self._response


@pytest.fixture
def patch_llm(monkeypatch: pytest.MonkeyPatch):
    def _patch(response: str) -> None:
        monkeypatch.setattr(
            "assistant_rh_rag_pipeline.context_selector.LLMClient",
            lambda **kwargs: FakeLLM(response),
        )
        # Hermétique: load_prompt lit les prompts en base (DSN requis) — sans
        # ce patch, l'exception fait retomber select() sur « keep all » et le
        # test ne mesure plus le plancher.
        monkeypatch.setattr(
            "assistant_rh_rag_pipeline.context_selector.load_prompt",
            lambda *args, **kwargs: "Question: {query}\n\nSections:\n{context}",
        )

    return _patch


def test_floor_tops_up_a_thin_selection(patch_llm) -> None:
    patch_llm('{"selected_ids": [2], "reason": "une seule pertinente"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=4))
    sections = make_sections(6)

    kept = selector.select("question", sections)

    assert len(kept) == 4
    # Le choix du LLM reste en tête, le complément suit le rang d'agrégation.
    assert kept[0] is sections[2]
    assert [s.section_id for s in kept[1:]] == ["s0", "s1", "s3"]
    assert selector.last_decisions["topped_up_to_min"] == {
        "floor": 4,
        "selected_by_llm": 1,
        "served": 4,
    }
    expected_prompt = "Question: question\n\nSections:\n" + "\n\n---\n\n".join(
        f"[{i}] {section.heading} (unknown)\n{section.markdown}" for i, section in enumerate(sections)
    )
    assert selector.last_prompt_chars == len(expected_prompt)


def test_floor_does_not_touch_a_wide_enough_selection(patch_llm) -> None:
    patch_llm('{"selected_ids": [0, 1, 2, 3, 4], "reason": "large"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=4))

    kept = selector.select("question", make_sections(6))

    assert len(kept) == 5
    assert "topped_up_to_min" not in selector.last_decisions


def test_floor_capped_by_available_sections(patch_llm) -> None:
    patch_llm('{"selected_ids": [1], "reason": "peu de matière"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=8))

    kept = selector.select("question", make_sections(3))

    assert len(kept) == 3  # jamais au-delà du disponible


def test_floor_zero_disables_topping_up(patch_llm) -> None:
    patch_llm('{"selected_ids": [2], "reason": "minimal"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=0))

    kept = selector.select("question", make_sections(6))

    assert len(kept) == 1


def test_explicit_total_rejection_stays_empty(patch_llm) -> None:
    # Le rejet total pilote le retry du pipeline: le plancher ne doit pas
    # le transformer en sélection pleine.
    patch_llm('{"selected_ids": [], "reason": "rien de pertinent"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=4))

    kept = selector.select("question", make_sections(6))

    assert kept == []
    assert selector.all_rejected is True


def test_parse_failure_keeps_legacy_top5_fallback(patch_llm) -> None:
    patch_llm("réponse illisible sans JSON")
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=4))
    sections = make_sections(8)

    kept = selector.select("question", sections)

    assert kept == sections[:5]


def test_duplicate_ids_are_not_served_twice(patch_llm) -> None:
    # Un LLM peut répéter un indice: la section ne doit être servie qu'une fois
    # (sinon contexte dupliqué au générateur et trace kept incohérente).
    patch_llm('{"selected_ids": [2, 2, 0], "reason": "doublon"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=0))
    sections = make_sections(6)

    kept = selector.select("question", sections)

    assert [s.section_id for s in kept] == ["s2", "s0"]
    kept_idx = [entry["idx"] for entry in selector.last_decisions["kept"]]
    assert kept_idx == [2, 0]
