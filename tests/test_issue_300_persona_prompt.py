"""Contract tests for the additive « persona gestionnaire RH » generator prompt (#300).

La ligne est une CANDIDATE d'A/B (`--system-prompt-name` côté eval, sélection
Admin Config côté app) : la migration qui la seed ne doit jamais toucher au
prompt actif de `rag_config` — l'adoption éventuelle est une migration séparée,
gatée par l'A/B sous le protocole officiel.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from assistant_rh_rag_pipeline.ministry_scope import get_ministry, render_ministry_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_NAME = "system_prompt_persona_gestionnaire_rh.md"
PROMPT_PATH = REPO_ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts" / PROMPT_NAME
MIGRATION_PATH = REPO_ROOT / "supabase/migrations/20260824150000_issue_300_persona_gestionnaire_prompt.sql"


def _prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def test_prompt_encodes_issue_300_audience_and_scope_contract() -> None:
    prompt = _prompt().casefold()

    required_policy_markers = (
        "gestionnaire rh exerçant en service employeur, notamment en sgcd",
        "jamais à l'agent concerné par la procédure",
        "le gestionnaire vérifie",
        "le service rh informe l'agent",
        "l'autorité compétente signe",
        "injonction à la deuxième personne",
        "les enseignants et la police nationale",
        "que si la question le vise explicitement",
    )
    # Volet « niveau de détail » DIFFÉRÉ (ablation run 165 : l'instruction de
    # restitution exhaustive poussait le générateur à sur-citer -> wrong_law
    # 13->20). Le persona ne porte que le STYLE (voix, acteurs, périmètre) ;
    # la complétude reviendra comme levier séparé, avec grounding des citations.
    deferred_detail_markers = ("types de contrôles", "ne réduisez pas à deux ou trois phrases")
    for marker in deferred_detail_markers:
        assert marker not in prompt

    for marker in required_policy_markers:
        assert marker in prompt


def test_prompt_name_avoids_versioned_v7_namespace() -> None:
    """`system_prompt_V7_ancrage.md` (campagne deepseek 20-21/08) occupe déjà
    l'espace de nommage V7 dans system_prompts — le persona n'y touche pas."""
    assert "v7" not in PROMPT_NAME.casefold()
    assert "v7" not in _prompt().casefold()


@pytest.mark.parametrize("ministry_id", ["matte", "mso", "mi", "masa"])
def test_prompt_remains_templated_for_every_ministry(ministry_id: str) -> None:
    rendered = render_ministry_prompt(_prompt(), get_ministry(ministry_id))

    assert "{ministere_label}" not in rendered
    assert "{ministere_sigle}" not in rendered
    assert get_ministry(ministry_id).label in rendered
    assert "gestionnaire RH" in rendered


def test_migration_seeds_exact_prompt_and_is_strictly_additive() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"\$prompt_persona\$\n(?P<prompt>.*?)\n    \$prompt_persona\$", sql, flags=re.DOTALL)

    assert match is not None
    assert match.group("prompt").strip() == _prompt().strip()
    assert PROMPT_NAME in sql
    assert "ON CONFLICT (name) DO UPDATE" in sql
    assert "is_active = TRUE" in sql
    # Invariant : seed additif, le prompt actif du runtime ne bouge pas.
    assert "UPDATE rag_config" not in sql
    assert "v3_system_prompt_name" not in sql
    assert "DELETE FROM system_prompts" not in sql
