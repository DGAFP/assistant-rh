"""Contract tests for the versioned gestionnaire RH generator prompt (#300)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from assistant_rh_rag_pipeline.admin import RuntimeRAGConfig
from assistant_rh_rag_pipeline.config import DEFAULT_GENERATOR_PROMPT_NAME, GenerationConfig
from assistant_rh_rag_pipeline.ministry_scope import get_ministry, render_ministry_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts" / DEFAULT_GENERATOR_PROMPT_NAME
MIGRATION_PATH = REPO_ROOT / "supabase/migrations/20260723142811_issue_300_gestionnaire_rh_prompt.sql"


def _prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def test_active_defaults_select_versioned_gestionnaire_prompt() -> None:
    assert DEFAULT_GENERATOR_PROMPT_NAME == "system_prompt_V7_gestionnaires_rh.md"
    assert GenerationConfig().system_prompt_name == DEFAULT_GENERATOR_PROMPT_NAME
    assert RuntimeRAGConfig().v3_system_prompt_name == DEFAULT_GENERATOR_PROMPT_NAME


def test_prompt_encodes_issue_300_audience_scope_and_detail_contract() -> None:
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
        "types de contrôles",
        "délais, montants, exceptions",
        "ne réduisez pas à deux ou trois phrases",
    )

    for marker in required_policy_markers:
        assert marker in prompt


@pytest.mark.parametrize("ministry_id", ["matte", "mso", "mi", "masa"])
def test_prompt_remains_templated_for_every_ministry(ministry_id: str) -> None:
    rendered = render_ministry_prompt(_prompt(), get_ministry(ministry_id))

    assert "{ministere_label}" not in rendered
    assert "{ministere_sigle}" not in rendered
    assert get_ministry(ministry_id).label in rendered
    assert "gestionnaire RH" in rendered


def test_migration_seeds_exact_versioned_prompt_and_preserves_v6_rollback() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"\$prompt_v7\$\n(?P<prompt>.*?)\n    \$prompt_v7\$", sql, flags=re.DOTALL)

    assert match is not None
    assert match.group("prompt").strip() == _prompt().strip()
    assert "ON CONFLICT (name) DO UPDATE" in sql
    assert "is_active = TRUE" in sql
    assert "UPDATE rag_config" in sql
    assert "system_prompt_V7_gestionnaires_rh.md" in sql
    assert "COALESCE(config ->> 'v3_system_prompt_name', 'system_prompt_V6_optimized.md')" in sql
    assert "DELETE FROM system_prompts" not in sql
