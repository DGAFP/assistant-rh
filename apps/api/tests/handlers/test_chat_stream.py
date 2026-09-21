"""Exercise the real C6 pipeline and ASGI stream, including live disconnects."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import openai
import pytest
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.chat import ChatInput, PipelineEvent
from assistant_rh_api.core.models.inference import Attempt, StreamCompleted, TextDelta, TokenUsage
from assistant_rh_api.core.sources import SOURCES_MARKER
from assistant_rh_api.gateways.chat import ChatGateway
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_stream import StreamSettings

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import LLM, Runtime
from apps.api.tests.gateways.conftest import ALBERT, POLICY, WireStream
from apps.api.tests.gateways.test_chat import event, reply

pytestmark = pytest.mark.anyio
BODY = {"model": "assistant-rh", "messages": [{"role": "user", "content": "Question RH"}], "stream": True}


class StreamLLM(LLM):
    def __init__(self):
        super().__init__()
        self.tokens = ["Réponse ", "fondée ", "sur les sources."]
        self.produced = 0
        self.closed = False
        self.stream_failure = None
        self.stream_release = None

    @asynccontextmanager
    async def stream(self, request):
        self.calls.append(request)

        async def events():
            for token in self.tokens:
                self.produced += 1
                yield TextDelta(token)
                if self.stream_release:
                    await self.stream_release.wait()
            if self.stream_failure:
                raise self.stream_failure
            yield StreamCompleted("albert", "synthetic", (Attempt("albert", "synthetic"),), "stop", TokenUsage(10, 4, 14))

        try:
            yield events()
        finally:
            self.closed = True


@pytest.fixture
async def setup():
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    runtime.llm = StreamLLM()
    app = create_app(
        auth_service=auth,
        chat_service=runtime.service,
        stream_settings=StreamSettings(workers=1, queue_size=2, delta_chars=16, ping_seconds=0.01, send_timeout=1),
    )
    yield app, runtime, issued
    await app.state.stream_workers.aclose()


def client_for(app, issued):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + issued.access_token}
    )


class Exchange:
    """No buffering ASGI client; disconnect and send pressure are controllable."""

    def __init__(self, app, issued, body=None, send_hook=None):
        self.messages = []
        self.received = asyncio.Queue()
        self.disconnected = asyncio.Event()
        self.first = True
        self.send_hook = send_hook
        payload = json.dumps(BODY if body is None else body).encode()

        async def receive():
            if self.first:
                self.first = False
                return {"type": "http.request", "body": payload, "more_body": False}
            await self.disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if self.send_hook:
                await self.send_hook(message)
            self.messages.append(message)
            await self.received.put(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1000),
            "headers": [(b"authorization", ("Bearer " + issued.access_token).encode()), (b"content-type", b"application/json")],
        }
        self.task = asyncio.create_task(app(scope, receive, send))

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages)

    async def until(self, needle):
        async with asyncio.timeout(3):
            while needle not in self.body:
                await self.received.get()

    async def finish(self):
        await asyncio.wait_for(self.task, 3)

    async def disconnect(self):
        self.disconnected.set()
        await self.finish()


@pytest.mark.parametrize("include_usage", [False, True])
async def test_sdk_complete_stream_persisted_sources_and_usage(setup, include_usage):
    app, runtime, issued = setup
    async with openai.AsyncOpenAI(
        api_key=issued.access_token,
        base_url="http://test/v1",
        http_client=client_for(app, issued),
        max_retries=0,
        _strict_response_validation=True,
    ) as sdk:
        stream = await sdk.chat.completions.create(
            model="assistant-rh",
            messages=BODY["messages"],
            stream=True,
            stream_options={"include_usage": include_usage},
            extra_body={"metadata": {"conversation_id": "correlation"}, "tools": [], "tool_choice": "required"},
        )
        chunks = [chunk async for chunk in stream]
    first = chunks[0]
    assert first.choices[0].delta.role == "assistant"
    assert first.choices[0].delta.content == ""
    assert {c.id for c in chunks} == {first.id}
    assert {c.created for c in chunks} == {first.created}
    assert {c.model for c in chunks} == {"assistant-rh-matte"}
    terminal = next(c for c in chunks if c.choices and c.choices[0].finish_reason)
    ext = terminal.model_extra["x_assistant_rh"]
    run = runtime.runs.rows[ext["turn_id"]]
    assert run.status == "completed" and run.conversation_id == "correlation"
    assert run.answer == "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert len(run.events) == 7 and all(e.status == "ok" for e in run.events)
    assert ext["sources"][0]["doc_ref"] == "guide-matte" and ext["sources"][0]["url"] is None
    assert (chunks[-1].choices == []) is include_usage
    if include_usage:
        assert chunks[-1].usage.total_tokens == 14
    assert runtime.llm.closed and not app.state.stream_workers.active


async def test_ping_during_retrieval_and_persistence_precedes_terminal(setup):
    app, runtime, issued = setup
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.search.search

    async def slow_search(request):
        entered.set()
        await release.wait()
        return await original(request)

    runtime.search.search = slow_search
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    exchange = Exchange(app, issued)
    await asyncio.wait_for(entered.wait(), 3)
    await exchange.until(b": ping\n\n")
    assert b'"role":"assistant"' in exchange.body and not runtime.runs.rows
    release.set()
    await asyncio.wait_for(runtime.runs.entered.wait(), 3)
    assert b'"content":"R' in exchange.body
    assert b'"finish_reason":"stop"' not in exchange.body and b"[DONE]" not in exchange.body
    runtime.runs.release.set()
    await exchange.until(b"[DONE]")
    assert next(iter(runtime.runs.rows.values())).status == "completed"
    await exchange.finish()
    assert exchange.body.count(b"data: [DONE]") == 1
    assert dict(exchange.messages[0]["headers"])[b"content-type"].startswith(b"text/event-stream")
    assert b"content-length" not in dict(exchange.messages[0]["headers"])


@pytest.mark.parametrize("after_content", [False, True])
async def test_post_headers_failure_is_sdk_error_without_done(setup, after_content):
    app, runtime, issued = setup
    failure = InferenceFailure(
        (Attempt("albert", "synthetic", "timeout"), Attempt("scaleway", "synthetic", "unavailable", 503)), partial=after_content
    )
    runtime.llm.stream_failure = failure
    if not after_content:
        runtime.llm.tokens = []
    exchange = Exchange(app, issued)
    await exchange.finish()
    assert b'"code":"stream_error"' in exchange.body
    assert b"[DONE]" not in exchange.body and b'"finish_reason":"stop"' not in exchange.body
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "failed" and not run.sources
    assert bool(run.answer) is after_content and run.diagnostics["partial"] is after_content
    assert len(run.diagnostics["inference_attempts"]) == 2
    assert run.events[-1].status == "failed"
    async with openai.AsyncOpenAI(api_key=issued.access_token, base_url="http://test/v1", http_client=client_for(app, issued), max_retries=0) as sdk:
        stream = await sdk.chat.completions.create(model="assistant-rh", messages=BODY["messages"], stream=True)
        with pytest.raises(openai.APIError) as error:
            _ = [chunk async for chunk in stream]
        assert error.value.code == "stream_error"


@pytest.mark.parametrize("double_failure", [False, True])
async def test_persistence_failure_suppresses_terminal_and_logs_safe_correlation(setup, double_failure, caplog):
    app, runtime, issued = setup
    original = runtime.runs.finalize

    async def fail(run):
        if run.status == "completed" or double_failure:
            runtime.runs.calls.append(run)
            raise RuntimeError("SECRET DSN password")
        await original(run)

    runtime.runs.finalize = fail
    exchange = Exchange(app, issued)
    await exchange.finish()
    assert b'"code":"stream_error"' in exchange.body and b"[DONE]" not in exchange.body
    assert [r.status for r in runtime.runs.calls] == ["completed", "failed"]
    assert "SECRET" not in caplog.text and b"SECRET" not in exchange.body
    assert runtime.runs.calls[0].turn_id in caplog.text
    if not double_failure:
        run = next(iter(runtime.runs.rows.values()))
        assert run.status == "failed" and run.answer and not run.sources
    assert not app.state.stream_workers.active


@pytest.mark.parametrize("partial", [False, True])
async def test_disconnect_cancels_provider_persists_partial_and_joins_tasks(setup, partial):
    app, runtime, issued = setup
    if partial:
        runtime.llm.stream_release = asyncio.Event()
    else:
        runtime.llm.entered, runtime.llm.release = asyncio.Event(), asyncio.Event()
    before = asyncio.all_tasks()
    exchange = Exchange(app, issued)
    await exchange.until(b'"content":"R' if partial else b'"role":"assistant"')
    await exchange.disconnect()
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "cancelled" and not run.sources
    assert bool(run.answer) is partial and run.diagnostics["partial"] is partial
    assert run.diagnostics["cancellation"] == "disconnect"
    assert run.events[-1].status == "cancelled"
    if partial:
        assert runtime.llm.closed
    assert b"[DONE]" not in exchange.body and not app.state.stream_workers.active
    assert not (asyncio.all_tasks() - before)


async def test_disconnect_during_commit_finishes_one_successful_transaction(setup):
    app, runtime, issued = setup
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    exchange = Exchange(app, issued)
    await asyncio.wait_for(runtime.runs.entered.wait(), 3)
    exchange.disconnected.set()
    await asyncio.sleep(0.02)
    assert not exchange.task.done() and not runtime.runs.rows
    runtime.runs.release.set()
    await exchange.finish()
    assert [run.status for run in runtime.runs.calls] == ["completed"]
    assert next(iter(runtime.runs.rows.values())).status == "completed"
    assert b"[DONE]" not in exchange.body and not app.state.stream_workers.active


async def test_workers_and_backpressure_are_bounded(setup):
    app, runtime, issued = setup
    runtime.llm.tokens = ["x" * 1000] * 30
    blocked, release = asyncio.Event(), asyncio.Event()

    async def slow_send(message):
        if b'"content":"x' in message.get("body", b""):
            blocked.set()
            await release.wait()

    exchange = Exchange(app, issued, send_hook=slow_send)
    await asyncio.wait_for(blocked.wait(), 3)
    await asyncio.sleep(0.02)
    response = next(iter(app.state.stream_workers.active))
    assert response.queue.qsize() == 2
    assert runtime.llm.produced == 1  # even a single large delta is split with pressure
    calls = runtime.config.calls
    async with client_for(app, issued) as client:
        saturated = await client.post("/v1/chat/completions", json=BODY)
    assert saturated.status_code == 503 and saturated.json()["error"]["code"] == "service_unavailable"
    assert runtime.config.calls == calls
    await exchange.disconnect()
    assert runtime.llm.closed and not app.state.stream_workers.active
    assert next(iter(runtime.runs.rows.values())).status == "cancelled"


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
async def test_send_failure_stops_worker_and_persists_run(setup, failure):
    app, runtime, issued = setup
    app.state.stream_workers.settings = replace(app.state.stream_workers.settings, send_timeout=0.02)
    runtime.llm.stream_release = asyncio.Event()

    async def fail_send(message):
        if b'"content":"R' in message.get("body", b""):
            if failure == "oserror":
                raise OSError("disconnected")
            await asyncio.Event().wait()

    exchange = Exchange(app, issued, send_hook=fail_send)
    await exchange.finish()
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "cancelled" and run.diagnostics["cancellation"] == "send_failed"
    assert runtime.llm.closed and not app.state.stream_workers.active


async def test_repeated_asgi_cancellation_waits_for_finalization(setup):
    app, runtime, issued = setup
    runtime.llm.stream_release = asyncio.Event()
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    exchange = Exchange(app, issued)
    await exchange.until(b'"content":"R')
    exchange.task.cancel()
    await asyncio.wait_for(runtime.runs.entered.wait(), 3)
    for _ in range(3):
        exchange.task.cancel()
        await asyncio.sleep(0)
    assert not exchange.task.done()
    runtime.runs.release.set()
    with pytest.raises(asyncio.CancelledError):
        await exchange.finish()
    assert next(iter(runtime.runs.rows.values())).status == "cancelled"
    assert not app.state.stream_workers.active


async def test_cancelled_run_storage_timeout_is_bounded(setup, caplog):
    app, runtime, issued = setup
    runtime.llm.stream_release = asyncio.Event()
    runtime.service._finalization_timeout = 0.02
    runtime.runs.release = asyncio.Event()
    exchange = Exchange(app, issued)
    await exchange.until(b'"content":"R')
    await exchange.disconnect()
    assert not runtime.runs.rows and not app.state.stream_workers.active
    assert "cancellation finalization failed" in caplog.text


@pytest.mark.parametrize("mode", ["direct", "no_answer"])
async def test_short_circuits_stream_as_success_without_sources(setup, mode):
    app, runtime, issued = setup
    if mode == "direct":
        runtime.llm.intent = "out_of_scope"
    else:
        runtime.llm.selector_responses = ['{"selected_ids":[]}', '{"selected_ids":[]}']
    exchange = Exchange(app, issued)
    await exchange.finish()
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "completed" and run.answer and not run.sources
    assert b"[DONE]" in exchange.body and b'"sources":[]' in exchange.body
    assert not any(c.messages[0].content.startswith("GENERATE") for c in runtime.llm.calls)


async def test_shutdown_cancels_active_stream_before_resources_close(setup):
    app, runtime, issued = setup
    runtime.llm.stream_release = asyncio.Event()
    exchange = Exchange(app, issued)
    await exchange.until(b'"content":"R')
    await app.state.stream_workers.aclose()
    await exchange.finish()
    assert next(iter(runtime.runs.rows.values())).status == "cancelled"
    assert runtime.llm.closed and not app.state.stream_workers.active
    assert b"[DONE]" not in exchange.body


@pytest.mark.parametrize(
    "change,status,code",
    [
        ({"model": "unknown"}, 404, "model_not_found"),
        ({"model": "assistant-rh-masa"}, 403, "ministry_forbidden"),
        ({"n": 2}, 422, "unsupported_n"),
        ({"messages": BODY["messages"] * 33}, 422, "too_many_messages"),
        ({"ignored": "x" * 1_048_576}, 413, "request_too_large"),
    ],
)
async def test_stream_validation_precedes_admission_and_headers(setup, change, status, code):
    app, runtime, issued = setup
    async with client_for(app, issued) as client:
        response = await client.post("/v1/chat/completions", json={**BODY, **change})
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.headers["content-type"] == "application/json"
    assert not runtime.config.calls and not app.state.stream_workers.active


async def test_stream_auth_rejection_precedes_worker(setup):
    app, runtime, issued = setup
    async with client_for(app, issued) as client:
        response = await client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401 and not runtime.config.calls and not app.state.stream_workers.active


async def test_stream_history_and_concurrent_ministries_remain_request_owned(setup):
    app, runtime, issued = setup
    app.state.stream_workers.settings = replace(app.state.stream_workers.settings, workers=2)
    auth = app.state.auth_service
    auth.groups.rows["other"] = replace(auth.groups.rows["beta"], slug="other", allowed_ministries=("mi",), default_ministry="mi")
    second = await auth.login("other", "password", "local")
    messages = [{"role": "system", "content": "CLIENT_SECRET"}]
    for index in range(7):
        messages += [{"role": "user", "content": f"U{index}"}, {"role": "assistant", "content": f"A{index}\n\n---\n**Sources :**\nOLD"}]
    messages += BODY["messages"]
    one = Exchange(app, issued, {**BODY, "messages": messages, "metadata": {"conversation_id": "same"}})
    two = Exchange(app, second, {**BODY, "messages": messages, "metadata": {"conversation_id": "same"}})
    await asyncio.gather(one.finish(), two.finish())
    assert len(runtime.runs.rows) == 2
    for run in runtime.runs.rows.values():
        assert run.sources[0].doc_ref == "guide-" + run.selected_ministry
        assert run.conversation_id == "same" and run.group_slug == ("beta" if run.selected_ministry == "matte" else "other")
        prompt = run.events[-1].output_ref["diagnostics"]["request"]["messages"]
        assert run.selected_ministry.upper() in prompt[0]["content"]
        assert len(prompt) == 2  # Generation matches C6; history is used by the query processor.
        assert "CLIENT_SECRET" not in str(prompt) and "OLD" not in str(prompt)
    assert not app.state.stream_workers.active


async def test_shutdown_joins_stalled_transport_too(setup):
    app, runtime, issued = setup
    runtime.llm.tokens = ["x"] * 100
    blocked = asyncio.Event()

    async def stalled_send(message):
        if b'"content":"x' in message.get("body", b""):
            blocked.set()
            await asyncio.Event().wait()

    before = asyncio.all_tasks()
    exchange = Exchange(app, issued, send_hook=stalled_send)
    await asyncio.wait_for(blocked.wait(), 3)
    await asyncio.wait_for(app.state.stream_workers.aclose(), 0.5)
    await exchange.finish()
    assert runtime.llm.closed and not app.state.stream_workers.active
    assert not (asyncio.all_tasks() - before)


async def test_transport_choice_keeps_generation_inputs_answer_and_sources_identical(setup):
    app, runtime, issued = setup
    runtime.llm = LLM()
    payload = {
        **BODY,
        "messages": [
            {"role": "user", "content": "PREVIOUS_QUESTION"},
            {"role": "assistant", "content": "PREVIOUS_ANSWER"},
            *BODY["messages"],
        ],
    }
    async with client_for(app, issued) as client:
        plain = (await client.post("/v1/chat/completions", json={**payload, "stream": False})).json()
        old_calls = list(runtime.llm.calls)
        runtime.llm.calls.clear()
        streamed = await client.post("/v1/chat/completions", json=payload)
    assert runtime.llm.calls == old_calls
    assert "PREVIOUS_QUESTION" in old_calls[0].messages[0].content
    assert "PREVIOUS_QUESTION" not in str(old_calls[-1])
    chunks = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: {")]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == plain["choices"][0]["message"]["content"]
    assert chunks[-1]["x_assistant_rh"]["sources"] == plain["x_assistant_rh"]["sources"]


async def test_real_gateway_normalizes_received_and_persisted_answers_in_both_transports(setup):
    app, runtime, issued = setup
    raw_answer = " \nRéponse test. \n"

    def provider(request):
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        if prompt.startswith("INTENT"):
            return httpx.Response(200, json=reply('{"intent":"rag_query","confidence":0.9}'))
        if prompt.startswith("SELECT"):
            return httpx.Response(200, json=reply('{"selected_ids":[0]}'))
        if body["stream"]:
            return httpx.Response(
                200, stream=WireStream(event(" \n"), event("Réponse test."), event(" \n"), event(reason="stop"), b"data: [DONE]\n\n")
            )
        return httpx.Response(200, json=reply(raw_answer))

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        runtime.llm = ChatGateway(upstream, ALBERT, policy=POLICY)
        async with client_for(app, issued) as client:
            plain = await client.post("/v1/chat/completions", json={**BODY, "stream": False})
            streamed = await client.post("/v1/chat/completions", json=BODY)
    chunks = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: {")]
    text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    full = plain.json()["choices"][0]["message"]["content"]
    assert text == full
    assert full.split(SOURCES_MARKER)[0] == raw_answer.strip()
    assert len(runtime.runs.rows) == 2
    assert all(run.answer == full for run in runtime.runs.rows.values())


async def test_stage_notifications_do_not_consume_stream_queue_capacity(setup):
    app, runtime, issued = setup
    auth = issued.context
    model = app.state.model_service.resolve("assistant-rh", auth.group)
    response = app.state.stream_workers.response(runtime.service, ChatInput("assistant-rh", "Question"), auth, model, include_usage=False)
    try:
        for stage in ("configuration", "retriever", "generator"):
            for phase in ("started", "completed"):
                await asyncio.wait_for(response.publish(PipelineEvent(response.context.turn_id, stage, phase)), 0.1)
        assert response.queue.empty()
    finally:
        # This test owns a response that was intentionally never served.
        app.state.stream_workers.active.discard(response)
        response.finished.set()
