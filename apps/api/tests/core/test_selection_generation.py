"""Differential C5 conformance on explicit synthetic inputs; no live/M0b claims."""

import asyncio
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable, InferenceFailure, RAGConfigurationError
from assistant_rh_api.core.models.configuration import Prompt, Snapshot
from assistant_rh_api.core.models.context import AggregatedSection, ContextItem
from assistant_rh_api.core.models.inference import Attempt, Completion, Message, StreamCompleted, TextDelta, TokenUsage
from assistant_rh_api.core.models.rag_configuration import GenerationConfig, SelectorConfig
from assistant_rh_api.core.models.retrieval import RetrievedChunk
from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
from assistant_rh_api.core.pipeline.steps.generator import Generator
from assistant_rh_api.core.prompt_policy import NO_ANSWER, load_prompt, render_ministry_prompt
from assistant_rh_api.gateways.chat import ChatGateway
from assistant_rh_api.gateways.packaged_prompts import PackagedPromptStore
from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy
from assistant_rh_rag_pipeline import config as legacy_config
from assistant_rh_rag_pipeline import context_selector as legacy_selector
from assistant_rh_rag_pipeline import generator as legacy_generator
from assistant_rh_rag_pipeline import models as legacy_models
from assistant_rh_rag_pipeline.ministry_scope import resolve_ministry

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = "Ministère {ministere_label} / {ministere_sigle}. Question {query}\n{context}\n{theme}"
SYSTEM = 'Tu es expert pour {ministere_label}. Priorité {ministere_sigle}, puis DGAFP. JSON {"untouched": true}'


def plain(value):
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class Prompts:
    def __init__(self, values=None, error=None):
        self.values = values or {}
        self.error = error
        self.calls = []

    async def get(self, name):
        self.calls.append(name)
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        if name not in self.values:
            return None
        return Snapshot(Prompt(name, self.values[name]), f"revision:{self.values[name]}", "database")


class LLM:
    def __init__(self, text='{"selected_ids": [2], "reason": "pertinent"}', error=None):
        self.text = text
        self.error = error
        self.calls = []
        self.closed = False
        self.started = asyncio.Event()
        self.usage = TokenUsage(17, 5, 22)

    async def complete(self, request):
        self.calls.append(request)
        self.started.set()
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return Completion(self.text, "albert", "model", (Attempt("albert", "model"),), "stop", self.usage)

    @asynccontextmanager
    async def stream(self, request):
        self.calls.append(request)

        async def events():
            yield TextDelta("premier")
            if self.error:
                raise self.error
            yield TextDelta(" second")
            yield StreamCompleted("albert", "model", (Attempt("albert", "model"),), "stop", self.usage)

        try:
            yield events()
        finally:
            self.closed = True


def sections(n=8):
    return tuple(
        AggregatedSection(
            f"s{i}",
            f"Titre {i}",
            f"Texte intégral {i} " * 20,
            (RetrievedChunk(f"c{i}", "extrait", 0.5, "DGAFP", {"cid": f"legal-{i}"}, None, "albert"),),
            0.8,
            document_id=f"doc-{i}" if i % 2 else None,
            publisher=("MATTE", "DGAFP", "SERVICE-PUBLIC")[i % 3],
            metadata={"doc_short_id": f"short-{i}"} if i % 3 else {},
        )
        for i in range(n)
    )


def legacy_sections(values):
    return [
        legacy_models.AggregatedSection(
            section_id=s.section_id,
            heading=s.heading,
            markdown=s.markdown,
            score=s.score,
            document_id=s.document_id,
            publisher=s.publisher,
            metadata=plain(s.metadata),
            chunks=[],
        )
        for s in values
    ]


def old_sections(values):
    result = legacy_sections(values)
    for original, converted in zip(values, result, strict=True):
        converted.chunks = [
            legacy_models.RetrievedChunk(
                chunk_id=c.chunk_id,
                text=c.text,
                score=c.score,
                table_source=c.table_source,
                metadata=plain(c.metadata),
                section_id=c.section_id,
            )
            for c in original.chunks
        ]
    return result


def items():
    return (
        ContextItem("s0", "Cadre légal", "Article 1 : règle générale.", 0.9, publisher="DGAFP", document_title="Décret"),
        ContextItem("s1", "Modalités", "Une condition propre au ministère.", 0.9, publisher="MSO"),
    )


@pytest.mark.parametrize("floor", [0, 4, 20])
@pytest.mark.parametrize(
    "raw",
    [
        '{"selected_ids": [2, 0, 2, 7], "reason": "complémentaires"}',
        '{"selected_indices": ["source 3", "-1", "[2]", 99, false], "reason": "raison"}',
        '```json\n{"selected_ordered": [3, 1], "reason": "ordre"}\n```',
        '{"selected_ids": [], "reason": "aucune"}',
        "{}",
        '{"selected_ids": [], "selected_indices": [1], "reason": "alias"}',
        '{"selected_ids": [999], "reason": "hors plage"}',
        "illisible",
        "[]",
        "null",
        '{"selected_ids": 3}',
        '{"selected_ids": [2], "reason": 42}',
        '{"selected_ids": [], "reason": null}',
        '{"selected_ids": ["' + "9" * 5000 + '"]}',
    ],
)
async def test_selector_matches_retained_runtime(monkeypatch, floor, raw):
    llm = LLM(raw)
    fake_legacy = Mock()
    fake_legacy.chat.return_value = raw
    monkeypatch.setattr(legacy_selector, "LLMClient", lambda **kwargs: fake_legacy)
    monkeypatch.setattr(legacy_selector, "load_prompt", lambda *args, **kwargs: TEMPLATE)
    old = legacy_selector.ContextSelector(legacy_config.SelectorConfig(enabled=True, min_kept_sections=floor))
    candidates = sections()
    expected = old.select("Question", old_sections(candidates), ministry=resolve_ministry("mso"))
    selector = ContextSelector(SelectorConfig(enabled=True, min_kept_sections=floor), Prompts({"v3_selector_business.md": TEMPLATE}), Prompts(), llm)
    result = await selector.select("Question", candidates, "mso", today="2026-09-14")
    assert [s.section_id for s in result.sections] == [s.section_id for s in expected]
    assert plain(result.diagnostics.decisions) == old.last_decisions
    assert plain(result.diagnostics.reason) == old.last_reasoning
    assert result.all_rejected == old.all_rejected
    assert result.diagnostics.user_prompt == fake_legacy.chat.call_args.args[0]
    assert result.diagnostics.prompt_chars == old.last_prompt_chars
    assert result.diagnostics.raw_response == old.last_raw_response
    assert result.diagnostics.completion.usage == llm.usage
    assert llm.calls[0].temperature == 0


@pytest.mark.parametrize("enabled,count", [(False, 8), (False, 0), (True, 0)])
async def test_selector_bypasses_do_no_io(enabled, count):
    prompts, llm = Prompts(error=AssertionError()), LLM(error=AssertionError())
    result = await ContextSelector(SelectorConfig(enabled=enabled), prompts, prompts, llm).select("query", sections(count), today="2026-09-14")
    assert result.sections == sections(count)
    assert not result.all_rejected
    assert not prompts.calls and not llm.calls


@pytest.mark.parametrize("error", ["timeout", "unavailable", "invalid_response", "rate_limited", "circuit_open"])
async def test_selector_provider_failure_keeps_all_with_diagnostics(error):
    failure = InferenceFailure((Attempt("albert", "model", error),))
    result = await ContextSelector(SelectorConfig(enabled=True), Prompts(), Prompts(), LLM(error=failure)).select("q", sections(), today="2026-09-14")
    assert result.sections == sections()
    assert result.diagnostics.status == "provider_failure"
    assert result.diagnostics.failed_attempts == failure.attempts
    assert result.diagnostics.decisions == {}


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("bug"),
        RAGConfigurationError(),
        asyncio.CancelledError(),
        InferenceFailure((Attempt("albert", "model", "rejected"),)),
        InferenceFailure((), partial=True),
    ],
)
async def test_selector_does_not_hide_configuration_bugs_or_cancellation(failure):
    selector = ContextSelector(SelectorConfig(enabled=True), Prompts(), Prompts(), LLM(error=failure))
    with pytest.raises(type(failure)):
        await selector.select("q", sections(), today="2026-09-14")


@pytest.mark.parametrize("ministry", [None, "matte", "mso", "mi", "masa"])
@pytest.mark.parametrize("template", [SYSTEM, "", None])
async def test_generator_exact_prompts_and_answer_against_legacy(monkeypatch, ministry, template):
    llm = LLM("Réponse fondée sur les sources.")
    fake_legacy = Mock(last_provider_used="albert", fallback_count=0)
    fake_legacy.chat.return_value = llm.text
    old = legacy_generator.StreamingGenerator(legacy_config.GenerationConfig())
    old._llm = fake_legacy
    monkeypatch.setattr(legacy_generator, "load_prompt", lambda *args, **kwargs: template)
    context = items()
    expected = old.generate("Question", [legacy_models.ContextItem(**{**plain(i), "metadata": {}}) for i in context], resolve_ministry(ministry))
    prompts = Prompts({"system_prompt_V6_optimized.md": template}) if template is not None else Prompts()
    new = Generator(GenerationConfig(), prompts, Prompts(), llm)
    result = await new.generate("Question", context, ministry, today="2026-09-14")
    assert result.answer == expected
    assert result.diagnostics.request.messages == (Message("system", old.last_system_prompt), Message("user", old.last_full_prompt))
    assert result.diagnostics.outcome.usage == llm.usage
    assert result.diagnostics.fallback_count == 0
    assert "Article 1 : règle générale." in old.last_full_prompt
    assert "Une condition propre au ministère." in old.last_full_prompt
    assert "uniquement sur les sources" in old.last_full_prompt
    assert "dites-le explicitement et n'inventez pas" in old.last_full_prompt


@pytest.mark.parametrize("ministry", [None, "matte", "mso", "mi", "masa", "unknown"])
async def test_packaged_prompts_are_identical_and_ministry_rendering_matches(ministry):
    for name in ("selector.md", "generator.md"):
        actual = await PackagedPromptStore().get(name)
        expected = (ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts" / name).read_text()
        assert actual.value.content == expected
        assert render_ministry_prompt(expected, ministry) == legacy_generator.render_ministry_prompt(expected, resolve_ministry(ministry))
    assert await PackagedPromptStore().get("../generator.md") is None


@pytest.mark.parametrize("all_rejected,count,expected_calls", [(True, 0, 0), (False, 0, 1), (True, 2, 1), (False, 2, 1)])
async def test_no_answer_only_on_final_rejection_and_empty_context(all_rejected, count, expected_calls):
    prompts, llm = Prompts(), LLM("Sources insuffisantes.")
    result = await Generator(GenerationConfig(), prompts, Prompts(), llm).generate(
        "q", items()[:count], all_rejected=all_rejected, today="2026-09-14"
    )
    assert len(llm.calls) == expected_calls
    if not expected_calls:
        assert result.answer == NO_ANSWER
        assert result.diagnostics.status == "no_answer"
        assert result.diagnostics.outcome is None and not prompts.calls
    else:
        assert result.answer == llm.text


@pytest.mark.parametrize("error_type", [DatabaseUnavailable, DatabaseConflict, DatabaseFailure])
async def test_prompt_store_recoverable_errors_are_distinct_from_absence(error_type):
    db, packaged = Prompts(error=error_type()), Prompts({"fallback": "resource"})
    result = await load_prompt(db, packaged, "configured", "fallback", "default")
    assert result.snapshot.value.content == "resource"
    assert result.store_errors == (error_type.code, error_type.code)
    assert db.calls == packaged.calls == ["configured", "fallback"]


@pytest.mark.parametrize(
    "db_values,resources,expected,db_calls,resource_calls",
    [
        ({"a": "db"}, {"a": "resource"}, "db", ["a"], []),
        ({}, {"a": "resource"}, "resource", ["a"], ["a"]),
        ({"a": "", "b": "fallback db"}, {"a": "resource"}, "fallback db", ["a", "b"], []),
        ({}, {"b": "fallback resource"}, "fallback resource", ["a", "b"], ["a", "b"]),
        ({}, {}, "default", ["a", "b"], ["a", "b"]),
    ],
)
async def test_prompt_lookup_priority(db_values, resources, expected, db_calls, resource_calls):
    db, packaged = Prompts(db_values), Prompts(resources)
    result = await load_prompt(db, packaged, "a", "b", "default")
    assert result.snapshot.value.content == expected
    assert db.calls == db_calls and packaged.calls == resource_calls


async def test_generator_refreshes_prompt_snapshot_per_request():
    db = Prompts({"system_prompt_V6_optimized.md": "v1 {ministere_sigle}"})
    gen = Generator(GenerationConfig(), db, Prompts(), LLM())
    first = await gen.generate("q1", items(), "matte", today="2026-09-14")
    db.values["system_prompt_V6_optimized.md"] = "v2 {ministere_sigle}"
    second = await gen.generate("q2", items(), "mso", today="2026-09-14")
    assert first.diagnostics.prompt.revision != second.diagnostics.prompt.revision
    assert first.diagnostics.request.messages[0].content.startswith("v1 MATTE")
    assert second.diagnostics.request.messages[0].content.startswith("v2 MSO")


async def test_shared_steps_keep_concurrent_requests_and_diagnostics_isolated():
    class Echo(LLM):
        async def complete(self, request):
            await asyncio.sleep(0)
            text = '{"selected_ids": [1]}' if "question-b" in request.messages[-1].content else '{"selected_ids": [0]}'
            return Completion(text, "albert", "model", (Attempt("albert", "model"),))

    selector = ContextSelector(SelectorConfig(enabled=True), Prompts(), Prompts(), Echo())
    generator = Generator(GenerationConfig(), Prompts(), Prompts(), Echo())
    a, b = await asyncio.gather(
        selector.select("question-a", sections(), "matte", today="2026-09-14"), selector.select("question-b", sections(), "mso", today="2026-09-14")
    )
    assert a.sections == (sections()[0],) and b.sections == (sections()[1],)
    x, y = await asyncio.gather(
        generator.generate("question-a", items(), "matte", today="2026-09-14"), generator.generate("question-b", items(), "mso", today="2026-09-14")
    )
    assert x.answer != y.answer
    assert "MATTE" in x.diagnostics.request.messages[0].content
    assert "MSO" in y.diagnostics.request.messages[0].content
    assert not any(name.startswith("last_") or name.startswith("_last_") for step in (selector, generator) for name in vars(step))
    with pytest.raises(TypeError):
        a.diagnostics.decisions["kept"][0]["idx"] = 7
    with pytest.raises(FrozenInstanceError):
        x.answer = "changed"


async def test_stream_retains_full_history_and_emits_terminal_result():
    llm = LLM()
    generator = Generator(GenerationConfig(), Prompts(), Prompts(), llm)
    history = (Message("user", "Ancienne question " * 100), Message("assistant", "Ancienne réponse"))
    async with generator.stream("q", items(), history, "mi", today="2026-09-14") as events:
        result = [event async for event in events]
    assert llm.closed
    assert llm.calls[0].messages[1:-1] == history
    assert result[-1].answer == "premier second"
    assert result[-1].diagnostics.outcome.usage == llm.usage
    assert [e.text for e in result[:-1]] == ["premier", " second"]


async def test_stream_early_exit_closes_port():
    llm = LLM()
    async with Generator(GenerationConfig(), Prompts(), Prompts(), llm).stream("q", items(), today="2026-09-14") as events:
        async for _ in events:
            break
    assert llm.closed


async def test_partial_stream_error_propagates_without_success_or_error_text():
    failure = InferenceFailure((Attempt("albert", "model", "unavailable"),), partial=True)
    llm = LLM(error=failure)
    seen = []
    with pytest.raises(InferenceFailure) as caught:
        async with Generator(GenerationConfig(), Prompts(), Prompts(), llm).stream("q", items(), today="2026-09-14") as events:
            async for event in events:
                seen.append(event)
    assert caught.value is failure and llm.closed
    assert seen == [TextDelta("premier")]


async def test_stream_rejected_empty_context_never_calls_llm():
    llm = LLM(error=AssertionError())
    async with Generator(GenerationConfig(), Prompts(), Prompts(), llm).stream("q", (), all_rejected=True, today="2026-09-14") as events:
        result = [event async for event in events]
    assert result[0] == TextDelta(NO_ANSWER)
    assert result[1].answer == NO_ANSWER and result[1].diagnostics.outcome is None
    assert not llm.calls


@pytest.mark.parametrize("statuses", [(200,), (503, 200), (503, 503)])
async def test_generation_uses_real_gateway_fallback_and_safe_double_failure(statuses):
    hosts, payloads = [], []

    def handle(request):
        hosts.append(request.url.host)
        payloads.append(json.loads(request.content))
        status = statuses[len(hosts) - 1]
        return httpx.Response(
            status,
            json={
                "choices": [{"index": 0, "message": {"content": "Selon l'article 1 du décret."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 12, "total_tokens": 135},
            },
        )

    endpoints = (
        Endpoint("albert", "primary", "https://albert.test", "test-key"),
        Endpoint("scaleway", "backup", "https://scaleway.test", "test-key"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        llm = ChatGateway(client, *endpoints, policy=RequestPolicy(max_attempts=1))
        generator = Generator(GenerationConfig(), Prompts(), PackagedPromptStore(), llm)
        if statuses[-1] == 503:
            with pytest.raises(InferenceFailure) as caught:
                await generator.generate("q", items(), "masa", today="2026-09-14")
            assert len(caught.value.attempts) == 2
            assert str(caught.value) == "inference_failure"
        else:
            result = await generator.generate("q", items(), "masa", today="2026-09-14")
            assert result.answer == "Selon l'article 1 du décret."
            assert result.diagnostics.fallback_count == len(statuses) - 1
            assert result.diagnostics.outcome.usage == TokenUsage(123, 12, 135)
    assert hosts == ["albert.test", "scaleway.test"][: len(statuses)]
    if len(payloads) == 2:
        assert payloads[0]["messages"] == payloads[1]["messages"]


@pytest.mark.parametrize(
    "template",
    [
        'JSON {"selected_ids": [0]} Question {query} {context}',
        "{query.bad} {context} {theme}",
        "{query[999]} {context}",
        "{query:invalid} {context}",
        "{query} {context} {unknown}",
    ],
)
async def test_selector_template_fallback_matches_legacy(monkeypatch, template):
    fake = Mock()
    fake.chat.return_value = '{"selected_ids": [1]}'
    monkeypatch.setattr(legacy_selector, "LLMClient", lambda **kwargs: fake)
    monkeypatch.setattr(legacy_selector, "load_prompt", lambda *args, **kwargs: template)
    old = legacy_selector.ContextSelector(legacy_config.SelectorConfig(enabled=True))
    expected = old.select("q", old_sections(sections()))
    llm = LLM(fake.chat.return_value)
    result = await ContextSelector(SelectorConfig(enabled=True), Prompts({"v3_selector_business.md": template}), Prompts(), llm).select(
        "q", sections(), today="2026-09-14"
    )
    assert result.diagnostics.user_prompt == fake.chat.call_args.args[0]
    assert [s.section_id for s in result.sections] == [s.section_id for s in expected]


async def test_generator_does_not_swallow_cancellation_and_closes_stream():
    class Blocked(LLM):
        @asynccontextmanager
        async def stream(self, request):
            async def events():
                self.started.set()
                await asyncio.Event().wait()
                yield TextDelta("unreachable")

            try:
                yield events()
            finally:
                self.closed = True

    llm = Blocked()
    generator = Generator(GenerationConfig(), Prompts(), Prompts(), llm)

    async def run():
        async with generator.stream("q", items(), today="2026-09-14") as events:
            return [event async for event in events]

    task = asyncio.create_task(run())
    await llm.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.closed


async def test_generator_complete_cancellation_and_unexpected_errors_propagate():
    for failure in (asyncio.CancelledError(), RuntimeError("bug"), RAGConfigurationError()):
        generator = Generator(GenerationConfig(), Prompts(), Prompts(), LLM(error=failure))
        with pytest.raises(type(failure)):
            await generator.generate("q", items(), today="2026-09-14")


async def test_incomplete_stream_port_is_not_reported_as_success():
    class Incomplete(LLM):
        @asynccontextmanager
        async def stream(self, request):
            async def events():
                yield TextDelta("partial")

            yield events()

    with pytest.raises(InferenceFailure) as caught:
        async with Generator(GenerationConfig(), Prompts(), Prompts(), Incomplete()).stream("q", items(), today="2026-09-14") as events:
            results = []
            async for event in events:
                results.append(event)
    assert caught.value.partial and results == [TextDelta("partial")]


@pytest.mark.parametrize("name,today", [("selector.md", "2026-01-02"), ("generator.md", "2027-12-31")])
async def test_request_date_substitution_keeps_raw_snapshot_and_exact_legacy_prompt(monkeypatch, name, today):
    raw = (await PackagedPromptStore().get(name)).value.content
    # Include a date in a DB selector template too; its format_map used to erase it.
    if name == "selector.md":
        raw = "Nous sommes le {today}.\n" + raw
    llm = LLM('{"selected_ids": [0]}')
    fake = Mock(last_provider_used="albert", fallback_count=0)
    fake.chat.return_value = llm.text
    if name == "selector.md":
        monkeypatch.setattr(legacy_selector, "LLMClient", lambda **kwargs: fake)
        monkeypatch.setattr(legacy_selector, "load_prompt", lambda *args, **kwargs: raw.replace("{today}", today))
        old = legacy_selector.ContextSelector(legacy_config.SelectorConfig(enabled=True))
        old.select("q", old_sections(sections()), ministry=resolve_ministry("mso"))
        new = ContextSelector(SelectorConfig(enabled=True), Prompts({"v3_selector_business.md": raw}), Prompts(), llm)
        result = await new.select("q", sections(), "mso", today=today)
        assert result.diagnostics.user_prompt == fake.chat.call_args.args[0]
        rendered = result.diagnostics.user_prompt
    else:
        monkeypatch.setattr(legacy_generator, "load_prompt", lambda *args, **kwargs: raw.replace("{today}", today))
        old = legacy_generator.StreamingGenerator(legacy_config.GenerationConfig())
        old._llm = fake
        old.generate("q", [legacy_models.ContextItem(**plain(i)) for i in items()], resolve_ministry("mso"))
        result = await Generator(GenerationConfig(), Prompts(), PackagedPromptStore(), llm).generate("q", items(), "mso", today=today)
        rendered = result.diagnostics.request.messages[0].content
        assert rendered == old.last_system_prompt
    assert result.diagnostics.prompt.value.content == raw
    assert today in rendered and "{today}" not in rendered


@pytest.mark.parametrize("db_error", [None, DatabaseUnavailable(), DatabaseConflict(), DatabaseFailure()])
async def test_configured_persona_prompt_keeps_same_name_packaged_fallback(db_error):
    name = "system_prompt_persona_gestionnaire_rh.md"
    configured = GenerationConfig(system_prompt_name=name)
    db = Prompts(error=db_error)
    result = await Generator(configured, db, PackagedPromptStore(), LLM("answer")).generate("q", items(), "mso", today="2026-09-14")
    expected = (ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts" / name).read_text()
    assert result.diagnostics.prompt.value.content == expected
    assert result.diagnostics.prompt.value.name == name
    assert result.diagnostics.prompt.origin == "packaged"
    assert result.diagnostics.store_errors == ((db_error.code,) if db_error else ())
    assert db.calls == [name]
    assert "Adressez toujours la réponse au gestionnaire RH" in result.diagnostics.request.messages[0].content
    assert "{today}" not in result.diagnostics.request.messages[0].content
