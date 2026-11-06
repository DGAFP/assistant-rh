import logging
import re

import httpx
import pytest
from assistant_rh_api.handlers.app import create_app
from starlette.responses import StreamingResponse


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_correlation_survives_json_and_stream_without_logging_secrets(caplog, stream):
    app = create_app(environ={})

    @app.get("/correlation-test")
    async def answer():
        if stream:
            return StreamingResponse(iter([b"data: hello\n\n", b"data: [DONE]\n\n"]), media_type="text/event-stream")
        return {"ok": True}

    request_id = "a" * 32
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/correlation-test", headers={"X-Client-Request-Id": request_id, "Authorization": "Bearer SECRET"})
    assert response.status_code == 200 and response.headers["X-Request-Id"] == request_id
    assert f"request_id={request_id} status=200" in caplog.text
    assert "SECRET" not in caplog.text
    if stream:
        assert response.content.endswith(b"data: [DONE]\n\n")


@pytest.mark.anyio
@pytest.mark.parametrize("client_request_id", ["a" * 32, "unsafe value"])
async def test_unexpected_error_keeps_correlation_without_logging_secrets(caplog, client_request_id):
    app = create_app(environ={})

    @app.get("/correlation-error")
    async def fail():
        raise RuntimeError("SECRET exception details")

    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.get("/correlation-error", headers={"X-Client-Request-Id": client_request_id, "Authorization": "Bearer SECRET"})
    assert response.status_code == 500
    assert response.json() == {"error": {"message": "Internal server error", "type": "server_error", "code": "internal_error"}}
    assert response.headers["Cache-Control"] == "no-store"
    request_id = response.headers["X-Request-Id"]
    assert re.fullmatch("[0-9a-f]{32}", request_id)
    if client_request_id == "a" * 32:
        assert request_id == client_request_id
    assert caplog.text.count(f"api_request request_id={request_id} status=500") == 1
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize(
    "headers", [{}, {"X-Client-Request-Id": "unsafe value"}, [("X-Client-Request-Id", "a" * 32), ("X-Client-Request-Id", "b" * 32)]]
)
@pytest.mark.anyio
async def test_untrusted_or_ambiguous_correlation_gets_a_fresh_identifier(headers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(environ={})), base_url="http://test") as client:
        response = await client.get("/missing", headers=headers)
    assert re.fullmatch("[0-9a-f]{32}", response.headers["X-Request-Id"])
    assert response.headers["X-Request-Id"] not in ("a" * 32, "b" * 32)
