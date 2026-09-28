"""SSE terminal and disconnect guarantees against an actual local transaction."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_stream import StreamSettings

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.db.test_network_failure import proxied_database
from apps.api.tests.handlers.test_chat_stream import BODY, Exchange, StreamLLM

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


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_lost_commit_response_releases_requests_without_retry(repository_db, repository_dsn, streaming, shutdown, caplog):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    async with proxied_database(repository_dsn, timeout_seconds=0.5) as (database, proxy):
        transaction = database.transaction
        writes = []

        @asynccontextmanager
        async def lose_commit_response():
            async with transaction() as connection:
                yield connection
                # Lose only COMMIT's reply, after all real INSERTs completed.
                proxy.drop_responses = True

        database.transaction = lose_commit_response
        store = ChatRunStore(database)

        class Store:
            async def finalize(self, run):
                writes.append(run)
                await store.finalize(run)

        runtime = Runtime(runs=Store())
        runtime.llm = StreamLLM()
        runtime.service._finalization_timeout = 0.25
        app = create_app(auth_service=auth, chat_service=runtime.service, environ={}, stream_settings=StreamSettings(ping_seconds=0.01))
        exchange = Exchange(app, issued, {**BODY, "stream": streaming})
        closing = None
        try:
            await asyncio.wait_for(proxy.response_dropped.wait(), 2)
            # Observe the committed result through an independent connection.
            saved = await ChatRunStore(repository_db).get(writes[0].turn_id)
            assert saved.status == "completed" and saved.sources and saved.events
            if shutdown:
                closing = asyncio.gather(app.state.stream_workers.aclose(), app.state.non_stream_requests.aclose())
                done, _ = await asyncio.wait({closing}, timeout=1)
                assert closing in done, "shutdown must not wait indefinitely for the missing COMMIT reply"
                await closing
            await exchange.finish()
            assert [run.status for run in writes] == ["completed"]
            assert "finalization timed out" in caplog.text
            assert not app.state.stream_workers.active and not app.state.non_stream_requests.active
            assert b"[DONE]" not in exchange.body and b'"finish_reason":"stop"' not in exchange.body
            if streaming:
                if not shutdown:
                    assert b'"code":"stream_error"' in exchange.body
            else:
                assert exchange.messages[0]["status"] == 500
            assert database.statistics()["returns_bad"] == 1
            proxy.drop_responses = False
            database.transaction = transaction
            assert (await store.get(saved.turn_id)).status == "completed"
        finally:
            proxy.drop_responses = False
            await proxy.close()
            await asyncio.gather(exchange.task, return_exceptions=True)
            if closing is not None:
                await closing
