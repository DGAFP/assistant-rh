"""Offline differential behavior checks against the retained historical runtime."""

import asyncio
import json
import unicodedata
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from assistant_rh_api.core.errors import DatabaseFailure, DatabaseUnavailable
from assistant_rh_api.core.inference import Attempt, Completion, InferenceFailure
from assistant_rh_api.core.models.configuration import Acronym, Prompt, Snapshot
from assistant_rh_api.core.models.rag_configuration import QueryProcessorConfig
from assistant_rh_api.core.pipeline.steps.query_processor import QueryProcessor
from assistant_rh_api.gateways.packaged_prompts import PackagedPromptStore
from assistant_rh_rag_pipeline.config import QueryProcessorConfig as LegacyConfig
from assistant_rh_rag_pipeline.ministry_scope import get_ministry
from assistant_rh_rag_pipeline.query_processor import QueryProcessor as LegacyProcessor

ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = '{today} {ministere_label}/{ministere_sigle}\n{history}\n{query}\n{acronyms_section}\n{{"example": true}}'
ACRONYMS = (Acronym("CDD", "contrat à durée déterminée"), Acronym("FPE", "fonction publique d'État"))
TODAY = "2026-09-10"
pytestmark = pytest.mark.anyio


def make_processor(*, raw="{}", acronyms=ACRONYMS, template=TEMPLATE, config=None):
    acronym_store = AsyncMock()
    acronym_store.load.return_value = Snapshot(acronyms, "acr-v1", "database")
    prompt_store = AsyncMock()
    prompt_store.get.return_value = Snapshot(Prompt("intent.md", template), "prompt-v1", "database") if template is not None else None
    packaged = AsyncMock()
    packaged.get.return_value = None
    llm = AsyncMock()
    if isinstance(raw, BaseException):
        llm.complete.side_effect = raw
    else:
        llm.complete.return_value = Completion(raw, "albert", "openweight-medium", (Attempt("albert", "openweight-medium"),))
    return QueryProcessor(config or QueryProcessorConfig(), acronym_store, prompt_store, packaged, llm), acronym_store, prompt_store, packaged, llm


def legacy_result(query, raw, *, acronyms=ACRONYMS, template=TEMPLATE, config=None, history=None, ministry=None):
    config = config or QueryProcessorConfig()
    llm = MagicMock()
    if isinstance(raw, BaseException):
        llm.chat.side_effect = raw
    else:
        llm.chat.return_value = raw
    with (
        patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={a.short: a.expansion for a in acronyms}),
        patch("assistant_rh_rag_pipeline.query_processor.load_prompt", return_value=template.replace("{today}", TODAY) if template else template),
        patch("assistant_rh_rag_pipeline.query_processor.LLMClient", return_value=llm) as constructor,
    ):
        result = LegacyProcessor(LegacyConfig(**config.to_dict())).process(query, history, get_ministry(ministry) if ministry else None)
    if config.enable_intent_gating:
        constructor.assert_called_once_with(provider="albert", model=config.intent_model, temperature=0.0)
    return result, llm


def comparable(result):
    values = asdict(result)
    values["expanded_acronyms"] = list(result.expanded_acronyms)
    if isinstance(result.detected_acronyms, tuple):
        values["detected_acronyms"] = {a.short: a.expansion for a in result.detected_acronyms}
    values["intent"] = result.intent.value
    values["should_proceed"] = result.should_proceed
    values["query_for_retrieval"] = result.query_for_retrieval
    return values


RAW_CASES = [
    "{}",
    *[
        json.dumps({"intent": intent, "needs_legal_search": legal, "theme": "conges"})
        for intent in ("rag_query", "follow_up", "chit_chat", "out_of_scope", "clarification", "document_request", "unknown")
        for legal in (True, False)
    ],
    '{"confidence":"0.91","theme":"unknown","requested_source":"ministere","is_catalog_query":true}',
    '{"confidence":null}',
    '{"intent":[]}',
    '{"needs_legal_search":"false"}',
    '{"reformulated_query":"Une autre question", "query_for_retrieval":"CDD (contrat à durée déterminée)"}',
    '```json\n{"intent":"follow_up"}\n```',
    '```\n{"theme":"formation"}\n```',
    'json {"theme":"psc"}',
    '```JSON\n{"theme":"psc"}\n```',
    "[1]",
    "null",
    "invalid json",
    "",
    TimeoutError("synthetic timeout"),
    InferenceFailure((Attempt("albert", "openweight-medium", "unavailable"),)),
]


@pytest.mark.parametrize("raw", RAW_CASES)
async def test_full_output_and_llm_request_match_legacy(raw):
    query = unicodedata.normalize("NFD", "Selon quel texte le CDD relève de la FPE ?")
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}:" + "é" * 350} for i in range(11)]
    proc, _, _, _, llm = make_processor(raw=raw)
    actual = await proc.process(query, history, "mso", today=TODAY)
    expected, legacy_llm = legacy_result(query, raw, history=history, ministry="mso")
    assert comparable(actual.result) == comparable(expected)
    request = llm.complete.call_args.args[0]
    assert request.temperature == 0.0
    assert len(request.messages) == 1
    assert request.messages[0].role == "user"
    assert request.messages[0].content == legacy_llm.chat.call_args.args[0]
    assert legacy_llm.chat.call_args.kwargs == {"system_prompt": ""}


@pytest.mark.parametrize("history", [None, [], [{"role": "user", "content": "ignored"}], [{"role": "user"}, {}]])
@pytest.mark.parametrize("ministry", [None, "matte", "mso", "mi", "masa"])
async def test_short_or_malformed_history_and_ministry_rendering(history, ministry):
    proc, _, _, _, llm = make_processor()
    actual = await proc.process("Question", history, ministry, today=TODAY)
    expected, legacy_llm = legacy_result("Question", "{}", history=history, ministry=ministry)
    assert comparable(actual.result) == comparable(expected)
    if llm.complete.called:
        assert llm.complete.call_args.args[0].messages[0].content == legacy_llm.chat.call_args.args[0]


@pytest.mark.parametrize("gating", [True, False])
@pytest.mark.parametrize("expansion", [True, False])
async def test_flags_case_boundaries_duplicate_order_and_nested_expansion(gating, expansion):
    acronyms = (Acronym("ZZ", "AA"), Acronym("AA", "old"), Acronym("CDD", "contrat"), Acronym("AA", "final"))
    config = QueryProcessorConfig(enable_intent_gating=gating, enable_acronym_expansion=expansion, enable_hyde=True)
    query = "ZZ AA AA CDD cdd CDDx XCDD"
    proc, store, prompts, packaged, llm = make_processor(acronyms=acronyms, config=config)
    actual = await proc.process(query, today=TODAY)
    expected, _ = legacy_result(query, "{}", acronyms=acronyms if expansion else (), config=config)
    assert comparable(actual.result) == comparable(expected)
    assert store.load.call_count == int(expansion)
    assert llm.complete.call_count == int(gating)
    if not gating:
        prompts.get.assert_not_called()
        packaged.get.assert_not_called()
        if expansion:
            assert actual.result.processed_query == "ZZ (AA (final)) AA (final) AA (final) CDD (contrat) cdd CDDx XCDD"
            assert actual.result.expanded_acronyms == ("ZZ", "AA", "CDD")


@pytest.mark.parametrize("error", [DatabaseFailure(), DatabaseUnavailable()])
async def test_acronym_store_failure_continues_with_empty_detection(error):
    proc, store, _, _, _ = make_processor()
    store.load.side_effect = error
    outcome = await proc.process("CDD", today=TODAY)
    expected, _ = legacy_result("CDD", "{}", acronyms=())
    assert comparable(outcome.result) == comparable(expected)
    assert outcome.diagnostics.acronyms is None
    assert outcome.diagnostics.store_errors == (error.code,)


@pytest.mark.parametrize("database_state", ["absent", "empty", "failed", "present"])
async def test_prompt_fallback_precedence(database_state):
    proc, _, db, packaged, llm = make_processor()
    primary = Snapshot(Prompt("intent_unified.md", "primary {query}"), "primary", "packaged")
    fallback = Snapshot(Prompt("intent.md", "fallback {query}"), "fallback", "database")
    db.get.side_effect = {
        "absent": [None],
        "empty": [Snapshot(Prompt("intent_unified.md", ""), "empty", "database"), fallback],
        "failed": [DatabaseUnavailable()],
        "present": [fallback],
    }[database_state]
    packaged.get.return_value = primary
    outcome = await proc.process("CDD", today=TODAY)
    if database_state in ("absent", "failed"):
        assert outcome.diagnostics.prompt == primary
        packaged.get.assert_awaited_once_with("intent_unified.md")
    else:
        assert outcome.diagnostics.prompt == fallback
        packaged.get.assert_not_called()
    assert llm.complete.call_count == 1


@pytest.mark.parametrize("template", [None, "", "{unknown}"])
async def test_missing_or_invalid_prompt_defaults_without_heuristic(template):
    proc, _, _, _, llm = make_processor(template=template)
    outcome = await proc.process("CDD article L.132-1", today=TODAY)
    expected, _ = legacy_result("CDD article L.132-1", "{}", template=template)
    assert comparable(outcome.result) == comparable(expected)
    assert outcome.result.needs_legal_search is False
    llm.complete.assert_not_called()


@pytest.mark.parametrize("stage", ["acronyms", "prompts", "packaged", "llm"])
async def test_cancellation_propagates(stage):
    proc, acronyms, prompts, packaged, llm = make_processor()
    target = {"acronyms": acronyms.load, "prompts": prompts.get, "packaged": packaged.get, "llm": llm.complete}[stage]
    if stage == "packaged":
        prompts.get.return_value = None
    target.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await proc.process("CDD", today=TODAY)


async def test_snapshots_results_and_provider_evidence_are_request_local():
    proc, acronyms, prompts, _, llm = make_processor()
    snapshots = [Snapshot((Acronym("CDD", f"version {i}"),), f"acr-{i}", "database") for i in range(2)]
    acronyms.load.side_effect = snapshots
    prompts.get.side_effect = [Snapshot(Prompt("intent.md", "{query} {acronyms_section}"), f"prompt-{i}", "database") for i in range(2)]
    ready = asyncio.Event()

    async def complete(request):
        prompt = request.messages[0].content
        if "version 0" in prompt:
            await ready.wait()
        else:
            ready.set()
        return Completion(json.dumps({"query_for_retrieval": prompt}), "albert", "openweight-medium", ())

    llm.complete.side_effect = complete
    one, two = await asyncio.gather(proc.process("CDD", today=TODAY), proc.process("CDD", today=TODAY))
    for i, result in enumerate((one, two)):
        assert result.diagnostics.acronyms == snapshots[i]
        assert result.diagnostics.prompt.revision == f"prompt-{i}"
        assert f"version {i}" in result.result.processed_query
        assert result.diagnostics.completion.text == result.result.intent_raw_response
        with pytest.raises(FrozenInstanceError):
            result.result.processed_query = "mutated"
        with pytest.raises(FrozenInstanceError):
            result.result.detected_acronyms[0].expansion = "mutated"


async def test_packaged_fallback_is_identical_to_legacy_resource():
    store = PackagedPromptStore()
    snapshot = await store.get("intent.md")
    assert snapshot.value.content == (ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts/intent.md").read_text()
    assert snapshot.origin == "packaged"
    assert await store.get("../core/errors.py") is None
    proc, _, db, _, _ = make_processor()
    proc._packaged_prompts = store
    db.get.side_effect = DatabaseUnavailable()
    outcome = await proc.process("CDD", today=TODAY)
    assert outcome.diagnostics.prompt == snapshot
    assert outcome.result.intent_confidence == 0.8


LEGAL_CASES = [json.loads(line) for line in (ROOT / "tests/conformance/queries.legifrance-source-check.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("case", LEGAL_CASES, ids=lambda case: case["id"])
async def test_legal_conformance_cases(case):
    raw = json.dumps({"intent": case["expected_intent"], "needs_legal_search": False})
    proc, _, _, _, _ = make_processor(raw=raw)
    outcome = await proc.process(case["query"], today=TODAY)
    expected, _ = legacy_result(case["query"], raw)
    assert comparable(outcome.result) == comparable(expected)
    assert outcome.result.needs_legal_search is case["expected_needs_legal_search"]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Article L‑132-1", True),
        ("articles R123-4", True),
        ("article 2025 du blog", False),
        ("cet article a 5 ans", False),
        ("la loi du plus fort", False),
        ("loi nouvelle", False),
        ("loi n° 84-16", True),
        ("loi de finances", True),
        ("Quel arrêté fixe la prime ?", True),
        ("Il les arrête à la frontière", False),
        ("Arrête de poser des questions", False),
        ("arrêté ministériel", True),
        ("code général de la fonction publique", True),
        ("Quelles conditions pour un agent contractuel ?", False),
        ("Quelles conditions pour un agent contractuel et son casier judiciaire ?", True),
        ("Quelles conditions pour un congé parental ?", True),
        ("", False),
    ],
)
@pytest.mark.parametrize("normalization", ["NFC", "NFD"])
async def test_legal_positive_and_adversarial_unicode_cases(query, expected, normalization):
    query = unicodedata.normalize(normalization, query)
    proc, _, _, _, _ = make_processor()
    outcome = await proc.process(query, today=TODAY)
    legacy, _ = legacy_result(query, "{}")
    assert comparable(outcome.result) == comparable(legacy)
    assert outcome.result.needs_legal_search is expected


async def test_enriched_query_does_not_feed_legal_heuristic():
    raw = '{"reformulated_query":"article L132-1", "query_for_retrieval":"CDD (contrat à durée déterminée)"}'
    proc, _, _, _, _ = make_processor(raw=raw)
    outcome = await proc.process("CDD", today=TODAY)
    assert outcome.result.query_for_retrieval == "article L132-1"
    assert outcome.result.needs_legal_search is False


async def test_malformed_history_fails_before_prompt_lookup():
    history = [{"role": "user"}, {}]
    proc, _, prompts, packaged, llm = make_processor(template=None)
    outcome = await proc.process("CDD", history, today=TODAY)
    expected, _ = legacy_result("CDD", "{}", history=history, template=None)
    assert comparable(outcome.result) == comparable(expected)
    prompts.get.assert_not_called()
    packaged.get.assert_not_called()
    llm.complete.assert_not_called()
