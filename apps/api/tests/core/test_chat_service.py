import asyncio
from dataclasses import replace

import pytest
from assistant_rh_api.core.errors import ApplicationError, DatabaseFailure, InferenceFailure
from assistant_rh_api.core.models.chat import Cancellation, ChatInput
from assistant_rh_api.core.models.context import ContextBuildDiagnostics, ContextBuildResult
from assistant_rh_api.core.models.inference import Attempt
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.prompt_policy import NO_ANSWER
from assistant_rh_api.core.sources import SOURCES_MARKER

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime

pytestmark = pytest.mark.anyio


async def auth():
    return (await service().login("beta", "password", "local")).context


async def test_real_pipeline_all_stages_are_persisted_before_return():
    runtime = Runtime()
    run, result = await runtime.service.complete(ChatInput("assistant-rh", "Question RH", conversation_id="correlation"), await auth())
    assert runtime.runs.rows[run.turn_id] is run
    assert run.status == "completed" and run.conversation_id == "correlation"
    assert run.answer.startswith("Réponse MATTE") and SOURCES_MARKER in run.answer
    assert result.usage.total_tokens == 14
    assert [event.stage for event in run.events] == [
        "configuration",
        "query-processor",
        "retriever",
        "section-aggregator",
        "context-selector",
        "context-builder",
        "generator",
    ]
    assert all(event.output_ref is not None for event in run.events)
    assert run.sources[0].url == "" and run.sources[0].access == "authenticated"
    assert "SECRET" not in run.answer
    assert runtime.config.calls == 1
    assert {request.source for request in runtime.search.calls} == {"matte", "service_public", "dgafp"}
    assert not any(name.startswith("last_") for name in vars(runtime.pipelines[0]))


async def test_concurrent_ministries_do_not_share_prompt_source_result_or_traces():
    runtime = Runtime()
    one = await auth()
    two = replace(
        one,
        group=replace(one.group, slug="second", allowed_ministries=("mi",), default_ministry="mi"),
        session=replace(one.session, group_slug="second", token_hash="second-hash"),
    )
    results = await asyncio.gather(
        runtime.service.complete(ChatInput("assistant-rh", "Question MATTE"), one),
        runtime.service.complete(ChatInput("assistant-rh", "Question MI"), two),
    )
    runs = [result[0] for result in results]
    assert len({r.turn_id for r in runs}) == len({r.trace_id for r in runs}) == 2
    for run, expected, other in [(runs[0], "matte", "mi"), (runs[1], "mi", "matte")]:
        assert expected.upper() in run.answer and other.upper() not in run.answer
        assert all(source.doc_ref == "guide-" + expected for source in run.sources)
        generation = run.events[-1].output_ref
        prompt = generation["diagnostics"]["request"]["messages"][0]["content"]
        assert "GENERATE " + expected.upper() in prompt
        assert "GENERATE " + other.upper() not in prompt
    assert runs[0].group_slug != runs[1].group_slug and runs[0].session_hash != runs[1].session_hash


async def test_direct_response_skips_corpus_and_no_answer_preserves_retry_rules():
    runtime = Runtime()
    runtime.llm.intent = "out_of_scope"
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert run.status == "completed" and not run.sources and not runtime.search.calls
    assert len(runtime.llm.calls) == 1
    runtime = Runtime()
    runtime.llm.selector_responses = ['{"selected_ids":[]}', '{"selected_ids":[]}']
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert "pas trouvé" in run.answer and not run.sources
    assert run.diagnostics["selector_retry_triggered"] is True
    assert [e.attempt_name for e in run.events if e.stage == "retriever"] == ["initial", "selector_retry"]
    assert not any(r.messages[0].content.startswith("GENERATE") for r in runtime.llm.calls)


async def test_retry_can_recover_and_only_final_sources_are_retained():
    runtime = Runtime()
    runtime.llm.selector_responses = ['{"selected_ids":[]}', '{"selected_ids":[0]}']
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert run.sources and run.diagnostics["selector_retry_succeeded"] is True


async def test_failure_is_persisted_safely_without_candidate_source_authority():
    runtime = Runtime()
    runtime.llm.failure = RuntimeError("secret DSN and token")
    with pytest.raises(ApplicationError):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "failed" and not run.sources and not run.answer
    assert run.events[-1].status == "failed"
    assert "secret DSN" not in repr(run.events) + repr(run.diagnostics)


async def test_storage_failure_never_returns_success():
    runtime = Runtime()
    runtime.runs.failure = DatabaseFailure()
    with pytest.raises(ApplicationError):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert not runtime.runs.rows
    assert [r.status for r in runtime.runs.calls] == ["completed", "failed"]


async def test_cooperative_cancellation_and_stage_events_prepare_c7():
    runtime = Runtime()
    cancellation = Cancellation()
    events = []

    class Sink:
        async def publish(self, event):
            events.append(event)
            if event.stage == "context-selector" and event.phase == "completed":
                cancellation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth(), sink=Sink(), cancellation=cancellation)
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "cancelled" and not run.sources
    assert len({event.turn_id for event in events}) == 1
    assert not any(event.stage == "generator" for event in events)


async def test_task_cancellation_stops_inflight_stage_and_persists_cancelled_run():
    runtime = Runtime()
    runtime.llm.entered, runtime.llm.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth()))
    await runtime.llm.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "cancelled" and run.events[-1].status == "cancelled" and not run.sources


@pytest.mark.parametrize("failed_call", [1, 2])
async def test_embedding_outage_initial_or_retry_never_becomes_no_answer(monkeypatch, failed_call):
    from apps.api.tests.chat_fakes import Embeddings

    calls = 0
    original = Embeddings.embed
    attempts = (
        Attempt("albert", "openweight-embeddings", "timeout"),
        Attempt("scaleway", "bge-multilingual-gemma2", "unavailable", 503),
    )

    async def fail_embedding(self, text):
        nonlocal calls
        calls += 1
        if calls == failed_call:
            raise InferenceFailure(attempts)
        return await original(self, text)

    monkeypatch.setattr(Embeddings, "embed", fail_embedding)
    runtime = Runtime()
    runtime.llm.selector_responses = ['{"selected_ids":[]}']
    with pytest.raises(InferenceFailure) as caught:
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert caught.value.attempts == attempts
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "failed" and not run.sources and not run.answer
    assert [(a["provider"], a["error"], a["status"]) for a in run.diagnostics["inference_attempts"]] == [
        ("albert", "timeout", None),
        ("scaleway", "unavailable", 503),
    ]
    assert run.events[-1].metrics["inference_attempts"] == run.diagnostics["inference_attempts"]
    assert not any(request.messages[0].content.startswith("GENERATE") for request in runtime.llm.calls)


async def test_retry_empty_candidates_keeps_initial_no_answer():
    runtime = Runtime()
    runtime.llm.selector_responses = ['{"selected_ids":[]}']

    class Sink:
        async def publish(self, event):
            if event.stage == "retriever" and event.attempt_name == "selector_retry" and event.phase == "started":
                runtime.search.empty = True

    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth(), sink=Sink())
    assert "pas trouvé" in run.answer and not run.sources and run.diagnostics["selector_all_rejected"]


@pytest.mark.parametrize("empty_at", ["retrieval", "context_builder"])
@pytest.mark.parametrize("selector_enabled", [False, True])
async def test_empty_final_context_never_calls_generation_without_explicit_rejection(monkeypatch, empty_at, selector_enabled):
    runtime = Runtime(v3_enable_selector=selector_enabled)
    if empty_at == "retrieval":
        runtime.search.empty = True
    else:

        async def empty_build(self, sections):
            assert sections
            return ContextBuildResult(items=(), resolved_refs={}, diagnostics=ContextBuildDiagnostics())

        monkeypatch.setattr(ContextBuilder, "build", empty_build)
    run, result = await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert run.status == "completed" and run.answer == NO_ANSWER and not run.sources
    assert result.usage.total_tokens == 0
    assert runtime.runs.rows[run.turn_id] is run
    assert not run.diagnostics["selector_all_rejected"] and not run.diagnostics["selector_retry_triggered"]
    assert run.events[-1].output_ref["diagnostics"]["status"] == "no_answer"
    assert not any(request.messages[0].content.startswith("GENERATE") for request in runtime.llm.calls)


async def test_failure_observer_is_not_allowed_to_mask_error():
    runtime = Runtime()
    runtime.llm.failure = RuntimeError("secret")
    events = []

    class Sink:
        async def publish(self, event):
            events.append(event)
            if event.phase == "failed":
                raise RuntimeError("observer error")

    with pytest.raises(ApplicationError):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth(), sink=Sink())
    assert events[-1].phase == "failed"
    assert next(iter(runtime.runs.rows.values())).status == "failed"


async def test_request_date_is_captured_once_in_paris_even_when_utc_date_differs():
    from datetime import datetime, timezone

    runtime = Runtime()
    runtime.clock.value = datetime(2026, 9, 15, 23, 30, tzinfo=timezone.utc)
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), await auth())
    assert run.timestamp.day == 15
    assert "2026-09-16" in runtime.llm.calls[0].messages[0].content
    assert "2026-09-16" in runtime.llm.calls[-1].messages[0].content
