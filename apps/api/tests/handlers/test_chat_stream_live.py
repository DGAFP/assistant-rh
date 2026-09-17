"""Real TCP proof: no ASGITransport buffering, fake I/O ports, real API pipeline."""

import asyncio
import socket

import httpx
import openai
import pytest
import uvicorn
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_stream import StreamSettings

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.handlers.test_chat_stream import BODY, StreamLLM

pytestmark = pytest.mark.anyio


@pytest.fixture
async def live():
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    runtime.llm = StreamLLM()
    app = create_app(auth_service=auth, chat_service=runtime.service, environ={}, stream_settings=StreamSettings(ping_seconds=0.01))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield app, runtime, issued, "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def test_sdk_consumes_live_stream_and_post_header_error(live):
    app, runtime, issued, base = live
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
    app, runtime, issued, base = live
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
