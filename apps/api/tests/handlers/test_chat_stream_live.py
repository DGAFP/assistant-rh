"""Real TCP proof: no ASGITransport buffering, fake I/O ports, real API pipeline."""

import asyncio
import socket

import httpx
import openai
import pytest
import uvicorn
from assistant_rh_api.__main__ import main
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_stream import StreamSettings

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.handlers.test_chat_stream import BODY, StreamLLM

pytestmark = pytest.mark.anyio


@pytest.fixture
async def live(monkeypatch):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    runtime.llm = StreamLLM()
    resource_closed = asyncio.Event()

    class DatabaseResource:
        async def open(self):
            pass

        async def close(self):
            resource_closed.set()

    async def maintain_sessions(_):
        await asyncio.Event().wait()

    monkeypatch.setattr("assistant_rh_api.handlers.app.maintain_sessions", maintain_sessions)
    app = create_app(
        database=DatabaseResource(),
        auth_service=auth,
        chat_service=runtime.service,
        rag_configuration_service=RAGConfigurationService(runtime.config),
        environ={},
        stream_settings=StreamSettings(ping_seconds=0.01),
    )
    app.state.test_resource_closed = resource_closed
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    # Exercise the entrypoint's shutdown policy, not a test-only timeout.
    server_options = {}
    with monkeypatch.context() as patch:
        patch.setattr(uvicorn, "run", lambda app_path, **options: server_options.update(options))
        main()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", **server_options))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield app, runtime, issued, "http://127.0.0.1:" + str(sock.getsockname()[1]), server, task
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()


async def test_sdk_consumes_live_stream_and_post_header_error(live):
    app, runtime, issued, base, _, _ = live
    async with openai.AsyncOpenAI(api_key=issued.access_token, base_url=base + "/v1", max_retries=0) as sdk:
        stream = await sdk.chat.completions.create(model="assistant-rh", messages=BODY["messages"], stream=True)
        chunks = [chunk async for chunk in stream]
        terminal = chunks[-1]
        assert runtime.runs.rows[terminal.model_extra["x_assistant_rh"]["turn_id"]].status == "completed"
        runtime.llm.stream_failure = InferenceFailure((), partial=True)
        stream = await sdk.chat.completions.create(model="assistant-rh", messages=BODY["messages"], stream=True)
        with pytest.raises(openai.APIError) as failure:
            _ = [chunk async for chunk in stream]
        assert failure.value.code == "stream_error"
    assert sorted(run.status for run in runtime.runs.rows.values()) == ["completed", "failed"]


async def test_live_ping_while_retrieval_waits_and_socket_close_cancels(live):
    app, runtime, issued, base, _, _ = live
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.search.search

    async def slow_search(request):
        entered.set()
        await release.wait()
        return await original(request)

    runtime.search.search = slow_search
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + issued.access_token}, timeout=3) as client:
        async with client.stream("POST", "/v1/chat/completions", json=BODY) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line == ": ping":
                    assert entered.is_set()
                    break
            # Other requests remain responsive while retrieval is pending.
            assert (await client.get("/v1/models")).status_code == 200
    async with asyncio.timeout(3):
        while app.state.stream_workers.active:
            await asyncio.sleep(0.01)
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "cancelled" and not run.sources and not run.answer


@pytest.mark.parametrize("pending", ["generation", "commit"])
async def test_server_shutdown_with_connected_client_waits_for_finalization(live, pending):
    app, runtime, issued, base, server, serving = live
    if pending == "generation":
        runtime.llm.stream_release = asyncio.Event()
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    received_content = asyncio.Event()
    lines = []

    async def consume():
        async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + issued.access_token}, timeout=3) as client:
            try:
                async with client.stream("POST", "/v1/chat/completions", json=BODY) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        lines.append(line)
                        if '"content":"Réponse ' in line:
                            received_content.set()
            except httpx.RemoteProtocolError:
                # Cancelling an ASGI response may close chunked HTTP without a terminator.
                pass

    reader = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(received_content.wait(), 3)
        if pending == "commit":
            await asyncio.wait_for(runtime.runs.entered.wait(), 3)
        # Keep the client reading. Only the real server shutdown may cancel work.
        server.should_exit = True
        async with asyncio.timeout(8):
            await runtime.runs.entered.wait()
            while not app.state.stream_workers.closed:
                await asyncio.sleep(0.01)
        assert runtime.llm.closed
        assert not serving.done() and not runtime.runs.rows

        # Shutdown must join the shielded transaction before it can finish.
        runtime.runs.release.set()
        await asyncio.wait_for(serving, 3)
        await asyncio.wait_for(reader, 3)
        expected_status = "cancelled" if pending == "generation" else "completed"
        assert [run.status for run in runtime.runs.calls] == [expected_status]
        run = next(iter(runtime.runs.rows.values()))
        assert run.status == expected_status and run.answer
        assert bool(run.sources) is (pending == "commit")
        assert not app.state.stream_workers.active
        assert not server.server_state.tasks
        assert "data: [DONE]" not in lines
    finally:
        runtime.runs.release.set()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.parametrize("pending", ["generation", "commit"])
async def test_non_stream_shutdown_waits_for_finalization_before_closing_resources(live, pending):
    app, runtime, issued, base, server, serving = live
    entered, release = asyncio.Event(), asyncio.Event()
    complete = runtime.llm.complete

    async def delayed_generation(request):
        if request.messages[0].content.startswith("GENERATE"):
            entered.set()
            await release.wait()
        return await complete(request)

    if pending == "generation":
        runtime.llm.complete = delayed_generation
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + issued.access_token}, timeout=15) as client:
        reader = asyncio.create_task(client.post("/v1/chat/completions", json={**BODY, "stream": False}))
        try:
            await asyncio.wait_for((entered if pending == "generation" else runtime.runs.entered).wait(), 3)
            server.should_exit = True
            async with asyncio.timeout(8):
                await runtime.runs.entered.wait()
                while not app.state.stream_workers.closed:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert not app.state.test_resource_closed.is_set()
            assert not serving.done() and not runtime.runs.rows

            runtime.runs.release.set()
            await asyncio.wait_for(serving, 3)
            await asyncio.gather(reader, return_exceptions=True)
            expected_status = "cancelled" if pending == "generation" else "completed"
            assert [run.status for run in runtime.runs.calls] == [expected_status]
            run = next(iter(runtime.runs.rows.values()))
            assert run.status == expected_status
            assert bool(run.sources) is (pending == "commit")
            assert app.state.test_resource_closed.is_set()
            assert not server.server_state.tasks
        finally:
            release.set()
            runtime.runs.release.set()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
