"""The M1 bridge must feed the same evaluation metrics with real core outputs."""

import asyncio
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace

import pytest
from assistant_rh_rag_pipeline.ministry_scope import build_retrieval_scope

from apps.api.tests.chat_fakes import Runtime
from scripts.conformance.m1_live import join_evaluations
from scripts.conformance.m1_runtime import CoreEvaluator
from src.goldset.eval import _aggregate_token_usage, context_payload, retrieved_doc_ids, stage_retrieval_metrics


@pytest.mark.anyio
async def test_core_eval_bridge_preserves_document_ids_and_request_isolation():
    runtime = Runtime()
    loop = asyncio.get_running_loop()

    async def run(ministry):
        adapter = CoreEvaluator(runtime.service, loop, "local-test")
        return await asyncio.to_thread(adapter.run_with_trace, "Question " + ministry, retrieval_scope=build_retrieval_scope(ministry))

    matte, mi = await asyncio.gather(run("matte"), run("mi"))
    for result, ministry, other in ((matte, "matte", "mi"), (mi, "mi", "matte")):
        contexts = context_payload(result)
        ids = retrieved_doc_ids(result, contexts)
        assert "guide-" + ministry in ids and "guide-" + other not in ids
        assert stage_retrieval_metrics(result.metadata, ["guide-" + ministry])
        saved = runtime.runs.rows[result.metadata["turn_id"]]
        assert saved.selected_ministry == ministry and saved.group_slug == "m1-core-" + ministry
        assert result.metadata["generator_model_used"] == "synthetic"
        assert result.metadata["generator_used_fallback"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("month", [1, 7])
async def test_core_eval_uses_service_clock_and_french_prompt_date(month):
    runtime = Runtime()
    runtime.clock.value = datetime(2026, month, 10, 23, 30, tzinfo=UTC)
    adapter = CoreEvaluator(runtime.service, asyncio.get_running_loop(), "local-test")

    result = await adapter.run("Question", "matte")

    saved = runtime.runs.rows[result.metadata["turn_id"]]
    assert saved.timestamp == runtime.clock.value
    assert len(runtime.llm.calls) == 3
    for request in runtime.llm.calls:
        assert f"2026-{month:02d}-11" in request.messages[0].content


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pair_joins_thread_before_propagating_failure_or_repeated_cancellation(cancel):
    release, started, finished = Event(), Event(), Event()

    def surviving_arm():
        started.set()
        assert release.wait(5)
        finished.set()

    async def failing_arm():
        await asyncio.to_thread(started.wait, 5)
        raise RuntimeError("write failed")

    pair = asyncio.create_task(join_evaluations(failing_arm(), asyncio.to_thread(surviving_arm)))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(2):
            if cancel:
                pair.cancel()
            await asyncio.sleep(0.01)
            assert not pair.done()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await pair
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(pair, return_exceptions=True)


@pytest.mark.anyio
async def test_core_timeout_waits_for_cancelled_run_persistence(monkeypatch):
    monkeypatch.setattr("scripts.conformance.m1_runtime.CORE_TIMEOUT_SECONDS", 0.05)
    runtime = Runtime()
    runtime.llm.release = asyncio.Event()
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    adapter = CoreEvaluator(runtime.service, asyncio.get_running_loop(), "timeout-test")
    worker = asyncio.create_task(asyncio.to_thread(adapter.run_with_trace, "Question", retrieval_scope=build_retrieval_scope("matte")))
    try:
        await asyncio.wait_for(runtime.runs.entered.wait(), 2)
        await asyncio.sleep(0.01)
        assert not worker.done() and not runtime.runs.rows
        runtime.runs.release.set()
        with pytest.raises(TimeoutError):
            await worker
        assert [run.status for run in runtime.runs.rows.values()] == ["cancelled"]
    finally:
        runtime.runs.release.set()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("retry", [False, True])
async def test_core_eval_feeds_usage_estimates_including_selector_retry(retry):
    from assistant_rh_rag_pipeline.models import estimate_tokens

    runtime = Runtime()
    if retry:
        runtime.llm.selector_responses = ['{"selected_ids":[],"reason":"missing"}']
    adapter = CoreEvaluator(runtime.service, asyncio.get_running_loop(), "usage-test")
    result = await adapter.run("Question", "matte")
    selectors = [call for call in runtime.llm.calls if call.messages[0].content.startswith("SELECT")]
    assert len(selectors) == (2 if retry else 1)
    assert result.metadata["selector_prompt_chars"] == sum(len(call.messages[-1].content) for call in selectors)
    assert result.metadata["selector_response_chars"] > 0
    item = SimpleNamespace(timing=result.timing, metadata=result.metadata, judge_result={}, ragas_metrics={})
    usage = _aggregate_token_usage([item])
    assert usage["generator_albert_est"]["completion_tokens"] == estimate_tokens(result.answer) > 0
    assert usage["selector_albert_est"]["prompt_tokens"] > 0
    assert usage["selector_albert_est"]["completion_tokens"] > 0
