import asyncio
import logging

import httpx
import pytest
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.observability import Observation, observe, observing
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_stream import StreamSettings
from assistant_rh_api.telemetry import Telemetry
from prometheus_client import generate_latest
from prometheus_client import start_http_server as real_metrics_server

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from apps.api.tests.handlers.test_chat_stream import BODY, StreamLLM

pytestmark = pytest.mark.anyio


async def test_metrics_listener_is_separate_and_owned_by_lifespan(monkeypatch):
    servers = []

    def start(port, **kwargs):
        result = real_metrics_server(port, addr="127.0.0.1", **kwargs)
        servers.append(result)
        return result

    monkeypatch.setattr("assistant_rh_api.handlers.app.start_http_server", start)
    app = create_app(health_probe=object(), environ={"API_METRICS_PORT": "0"})
    async with app.router.lifespan_context(app):
        server, thread = servers[0]
        async with httpx.AsyncClient(trust_env=False) as client:
            metrics = await client.get(f"http://127.0.0.1:{server.server_port}/metrics")
        assert metrics.status_code == 200 and "assistant_rh_sse_capacity" in metrics.text
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/metrics")).status_code == 404
    assert not thread.is_alive()


async def test_finished_sse_error_is_not_http_200_success(caplog):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    runtime.llm = StreamLLM()
    app = create_app(auth_service=auth, chat_service=runtime.service, environ={})
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + issued.access_token}
    ) as client:
        success = await client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers={"X-Client-Request-Id": "a" * 32})
        assert success.content.endswith(b"data: [DONE]\n\n")
        runtime.llm.stream_failure = InferenceFailure((), partial=True)
        failed = await client.post("/v1/chat/completions", json={**BODY, "stream": True})
        assert failed.status_code == 200 and b"stream_error" in failed.content
    registry = app.state.telemetry.registry
    assert registry.get_sample_value("assistant_rh_sse_streams_total", {"outcome": "completed"}) == 1
    assert registry.get_sample_value("assistant_rh_sse_streams_total", {"outcome": "failed"}) == 1
    assert (
        registry.get_sample_value(
            "assistant_rh_http_requests_total", {"route": "/v1/chat/completions", "method": "POST", "status": "200", "outcome": "failed"}
        )
        == 1
    )
    assert registry.get_sample_value("assistant_rh_sse_first_text_seconds_count") == 2
    assert "api_diagnostic request_id=" + "a" * 32 + " kind=stage name=configuration" in caplog.text
    assert BODY["messages"][0]["content"] not in caplog.text
    assert issued.access_token not in caplog.text


async def test_admission_rejection_is_counted_before_a_run_exists():
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    app = create_app(auth_service=auth, chat_service=runtime.service, environ={}, stream_settings=StreamSettings(workers=1))
    app.state.stream_workers.closed = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions", json={**BODY, "stream": True}, headers={"Authorization": "Bearer " + issued.access_token}
        )
    assert response.status_code == 503 and not runtime.runs.rows
    assert app.state.telemetry.registry.get_sample_value("assistant_rh_sse_streams_total", {"outcome": "rejected"}) == 1
    assert app.state.telemetry.registry.get_sample_value("assistant_rh_http_active", {"route": "/v1/chat/completions"}) == 0


async def test_observers_are_request_scoped_and_cannot_break_execution(caplog):
    app = create_app(environ={})
    entered = asyncio.Event()
    release = asyncio.Event()

    @app.get("/synthetic")
    async def diagnostic():
        entered.set()
        await release.wait()
        observe(Observation("provider", "SECRET_operation", "SECRET_failure", provider="SECRET_provider"))
        return {"ok": True}

    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.get("/synthetic", headers={"X-Client-Request-Id": "a" * 32}))
        await entered.wait()
        second = asyncio.create_task(client.get("/synthetic", headers={"X-Client-Request-Id": "b" * 32}))
        release.set()
        await asyncio.gather(first, second)
    assert "SECRET_" not in caplog.text
    for identifier in ("a" * 32, "b" * 32):
        assert "api_diagnostic request_id=" + identifier + " kind=provider name=other provider=other outcome=error" in caplog.text
    before = generate_latest(app.state.telemetry.registry)
    observe(Observation("provider", "chat", "timeout", provider="albert"))
    assert generate_latest(app.state.telemetry.registry) == before
    with observing(lambda _: (_ for _ in ()).throw(RuntimeError("observer unavailable"))):
        observe(Observation("stage", "configuration", "ok"))


async def test_registries_are_independent_and_user_paths_are_not_labels():
    app = create_app(environ={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/private/SECRET_ID?token=SECRET_TOKEN")
    text = generate_latest(app.state.telemetry.registry).decode()
    assert "SECRET" not in text
    assert 'route="other"' in text
    assert (
        Telemetry().registry.get_sample_value(
            "assistant_rh_http_requests_total", {"route": "other", "method": "GET", "status": "404", "outcome": "completed"}
        )
        is None
    )
