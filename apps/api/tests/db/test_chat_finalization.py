"""Real C6 service + PostgreSQL atomicity, with only inference/search ports synthetic."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from assistant_rh_api.core.errors import ApplicationError
from assistant_rh_api.core.models.chat import Cancellation, ChatInput
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime

pytestmark = pytest.mark.anyio


async def test_real_service_persists_entire_run_and_sources_before_http_success(repository_db):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    store = ChatRunStore(repository_db)
    runtime = Runtime(runs=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(auth_service=auth, chat_service=runtime.service)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + issued.access_token},
            json={"model": "assistant-rh", "messages": [{"role": "user", "content": "Question"}]},
        )
    assert response.status_code == 200
    body = response.json()
    run = await store.get(body["x_assistant_rh"]["turn_id"])
    assert run.status == "completed" and run.answer == body["choices"][0]["message"]["content"]
    assert len(run.events) == 7 and len(run.sources) == 1
    assert run.sources[0].publisher == "MATTE" and run.sources[0].access == "authenticated"
    assert await store.sources(run.turn_id, "beta") == run.sources
    assert await store.sources(run.turn_id, "other") == ()
    async with repository_db.transaction(read_only=True) as connection:
        count = await (await connection.execute("SELECT count(*) FROM public.rag_trace_events WHERE turn_id = %s", (run.turn_id,))).fetchone()
        assert count[0] == 7


async def test_source_insert_failure_rolls_back_completed_run_then_persists_failed(repository_db):
    store = ChatRunStore(repository_db)

    class BrokenFinalizer:
        async def finalize(self, run):
            if run.status == "completed":
                # Violate the real source uniqueness constraint after the parent
                # and first source have been inserted. Production code is unchanged.
                await store.finalize(replace(run, sources=run.sources * 2))
            else:
                await store.finalize(run)

    runtime = Runtime(runs=BrokenFinalizer())
    auth = (await service().login("beta", "password", "local")).context
    with pytest.raises(ApplicationError):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth)
    async with repository_db.transaction(read_only=True) as connection:
        row = await (await connection.execute("SELECT turn_id, api_record->>'status' FROM public.chat_runs")).fetchone()
        assert row[1] == "failed"
        assert await (await connection.execute("SELECT count(*) FROM public.chat_run_sources")).fetchone() == (0,)
    run = await store.get(row[0])
    assert run.status == "failed" and not run.answer and not run.sources and len(run.events) == 7


async def test_cancelled_run_is_durable_without_document_authority(repository_db):
    store = ChatRunStore(repository_db)
    runtime = Runtime(runs=store)
    cancellation = Cancellation()
    cancellation.cancel()
    auth = (await service().login("beta", "password", "local")).context
    with pytest.raises(asyncio.CancelledError):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth, cancellation=cancellation)
    async with repository_db.transaction(read_only=True) as connection:
        row = await (await connection.execute("SELECT turn_id FROM public.chat_runs")).fetchone()
    run = await store.get(row[0])
    assert run.status == "cancelled" and run.diagnostics["partial"] is False and not run.sources


async def test_store_refuses_source_authority_on_failed_runs(repository_db):
    store = ChatRunStore(repository_db)
    runtime = Runtime()
    auth = (await service().login("beta", "password", "local")).context
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth)
    with pytest.raises(ValueError, match="source authority"):
        await store.finalize(replace(run, status="failed"))
    assert await store.get(run.turn_id) is None
