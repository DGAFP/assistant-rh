"""SSE terminal and disconnect guarantees against an actual local transaction."""

import asyncio
import json
from dataclasses import replace

import pytest
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_stream import StreamSettings

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.handlers.test_chat_stream import Exchange, StreamLLM

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("mode", ["success", "disconnect", "rollback"])
async def test_stream_persistence_is_atomic_and_precedes_terminal(repository_db, mode):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    store = ChatRunStore(repository_db)

    class Store:
        async def finalize(self, run):
            if mode == "rollback" and run.status == "completed":
                run = replace(run, sources=run.sources * 2)
            await store.finalize(run)

    runtime = Runtime(runs=Store())
    runtime.llm = StreamLLM()
    if mode == "disconnect":
        runtime.llm.stream_release = asyncio.Event()
    app = create_app(auth_service=auth, chat_service=runtime.service, stream_settings=StreamSettings(ping_seconds=0.01))
    committed = []

    async def check_before_send(message):
        body = message.get("body", b"")
        if b'"finish_reason":"stop"' in body:
            payload = json.loads(body.removeprefix(b"data: "))
            run = await store.get(payload["x_assistant_rh"]["turn_id"])
            assert run.status == "completed" and len(run.events) == 7 and len(run.sources) == 1
            committed.append(run)
        if b"[DONE]" in body:
            assert committed

    exchange = Exchange(app, issued, send_hook=check_before_send)
    if mode == "disconnect":
        await exchange.until(b'"content":"R')
        await exchange.disconnect()
    else:
        await exchange.finish()
    async with repository_db.transaction(read_only=True) as connection:
        rows = await (await connection.execute("SELECT turn_id FROM public.chat_runs")).fetchall()
    assert len(rows) == 1
    run = await store.get(rows[0][0])
    assert run.status == {"success": "completed", "disconnect": "cancelled", "rollback": "failed"}[mode]
    assert run.answer
    assert (b"[DONE]" in exchange.body) is (mode == "success")
    assert await store.sources(run.turn_id, "other") == ()
    assert bool(await store.sources(run.turn_id, "beta")) is (mode == "success")
    assert not app.state.stream_workers.active
