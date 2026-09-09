from dataclasses import replace
from datetime import timedelta

import httpx
import openai
import pytest
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.auth import Authenticated

from apps.api.tests.auth_fakes import service

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "ministries,default,expected",
    [
        (("mi",), "mi", ["assistant-rh-mi"]),
        (("mi", "matte", "mi"), "matte", ["assistant-rh-matte", "assistant-rh-mi"]),
        (("mso", "masa", "matte", "mi"), "masa", ["assistant-rh-masa", "assistant-rh-matte", "assistant-rh-mi", "assistant-rh-mso"]),
    ],
)
async def test_openai_sdk_lists_group_models_with_strict_response_validation(ministries, default, expected):
    auth = service()
    auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], allowed_ministries=ministries, default_ministry=default)
    app = create_app(auth_service=auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        login = await http.post("/v1/auth/session", json={"slug": "beta", "password": "password"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        async with openai.AsyncOpenAI(
            api_key=token, base_url="http://test/v1", http_client=http, max_retries=0, _strict_response_validation=True
        ) as sdk:
            response = await sdk.models.with_raw_response.list()
            page = response.parse()
            assert page.object == "list"
            assert [model.id for model in page.data] == expected
            assert [model.id async for model in page] == expected
            assert not page.has_next_page()
            assert all(model.object == "model" and model.owned_by == "assistant-rh" and model.created == 1755734400 for model in page.data)
            assert response.headers["cache-control"] == "no-store"
            assert token not in response.text and "hash-password" not in response.text
            second = await sdk.models.list()
            assert second.model_dump() == page.model_dump()
    assert len(auth.passwords.calls) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic secret"},
        {"Authorization": "Bearer invalid"},
        [("Authorization", "Bearer one"), ("Authorization", "Bearer two")],
    ],
)
async def test_models_rejects_missing_malformed_and_duplicate_bearer(headers):
    auth = service()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth)), base_url="http://test") as client:
        response = await client.get("/v1/models", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}
    assert not auth.groups.lookups and not auth.sessions.lookups


@pytest.mark.parametrize("change", ["expired", "revoked", "policy_revision", "password", "unknown_token"])
async def test_sdk_rejects_unusable_sessions(change):
    auth = service()
    issued = await auth.login("beta", "password", "source")
    token = issued.access_token
    if change == "expired":
        auth.clock.value += timedelta(hours=8)
    elif change == "revoked":
        await auth.logout(issued.context)
    elif change == "policy_revision":
        auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], allowed_ministries=(), credential_revision=4)
    elif change == "password":
        auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], password_hash="new")
    else:
        token = auth.tokens.issue()
    async with openai.AsyncOpenAI(
        api_key=token,
        base_url="http://test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth))),
    ) as sdk:
        with pytest.raises(openai.AuthenticationError) as error:
            await sdk.models.list()
        assert error.value.code == "invalid_api_key"


@pytest.mark.parametrize("change", [{"allowed_ministries": ()}, {"default_ministry": ""}, {"default_ministry": "masa"}])
async def test_authenticated_corrupt_policy_is_safe_configuration_error(change):
    auth = service()
    issued = await auth.login("beta", "password", "source")
    # Simulate corrupt adapter data for an otherwise current session. Normal DB
    # policy updates increment the revision and invalidate the bearer (401).
    auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], **change)
    async with openai.AsyncOpenAI(
        api_key=issued.access_token,
        base_url="http://test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth))),
    ) as sdk:
        with pytest.raises(openai.InternalServerError) as error:
            await sdk.models.list()
        assert error.value.body == {"message": "Invalid group ministry configuration", "type": "server_error", "code": "ministry_configuration_error"}
        assert error.value.response.headers["cache-control"] == "no-store"


async def test_model_catalogues_are_isolated_between_groups_and_database_errors_are_normalized():
    auth = service()
    auth.groups.rows["other"] = replace(auth.groups.rows["beta"], slug="other", allowed_ministries=("masa",), default_ministry="masa")
    one = await auth.login("beta", "password", "a")
    two = await auth.login("other", "password", "b")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(auth_service=auth)), base_url="http://test") as client:
        cases = [(one, ["assistant-rh-matte", "assistant-rh-mi"]), (two, ["assistant-rh-masa"]), (one, ["assistant-rh-matte", "assistant-rh-mi"])]
        for issued, expected in cases:
            response = await client.get("/v1/models", headers={"Authorization": "Bearer " + issued.access_token})
            assert [model["id"] for model in response.json()["data"]] == expected

        async def unavailable(*args):
            raise DatabaseUnavailable()

        auth.groups.get = unavailable
        response = await client.get("/v1/models", headers={"Authorization": "Bearer " + one.access_token})
        assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"


async def test_future_chat_resolution_errors_are_sdk_compatible():
    auth = service()
    issued = await auth.login("beta", "password", "source")
    app = create_app(auth_service=auth)

    @app.get("/v1/test-resolve/{model}")
    async def resolve(model: str, context: Authenticated):
        return ModelService().resolve(model, context.group)

    async with openai.AsyncOpenAI(
        api_key=issued.access_token, base_url="http://test/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    ) as sdk:
        for model, error_type, code in [
            ("unknown", openai.NotFoundError, "model_not_found"),
            ("assistant-rh-masa", openai.PermissionDeniedError, "ministry_forbidden"),
        ]:
            with pytest.raises(error_type) as error:
                await sdk.get("/test-resolve/" + model, cast_to=dict)
            assert error.value.code == code
