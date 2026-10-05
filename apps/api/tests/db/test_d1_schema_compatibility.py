"""B4 completions keep working before the local-only D1 schema is installed."""

import json

import httpx
import pytest
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.db.conftest import FEEDBACK_MIGRATION

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("stream", [False, True])
async def test_b4_completion_and_sources_without_d1_schema(repository_db, stream):
    async with repository_db.transaction() as connection:
        await connection.execute("""
            DROP TRIGGER api_run_author_immutable ON public.chat_runs;
            DROP TRIGGER api_feedback_individual_guard ON public.chat_feedbacks;
            ALTER TABLE public.chat_runs DROP COLUMN author_user_id;
            ALTER TABLE public.chat_feedbacks DROP COLUMN api_actor_user_id;
            ALTER TABLE public.chat_feedback_audit DROP COLUMN actor_user_id;
        """)
    try:
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
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute(FEEDBACK_MIGRATION.read_text())
