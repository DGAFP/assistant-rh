"""Conversations delegation (#596) through the real HTTP routes, without a user registry."""

import httpx
import openai
import pytest
from assistant_rh_api.core.auth import DELEGATED_PRINCIPAL
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import Signer, delegated_service, service
from apps.api.tests.chat_fakes import Runtime

pytestmark = pytest.mark.anyio
ALICE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
async def delegated():
    signer = Signer()
    auth = delegated_service(signer)
    runtime = Runtime()
    app = create_app(auth_service=auth, chat_service=runtime.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, signer, runtime, auth


def bearer(token):
    return {"Authorization": "Bearer " + token}


def chat(model="assistant-rh-matte"):
    return {"model": model, "messages": [{"role": "user", "content": "Question RH"}]}


async def test_openai_sdk_lists_only_delegated_models(delegated):
    client, signer, _, auth = delegated
    token = signer.sign(signer.claims(models=["assistant-rh-mi", "assistant-rh-masa"]))
    options = {"base_url": "http://test/v1", "http_client": client, "max_retries": 0, "_strict_response_validation": True}
    async with openai.AsyncOpenAI(api_key=token, **options) as sdk:
        page = await sdk.models.list()
    assert [model.id for model in page.data] == ["assistant-rh-masa", "assistant-rh-mi"]
    # No registry, group or session lookup: rights are exactly those delegated.
    assert auth.groups.lookups == [] and auth.sessions.lookups == []


async def test_user_without_grant_sees_no_model_and_reaches_no_corpus(delegated):
    client, signer, runtime, _ = delegated
    listed = await client.get("/v1/models", headers=bearer(signer.sign(signer.claims(models=[]))))
    assert listed.status_code == 200 and listed.json()["data"] == []
    for model in ("assistant-rh", "assistant-rh-matte"):
        response = await client.post("/v1/chat/completions", json=chat(model), headers=bearer(signer.sign(signer.claims(models=[]))))
        assert response.status_code == 403 and response.json()["error"]["code"] == "ministry_forbidden"
    assert runtime.runs.calls == [] and runtime.llm.calls == []


@pytest.mark.parametrize("model", ["assistant-rh-masa", "assistant-rh-mi", "assistant-rh"])
async def test_model_name_outside_conversation_scope_is_forbidden(delegated, model):
    client, signer, runtime, _ = delegated
    # mi is granted, but this conversation was created for matte and stays fixed.
    token = signer.sign(signer.claims(ministry="matte")) if model != "assistant-rh" else signer.sign()
    response = await client.post("/v1/chat/completions", json=chat(model), headers=bearer(token))
    assert response.status_code == 403 and response.json()["error"]["code"] == "ministry_forbidden"
    assert runtime.runs.calls == []


async def test_two_users_get_distinct_authors_that_the_client_cannot_choose(delegated):
    client, signer, runtime, _ = delegated
    authors = []
    for user in (ALICE, BOB):
        token = signer.sign(signer.claims(user, ministry="matte"))
        # Client-supplied identity fields are ignored: only the signed subject names the author.
        body = {**chat("assistant-rh"), "user": BOB, "metadata": {"author_user_id": BOB, "user_id": BOB}}
        response = await client.post("/v1/chat/completions", json=body, headers=bearer(token))
        assert response.status_code == 200
        run = runtime.runs.rows[response.json()["id"].removeprefix("chatcmpl-")]
        authors.append(str(run.author_user_id))
        assert run.group_slug == DELEGATED_PRINCIPAL and run.selected_ministry == "matte"
        assert run.session_hash == "e" * 64
    assert authors == [ALICE, BOB]


async def test_withdrawn_grant_applies_to_the_next_request(delegated):
    client, signer, _, _ = delegated
    granted = await client.get("/v1/models", headers=bearer(signer.sign()))
    withdrawn = await client.get("/v1/models", headers=bearer(signer.sign(signer.claims(models=["assistant-rh-mi"]))))
    assert [m["id"] for m in granted.json()["data"]] == ["assistant-rh-matte", "assistant-rh-mi"]
    assert [m["id"] for m in withdrawn.json()["data"]] == ["assistant-rh-mi"]


async def test_calls_not_signed_by_the_pinned_backend_are_401(delegated):
    client, signer, runtime, _ = delegated
    stranger = Signer(signer.kid)
    for headers in (
        {},
        {"X-User-Id": ALICE},  # a declared identity is never an authentication
        bearer(stranger.sign()),
        bearer(signer.sign(signer.claims(exp=signer.claims()["iat"] - 1))),
        bearer(signer.sign(signer.claims(aud="another-api"))),
    ):
        for method, path, body in (("GET", "/v1/models", None), ("POST", "/v1/chat/completions", chat())):
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key"
            assert response.headers["www-authenticate"] == "Bearer"
    assert runtime.runs.calls == []


async def test_delegation_disabled_by_default_and_never_a_group_session(delegated):
    client, signer, _, _ = delegated
    token = signer.sign()
    # B4 session routes do not accept a delegation, and logout revokes nothing.
    assert (await client.get("/v1/auth/me", headers=bearer(token))).status_code == 401
    assert (await client.delete("/v1/auth/session", headers=bearer(token))).status_code == 401
    app = create_app(auth_service=service())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        assert (await other.get("/v1/models", headers=bearer(token))).status_code == 401


async def test_group_sessions_keep_working_next_to_delegations(delegated):
    client, signer, _, auth = delegated
    login = await client.post("/v1/auth/session", json={"slug": "beta", "password": "password"})
    group_token = login.json()["access_token"]
    me = await client.get("/v1/auth/me", headers=bearer(group_token))
    assert me.status_code == 200 and me.json()["group"]["slug"] == "beta"
    response = await client.post("/v1/chat/completions", json=chat("assistant-rh"), headers=bearer(group_token))
    assert response.status_code == 200 and response.json()["model"] == "assistant-rh-matte"
    assert signer.verifier().verify(group_token, auth.clock.now()) is None


async def test_each_signed_assertion_authenticates_a_single_request(delegated):
    client, signer, runtime, auth = delegated
    token = signer.sign()
    assert (await client.get("/v1/models", headers=bearer(token))).status_code == 200
    # Replayed, even for another route: refused before any corpus or LLM work.
    assert (await client.post("/v1/chat/completions", json=chat(), headers=bearer(token))).status_code == 401
    assert (await client.get("/v1/models", headers=bearer(token))).status_code == 401
    assert runtime.runs.calls == [] and len(auth.replays.claims) == 1
    # Unverified assertions are never recorded.
    assert (await client.get("/v1/models", headers=bearer(Signer(signer.kid).sign()))).status_code == 401
    assert len(auth.replays.claims) == 1
