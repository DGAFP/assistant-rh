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
        self.last_prompt = ""

    def chat(self, prompt: str, system_prompt: str = "") -> str:
        self.last_prompt = prompt
        return self._response


@pytest.fixture
def patch_llm(monkeypatch: pytest.MonkeyPatch):
    def _patch(response: str) -> FakeLLM:
        llm = FakeLLM(response)
        monkeypatch.setattr(
            "assistant_rh_rag_pipeline.context_selector.LLMClient",
            lambda **kwargs: llm,
        )
        # Hermétique: load_prompt lit les prompts en base (DSN requis) — sans
        # ce patch, l'exception fait retomber select() sur « keep all » et le
        # test ne mesure plus le plancher.
        monkeypatch.setattr(
            "assistant_rh_rag_pipeline.context_selector.load_prompt",
            lambda *args, **kwargs: "Question: {query}\n\nSections:\n{context}",
        )
        return llm

    return _patch


def test_floor_tops_up_a_thin_selection(patch_llm) -> None:
    llm = patch_llm('{"selected_ids": [2], "reason": "une seule pertinente"}')
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
    assert selector.last_prompt_chars == len(llm.last_prompt)


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


def test_issue_360_prompt_distinguishes_complementary_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """The selector must compare contributions, not publisher priority."""
    sections = [
        AggregatedSection(
            section_id="matte-2011-7",
            heading="7 – Modalités de prise en compte de la journée de solidarité",
            markdown="L'arrêté ministériel du 23 février 2010 est mis en œuvre par l'instruction du 6 janvier 2011.",
            chunks=[],
            score=0.287268,
            publisher="MATTE",
        ),
        AggregatedSection(
            section_id=None,
            heading="Article L621-10 du code général de la fonction publique",
            markdown=(
                "La journée de solidarité peut être accomplie par le travail d'un jour férié, "
                "d'un jour de réduction du temps de travail ou selon une autre modalité de sept heures."
            ),
            chunks=[],
            score=0.317215,
            publisher="DGAFP",
            metadata={"number": "L621-10"},
        ),
        AggregatedSection(
            section_id=None,
            heading="Article L621-11 du code général de la fonction publique",
            markdown=(
                "La journée de solidarité est fixée par arrêté du ministre compétent après avis "
                "du comité social d'administration ministériel."
            ),
            chunks=[],
            score=0.351979,
            publisher="DGAFP",
            metadata={"number": "L621-11"},
        ),
        AggregatedSection(
            section_id=None,
            heading="Article L132-5 du code général de la fonction publique",
            markdown=(
                "Le ministre de l'aménagement du territoire et de la transition écologique "
                "arrête la composition d'une instance consultative."
            ),
            chunks=[],
            score=0.004039,
            publisher="DGAFP",
            metadata={"number": "L132-5"},
        ),
    ]
    llm = FakeLLM(
        '{"selected_ids": [0, 1, 2], "reason": "Modalités légales, autorité compétente et mise en œuvre ministérielle distinctes"}'
    )
    monkeypatch.setattr("assistant_rh_rag_pipeline.context_selector.LLMClient", lambda **kwargs: llm)
    monkeypatch.setattr(
        "assistant_rh_rag_pipeline.context_selector.load_prompt",
        lambda *args, **kwargs: "Ancien prompt configuré en base\nQuestion: {query}\n\n{context}",
    )
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=0))

    selected = selector.select(
        "Quelles sont les règles relatives à la journée de solidarité au MATTE (Ministère Aménagement du territoire Transition écologique) ?",
        sections,
    )

    assert [section.metadata.get("number") for section in selected] == [None, "L621-10", "L621-11"]
    assert "Redondance et complémentarité" in llm.last_prompt
    assert "même règle, la même condition ou la même modalité" in llm.last_prompt
    assert "Applique le même test de pertinence à tous les éditeurs" in llm.last_prompt


def test_issue_360_compatibility_rule_applies_to_single_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale DB prompt must preserve complementary sections from one publisher."""
    sections = [
        AggregatedSection(
            section_id=None,
            heading="Article L621-10 du code général de la fonction publique",
            markdown="La journée de solidarité peut être accomplie selon trois modalités distinctes.",
            chunks=[],
            score=0.317215,
            publisher="DGAFP",
            metadata={"number": "L621-10"},
        ),
        AggregatedSection(
            section_id=None,
            heading="Article L621-11 du code général de la fonction publique",
            markdown="La journée est fixée par arrêté après avis du comité social d'administration.",
            chunks=[],
            score=0.351979,
            publisher="DGAFP",
            metadata={"number": "L621-11"},
        ),
    ]
    llm = FakeLLM('{"selected_ids": [0, 1], "reason": "Modalités et autorité compétente complémentaires"}')
    monkeypatch.setattr("assistant_rh_rag_pipeline.context_selector.LLMClient", lambda **kwargs: llm)
    monkeypatch.setattr(
        "assistant_rh_rag_pipeline.context_selector.load_prompt",
        lambda *args, **kwargs: "Ancien prompt configuré en base\nQuestion: {query}\n\n{context}",
    )
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=0))

    selected = selector.select("Quelles sont les règles relatives à la journée de solidarité ?", sections)

    assert [section.metadata["number"] for section in selected] == ["L621-10", "L621-11"]
    assert "Redondance et complémentarité" in llm.last_prompt
    assert "même règle, la même condition ou la même modalité" in llm.last_prompt


def test_decision_entries_carry_document_id(patch_llm) -> None:
    """Les entrées kept/removed doivent être joignables aux gold doc_ids.

    Sans document_id, l'analyse de funnel des runs d'eval ne peut pas attribuer
    une perte de gold au selector (constat run #180, 17/08/2026 : sections
    juridiques standalone avec document_id vide dans la trace)."""
    sections = [
        AggregatedSection(
            section_id="s0",
            heading="Fiche congés",
            markdown="Contenu",
            chunks=[],
            score=0.9,
            document_id="doc-uuid-0",
            publisher="Service-Public",
        ),
        AggregatedSection(
            section_id=None,
            heading="Article L621-10",
            markdown="Contenu légal",
            chunks=[],
            score=0.8,
            publisher="DGAFP",
            metadata={"cid": "LEGIARTI000044423797"},
        ),
    ]
    patch_llm('{"selected_ids": [0], "reason": "ok"}')
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=0))

    selector.select("question", sections)

    kept = selector.last_decisions["kept"]
    removed = selector.last_decisions["removed"]
    assert kept[0]["document_id"] == "doc-uuid-0"
    assert kept[0]["section_id"] == "s0"
    assert removed[0]["document_id"] == "LEGIARTI000044423797"
    assert removed[0]["section_id"] == ""
