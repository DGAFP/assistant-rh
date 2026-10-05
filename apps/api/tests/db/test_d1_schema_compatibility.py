"""B4 completions keep working before the local-only D1 schema is installed."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.feedback import FeedbackService
from assistant_rh_api.core.models.chat import ChatInput
from assistant_rh_api.core.models.conversations import FeedbackInput
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.auth import resolve_bearer

from apps.api.tests.auth_fakes import Clock, individual_context, service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.db.conftest import FEEDBACK_MIGRATION
from apps.api.tests.db.test_run_feedback_stores import NOW, make_run

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pre_d1(repository_db):
    async with repository_db.transaction() as connection:
        await connection.execute("""
            DROP TRIGGER api_run_author_immutable ON public.chat_runs;
            DROP TRIGGER api_feedback_individual_guard ON public.chat_feedbacks;
            ALTER TABLE public.chat_runs DROP COLUMN author_user_id;
            ALTER TABLE public.chat_feedbacks DROP COLUMN api_actor_user_id;
            ALTER TABLE public.chat_feedback_audit DROP COLUMN actor_user_id;
        """)
    try:
        yield repository_db
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute(FEEDBACK_MIGRATION.read_text())


@pytest.mark.parametrize("stream", [False, True])
async def test_b4_completion_and_sources_without_d1_schema(pre_d1, stream):
    repository_db = pre_d1
    auth = service()
    issued = await auth.login("beta", "password", "local")
    store = ChatRunStore(repository_db)
    runtime = Runtime(runs=store)
    app = create_app(auth_service=auth, chat_service=runtime.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + issued.access_token},
            json={"model": "assistant-rh", "messages": [{"role": "user", "content": "Question"}], "stream": stream},
        )
    assert response.status_code == 200
    if stream:
        assert "data: [DONE]" in response.text
        body = next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {") and '"id"' in line)
    else:
        body = response.json()
    run = await store.get(body["id"].removeprefix("chatcmpl-"))
    assert run is not None and run.status == "completed" and run.author_user_id is None
    assert run.sources
    assert await store.sources(run.turn_id, "beta", ministries=("matte",)) == run.sources


async def test_collective_feedback_without_d1_schema(pre_d1):
    run = make_run()
    await ChatRunStore(pre_d1).finalize(run)
    store = FeedbackStore(pre_d1)
    value = FeedbackInput(run.turn_id, 4, "Initial", ("Clair",), helpful=True)
    first = await store.save(value, run.group_slug, run.session_hash, NOW, ministries=("matte",))
    assert first is not None and await store.get(run.turn_id) == first
    assert await store.save_analysis(first.id, first.revision, "retrieval", "Old", NOW)
    edited = replace(value, stars=1, comment="Revised", reasons_positive=(), reasons_negative=("Confus",), helpful=False)
    results = await asyncio.gather(*(store.save(edited, run.group_slug, run.session_hash, NOW, ministries=("matte",)) for _ in range(3)))
    assert all(row == results[0] for row in results)
    assert results[0].value == edited and results[0].analysis_category is None
    async with pre_d1.transaction(read_only=True) as connection:
        assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (1,)
    assert await store.save(value, run.group_slug, run.session_hash, NOW, ministries=("mi",)) is None
    assert await store.get(run.turn_id) == results[0]


@pytest.mark.parametrize("stream", [False, True])
async def test_individual_requires_d1_before_any_pipeline_io(pre_d1, stream):
    runtime = Runtime(runs=ChatRunStore(pre_d1))
    context = individual_context()
    app = create_app(chat_service=runtime.service, feedback_service=FeedbackService(FeedbackStore(pre_d1), Clock()))
    app.dependency_overrides[resolve_bearer] = lambda: context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "assistant-rh", "messages": [{"role": "user", "content": "Question"}], "stream": stream},
        )
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/json"
        assert (await client.post("/v1/feedback", json={"completion_id": "run", "stars": 3, "comment": "Test"})).status_code == 503
        assert (await client.get("/v1/feedback/run")).status_code == 503
    with pytest.raises(DatabaseUnavailable):
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), context)
    assert not runtime.pipelines and not runtime.llm.calls and not runtime.search.calls and runtime.config.calls == 0
    async with pre_d1.transaction(read_only=True) as connection:
        assert await (await connection.execute("SELECT count(*) FROM public.chat_runs")).fetchone() == (0,)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE public.chat_feedbacks DROP COLUMN api_actor_user_id",
        "ALTER TABLE public.chat_runs DISABLE TRIGGER api_run_author_immutable",
    ],
)
async def test_partial_or_disabled_d1_fails_closed(repository_db, sql):
    async with repository_db.transaction() as connection:
        await connection.execute(sql)
    try:
        with pytest.raises(DatabaseUnavailable):
            await repository_db.individual_feedback_schema()
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute(FEEDBACK_MIGRATION.read_text())
