"""Ministry-agnostic prompt rendering + migration transform."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from assistant_rh_rag_pipeline.generator import StreamingGenerator
from assistant_rh_rag_pipeline.ministry_scope import (
    MINISTRY_CATALOG,
    build_retrieval_scope,
    get_ministry,
    ministry_placeholders,
    render_ministry_prompt,
    resolve_ministry,
)

_ALL_IDS = ["matte", "mso", "mi", "masa"]
_TOKENS = ("{ministere_label}", "{ministere_sigle}")


# ── render_ministry_prompt ────────────────────────────────────────────────


@pytest.mark.parametrize("ministry_id", _ALL_IDS)
def test_render_substitutes_all_tokens_for_each_ministry(ministry_id: str) -> None:
    ministry = get_ministry(ministry_id)
    text = "Source: {ministere_sigle}. Ministère: {ministere_label}."

    rendered = render_ministry_prompt(text, ministry)

    for token in _TOKENS:
        assert token not in rendered
    assert ministry.publisher in rendered
    assert ministry.label in rendered
    assert "MATTE" not in rendered or ministry_id == "matte"


def test_render_with_none_uses_generic_fallback() -> None:
    rendered = render_ministry_prompt("Bienvenue au sein de {ministere_label} ({ministere_sigle}).", None)

    assert "votre ministère" in rendered
    for token in _TOKENS:
        assert token not in rendered


def test_render_preserves_unrelated_braces() -> None:
    # {query} (downstream .format) and {{...}} (JSON few-shot) must survive.
    text = 'Q: {query}\nJSON: {{"a": 1}}\nSource: {ministere_sigle}'

    rendered = render_ministry_prompt(text, get_ministry("mso"))

    assert "{query}" in rendered
    assert '{{"a": 1}}' in rendered
    assert "MSO" in rendered


def test_render_empty_text_is_noop() -> None:
    assert render_ministry_prompt("", get_ministry("matte")) == ""


def test_placeholders_match_catalog() -> None:
    ph = ministry_placeholders(get_ministry("masa"))
    assert ph["ministere_sigle"] == "MASA"
    assert ph["ministere_label"] == MINISTRY_CATALOG["masa"].label


# ── resolve_ministry ──────────────────────────────────────────────────────


def test_resolve_ministry_from_scope_id_and_none() -> None:
    assert resolve_ministry(build_retrieval_scope("mi")).id == "mi"
    assert resolve_ministry("masa").id == "masa"
    assert resolve_ministry(None) is None
    assert resolve_ministry("does-not-exist") is None  # fail soft
    assert resolve_ministry(123) is None  # unexpected type → fail soft, never raises


# ── generator system prompt ───────────────────────────────────────────────


def test_generator_system_prompt_is_ministry_specific() -> None:
    gen = StreamingGenerator(SimpleNamespace(system_prompt_name="__missing__.md"))
    # Inject the on-disk template directly so the test needs no DB/DSN.
    repo_root = Path(__file__).resolve().parents[1]
    gen._base_prompt = (repo_root / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts/generator.md").read_text(encoding="utf-8")

    matte_sp = gen._system_prompt_for(get_ministry("matte"))
    masa_sp = gen._system_prompt_for(get_ministry("masa"))
    generic_sp = gen._system_prompt_for(None)

    for sp in (matte_sp, masa_sp, generic_sp):
        assert "{ministere_sigle}" not in sp
    assert "MATTE" in matte_sp
    assert "MASA" in masa_sp
    assert "MATTE" not in masa_sp
    assert "votre ministère" in generic_sp


# ── migration transform ───────────────────────────────────────────────────


def _load_migration_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_ministry_agnostic_prompts.py"
    spec = importlib.util.spec_from_file_location("migrate_ministry_agnostic_prompts", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_migration_transform_and_idempotence() -> None:
    transform = _load_migration_module().ministry_agnostic_transform

    src = (
        "Assistant pour le Ministère de la Transition Écologique - MATTE.\n"
        "Priorité 1 : Fiches MATTE. La pratique au MATTE prime.\n"
        '"requested_source": "MATTE|Service-Public" ou null'
    )
    out = transform(src)

    assert "MATTE" not in out
    assert "Service-Public" not in out
    assert "{ministere_label}" in out
    assert "{ministere_sigle}" in out
    assert '"ministere|service_public"' in out
    # Idempotent: re-running changes nothing.
    assert transform(out) == out


def test_migration_transform_is_case_insensitive_but_table_safe() -> None:
    transform = _load_migration_module().ministry_agnostic_transform

    # Lowercase/mixed-case tenant wording is migrated...
    assert transform("fiches matte prioritaires") == "fiches {ministere_sigle} prioritaires"
    assert transform("La pratique au Matte") == "La pratique au {ministere_sigle}"
    # ...but table identifiers (no word boundary before "matte") are left intact.
    assert transform("SELECT * FROM rag_chunks_matte") == "SELECT * FROM rag_chunks_matte"
    assert transform("MATTELAS") == "MATTELAS"
    # Empty / NULL rows must not crash re.sub.
    assert transform("") == ""
    assert transform(None) is None
