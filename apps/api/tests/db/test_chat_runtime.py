"""HTTP → production composition → real DB adapters; provider wire only is fake."""

import json

import httpx
import pytest
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.run_store import ChatRunStore, json_data
from assistant_rh_api.db.settings_stores import ConfigStore
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_runtime import create_chat_service
from psycopg.types.json import Jsonb

from apps.api.tests.auth_fakes import service

pytestmark = pytest.mark.anyio
DOC = "00000000-0000-0000-0000-000000000463"
SECTION = "00000000-0000-0000-0000-000000001463"


@pytest.mark.parametrize("fallback", [False, True])
async def test_real_runtime_wiring_nonstream_and_provider_fallback(repository_db, fallback):
    async with repository_db.transaction() as connection:
        await connection.execute(
            "UPDATE public.rag_config SET config = %s WHERE id = 1",
            (
                Jsonb(
                    {
                        "enable_intent_gating": True,
                        "v3_search_mode": "lexical",
                        "v3_enable_selector": True,
                        "v3_selector_model": "synthetic-selector",
                        "v3_generator_model": "synthetic-generator",
                    }
                ),
            ),
        )
        await connection.execute(
            "INSERT INTO public.rag_documents VALUES (%s, 'guide', 'Guide interne', %s, 'MATTE', 'Congés synthétiques.', 20)",
            (DOC, "https://storage.invalid/file?X-Amz-Signature=SECRET"),
        )
        await connection.execute(
            "INSERT INTO public.rag_sections VALUES (%s, %s, 'Congés', 'Guide > Congés', 'Congés synthétiques.', NULL)", (SECTION, DOC)
        )
        await connection.execute(
            "INSERT INTO public.rag_chunks_matte (hash_id, chunk_text, section_id) VALUES ('c6-chunk', 'Congés annuels', %s)", (SECTION,)
        )
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        calls.append((request.url.host, request.url.path, payload))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [1.0] + [0.0] * 1023}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 0.9} for i in range(len(payload["documents"]))]})
        if payload["model"] == "openweight-medium":
            answer = '{"intent":"rag_query", "confidence": 0.9}'
        elif payload["model"] == "synthetic-selector":
            answer = '{"selected_ids":[0]}'
        else:
            if fallback and request.url.host == "albert.invalid":
                return httpx.Response(503, json={"error": "private provider detail"})
            answer = "Réponse synthétique selon le guide."
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
            },
        )

    environment = {"ALBERT_BASE_URL": "https://albert.invalid/v1", "ALBERT_API_KEY": "synthetic-key"}
    if fallback:
        environment.update({"SCALEWAY_BASE_URL": "https://scaleway.invalid/v1", "SCALEWAY_API_KEY": "synthetic-fallback"})
    auth = service()
    issued = await auth.login("beta", "password", "local")
    configurations = RAGConfigurationService(ConfigStore(repository_db))
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False) as providers:
        chat = create_chat_service(repository_db, configurations, providers, environment)
        app = create_app(auth_service=auth, chat_service=chat)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "assistant-rh", "messages": [{"role": "user", "content": "Congés"}]},
                headers={"Authorization": "Bearer " + issued.access_token},
            )
    assert response.status_code == 200, response.text
    body = response.json()
    run = await ChatRunStore(repository_db).get(body["x_assistant_rh"]["turn_id"])
    assert run.status == "completed" and run.sources[0].document_id == DOC
    assert body["x_assistant_rh"]["sources"][0]["url"] is None and "SECRET" not in response.text
    assert "X-Amz-Signature" not in json.dumps(json_data(run))
    assert run.events[-1].output_ref["diagnostics"]["outcome"]["provider"] == ("scaleway" if fallback else "albert")
    assert all(payload.get("stream") is False for _, path, payload in calls if path.endswith("/chat/completions"))
    assert any(path.endswith("/embeddings") for _, path, _ in calls)
    assert any(path.endswith("/rerank") for _, path, _ in calls)
    generation_calls = [payload for _, _, payload in calls if payload.get("model") in ("synthetic-generator", "llama-3.1-70b-instruct")]
    assert all("Congés synthétiques." in payload["messages"][-1]["content"] for payload in generation_calls)


async def test_app_lifespan_owns_real_chat_service_and_closes_resources(repository_dsn):
    app = create_app(environ={"SCW_POSTGRES_DSN": repository_dsn})
    assert app.state.chat_service is None
    async with app.router.lifespan_context(app):
        assert app.state.chat_service is not None
        assert app.state.chat_service._runs._database is app.state.database
    assert app.state.chat_service is None
