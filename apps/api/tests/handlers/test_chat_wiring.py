"""The HTTP boundary and production chat composition share model routing."""

from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime


@pytest.mark.anyio
@pytest.mark.parametrize("model", ["test-alias", "assistant-rh"])
async def test_injected_model_policy_is_used_for_execution(monkeypatch, model):
    class Models(ModelService):
        def resolve(self, model, group):
            return super().resolve("assistant-rh-mi" if model in ("test-alias", "assistant-rh") else model, group)

    async def idle_cleanup(*args):
        await anyio.sleep_forever()

    monkeypatch.setattr("assistant_rh_api.handlers.app.maintain_sessions", idle_cleanup)
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    app = create_app(
        database=AsyncMock(),
        environ={},
        auth_service=auth,
        model_service=Models(),
        rag_configuration_service=RAGConfigurationService(runtime.config),
    )
    async with app.router.lifespan_context(app):
        # Keep the service built by the production factory; replace only I/O.
        app.state.chat_service._pipeline_factory = runtime.service._pipeline_factory
        app.state.chat_service._runs = runtime.runs
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + issued.access_token},
                json={"model": model, "messages": [{"role": "user", "content": "Question RH"}]},
            )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "assistant-rh-mi"
    run = next(iter(runtime.runs.rows.values()))
    assert run.selected_ministry == "mi"
    assert {request.source for request in runtime.search.calls} == {"mi", "service_public", "dgafp"}
