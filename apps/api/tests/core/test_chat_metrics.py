from dataclasses import replace
from datetime import timedelta

import pytest
from assistant_rh_api.core.models.chat import ChatInput
from assistant_rh_api.core.models.inference import Attempt

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("stream", [False, True])
async def test_measured_latency_distinguishes_request_and_generation_ttft(stream):
    runtime = Runtime()
    complete = runtime.llm.complete

    async def delayed(request):
        if request.messages[0].content.startswith("INTENT"):
            runtime.clock.value += timedelta(seconds=2)
        elif request.messages[0].content.startswith("GENERATE"):
            runtime.clock.value += timedelta(seconds=3)
        return await complete(request)

    runtime.llm.complete = delayed
    auth = (await service().login("beta", "password", "local")).context
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth, stream=stream)
    assert run.metrics.elapsed_ms == 5000
    assert run.metrics.first_token_ms == (5000 if stream else None)
    assert run.metrics.generation_first_token_ms == (3000 if stream else None)
    assert next(event.duration_ms for event in run.events if event.stage == "query-processor") == 2000
    assert next(event.duration_ms for event in run.events if event.stage == "generator") == 3000


@pytest.mark.parametrize("usage_known", [False, True])
async def test_summary_keeps_actual_provider_usage_and_full_count_despite_trace_truncation(usage_known):
    runtime = Runtime()
    complete = runtime.llm.complete

    async def fallback(request):
        result = await complete(request)
        if request.messages[0].content.startswith("GENERATE"):
            return replace(
                result,
                text="é" * 100_000,
                provider="scaleway",
                model="fallback-model",
                attempts=(Attempt("albert", "primary", "timeout"), Attempt("scaleway", "fallback-model")),
                usage=result.usage if usage_known else None,
            )
        return result

    runtime.llm.complete = fallback
    auth = (await service().login("beta", "password", "local")).context
    run, result = await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth)
    generator = run.events[-1]
    assert len(generator.output_ref["answer"]) < len(result.answer)
    assert generator.metrics["answer_characters"] == 100_000
    assert generator.metrics["provider"] == "scaleway" and generator.metrics["model"] == "fallback-model"
    assert generator.metrics["fallback_used"] is True and generator.metrics["failed_attempt_count"] == 1
    assert generator.metrics["usage_known"] is usage_known
    assert generator.metrics["total_tokens"] == (14 if usage_known else None)


async def test_short_circuit_records_intent_without_inventing_generation_usage():
    runtime = Runtime()
    runtime.llm.intent = "out_of_scope"
    auth = (await service().login("beta", "password", "local")).context
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth, stream=True)
    assert run.events[-1].metrics["should_proceed"] is False
    assert run.metrics.generation_first_token_ms is None
    assert not any(event.stage == "generator" for event in run.events)
