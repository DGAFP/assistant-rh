import json

import httpx
import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable, LoginRateLimited
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.auth import Authenticated

from apps.api.tests.auth_fakes import service

pytestmark = pytest.mark.anyio


async def test_public_http_login_me_scope_logout_contract():
    auth = service()
    app = create_app(auth_service=auth)

    @app.get("/v1/test-ministry/{ministry}")
    async def scoped(ministry: str, context: Authenticated):
        return {"ministry": context.authorize_ministry(ministry)}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        groups = await client.get("/v1/auth/groups")
        assert groups.json() == {"data": [{"slug": "beta", "label": "Beta", "icon": "🏛️", "color": "#0053b3"}]}
        login = await client.post("/v1/auth/session", json={"slug": "beta", "password": "password"})
        assert login.status_code == 200 and login.headers["cache-control"] == "no-store"
        body = login.json()
        assert body["expires_in"] == 28800 and body["token_type"] == "bearer"
        assert body["group"] == {"slug": "beta", "allowed_ministries": ["matte", "mi"], "default_ministry": "matte", "credential_revision": 3}
        headers = {"authorization": "Bearer " + body["access_token"]}
        me = await client.get("/v1/auth/me", headers=headers)
        assert me.status_code == 200 and me.json()["group"] == body["group"]
        assert body["access_token"] not in me.text and "password" not in me.text
        assert (await client.get("/v1/test-ministry/mi", headers=headers)).status_code == 200
        denied = await client.get("/v1/test-ministry/masa", headers=headers)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "ministry_forbidden"
        logout = await client.delete("/v1/auth/session", headers=headers)
        assert logout.status_code == 204 and logout.content == b""
        assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401
        assert (await client.delete("/v1/auth/session", headers=headers)).status_code == 401
        assert (await client.get("/admin/groups", headers=headers)).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Basic secret"}, {"authorization": "Bearer invalid"}, [("authorization", "Bearer one"), ("authorization", "Bearer two")]],
)
async def test_common_bearer_dependency_rejects_missing_invalid_and_duplicate_headers(headers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=service())), base_url="http://test") as client:
        for method, path in (("GET", "/v1/auth/me"), ("DELETE", "/v1/auth/session")):
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
            assert response.json() == {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}


async def test_failed_login_has_uniform_errors_and_ignores_forwarded_addresses():
    auth = service()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(auth_service=auth), client=("192.0.2.10", 123)), base_url="http://test"
    ) as client:
        responses = []
        for slug, password in (("missing", "password"), ("beta", "wrong")):
            responses.append(await client.post("/v1/auth/session", json={"slug": slug, "password": password}, headers={"X-Forwarded-For": "1.2.3.4"}))
        assert responses[0].status_code == responses[1].status_code == 401
        assert responses[0].json() == responses[1].json()
        assert auth.limiter.calls == [("192.0.2.10", "missing"), ("192.0.2.10", "beta")]


async def test_validation_never_echoes_password_or_unknown_fields():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=service())), base_url="http://test") as client:
        for body in (
            {"slug": "bad slug", "password": "never-echo-this"},
            {"slug": "beta", "password": ["never-echo-this"]},
            {"slug": "beta", "password": "never-echo-this", "extra": "never-echo-this"},
        ):
            response = await client.post("/v1/auth/session", json=body)
            assert response.status_code == 422 and "never-echo-this" not in response.text
            assert "input" not in response.text


@pytest.mark.parametrize("chunked", [False, True])
async def test_login_body_limit_before_service_or_json_parsing(chunked):
    auth = service()
    payload = json.dumps({"slug": "beta", "password": "s" * 17000}).encode()

    async def chunks():
        yield payload[:100]
        yield payload[100:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth)), base_url="http://test") as client:
        response = await client.post("/v1/auth/session", content=chunks() if chunked else payload)
    assert response.status_code == 413 and not auth.limiter.calls


async def test_rate_limit_and_database_failure_are_safe_http_errors():
    auth = service()

    async def limited(*args):
        raise LoginRateLimited(12)

    auth.limiter.acquire = limited
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth)), base_url="http://test") as client:
        response = await client.post("/v1/auth/session", json={"slug": "beta", "password": "password"})
        assert response.status_code == 429 and response.headers["retry-after"] == "12"

    async def unavailable(*args):
        raise DatabaseUnavailable()

    auth.groups.list_groups = unavailable
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth)), base_url="http://test") as client:
        response = await client.get("/v1/auth/groups")
        assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"


async def test_auth_without_lifespan_or_store_fails_closed():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        assert (await client.get("/v1/auth/groups")).status_code == 503


@pytest.mark.parametrize("body,status", [(b'{"slug":"beta","password":"\\ud800"}', 422), (b'{"password":"\xff"}', 400)])
async def test_malformed_unicode_is_safe_transport_error(body, status):
    auth = service()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth)), base_url="http://test") as client:
        response = await client.post("/v1/auth/session", content=body, headers={"content-type": "application/json"})
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"message": "Invalid request", "type": "invalid_request_error", "code": "invalid_request"}}
    assert not auth.passwords.calls


def test_canonical_server_entrypoint_disables_proxy_header_rewriting(monkeypatch):
    import uvicorn
    from assistant_rh_api.__main__ import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    main()
    assert calls[0]["proxy_headers"] is False
