"""Contrat du prompt selector (issue #299 — cascade ministérielle effective).

Campagne suivi-tests-20260708 : la règle « élimine les sections redondantes »
produisait l'inverse de la cascade voulue (le doc ministériel était jeté dès
qu'une fiche Service-Public couvrait le sujet), la sélection était trop
parcimonieuse (1-3 sections sur 20) et une section étiquetée FPT était gardée
sur une question FPE. Ces tests verrouillent les invariants du prompt fallback
``prompts/selector.md`` (même contenu seedé en base sous
``v3_selector_business_v2.md`` via ``scripts/seed_selector_prompt.py`` — la
v1 reste en base pour rollback) et son rendu à travers le chemin réel
(render_ministry_prompt puis format_map).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

import pytest
from assistant_rh_rag_pipeline.db_helpers import _PROMPTS_DIR
from assistant_rh_rag_pipeline.ministry_scope import get_ministry, render_ministry_prompt

PROMPT = (_PROMPTS_DIR / "selector.md").read_text(encoding="utf-8")


def _render(ministry_id: str | None) -> str:
    ministry = get_ministry(ministry_id) if ministry_id else None
    template = render_ministry_prompt(PROMPT, ministry)
    # Même chemin que ContextSelector.select(): format_map sur defaultdict(str).
    return template.format_map(defaultdict(str, {"query": "q", "context": "c", "theme": ""}))


def test_placeholders_pipeline_presents() -> None:
    for token in ("{query}", "{context}", "{ministere_sigle}"):
        assert token in PROMPT, f"placeholder {token} manquant dans selector.md"


def test_cascade_keeps_ministry_alongside_service_public() -> None:
    lower = PROMPT.lower()
    assert "garde les deux" in lower
    assert "n'élimine **jamais** une section {ministere_sigle}".lower() in lower
    # La redondance ne doit plus jouer entre sources différentes.
    assert "même source" in lower


def test_fpe_scope_excludes_fpt_fph_and_special_statuses() -> None:
    assert "FPT" in PROMPT and "FPH" in PROMPT
    assert "police nationale" in PROMPT.lower()


def test_empty_selection_escalation_is_documented() -> None:
    # Le rejet explicite total ('selected_ids': []) pilote le retry du pipeline
    # (all_rejected → selector_retry) : le prompt doit dire au modèle que la
    # liste vide est une réponse valide, sinon l'escalade ne se déclenche jamais.
    assert '"selected_ids": []' in PROMPT


@pytest.mark.parametrize("ministry_id", ["matte", "mso", "mi", "masa", None])
def test_prompt_renders_through_real_path(ministry_id: str | None) -> None:
    rendered = _render(ministry_id)

    assert "{ministere_sigle}" not in rendered
    assert "{query}" not in rendered and "{context}" not in rendered
    if ministry_id:
        assert get_ministry(ministry_id).publisher in rendered
    else:
        assert "votre ministère" in rendered

    # L'exemple JSON survit au format_map ({{ }} → { }) et reste parsable.
    m = re.search(r"```json\s*(\{.*?\})\s*```", rendered, re.DOTALL)
    assert m, "exemple JSON absent du prompt rendu"
    example = json.loads(m.group(1))
    assert example["selected_ids"] == [0, 2, 5]
