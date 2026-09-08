import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest
from assistant_rh_api.core.auth import InvalidCredentials, LoginRateLimited, MinistryForbidden
from assistant_rh_api.core.errors import DatabaseConflict

from apps.api.tests.auth_fakes import service

pytestmark = pytest.mark.anyio


async def test_login_resolve_scope_and_logout_keep_secrets_out_of_outcomes():
    auth = service()
    issued = await auth.login("beta", "password", "client")
    session = issued.context.session
    assert session.expires_at - session.created_at == timedelta(hours=8)
    assert issued.access_token not in repr(issued)
    assert "hash-password" not in repr(issued)
    assert issued.access_token not in auth.sessions.rows
    assert len(session.token_hash) == 64
    context = await auth.resolve(issued.access_token)
    assert context.authorize_ministry() == "matte"
    assert context.authorize_ministry("mi") == "mi"
    for ministry in ("masa", "unknown", "", "default"):
        with pytest.raises(MinistryForbidden):
            context.authorize_ministry(ministry)
    assert auth.groups.lookups == ["beta", "beta"]
    assert len(auth.passwords.calls) == 1  # Bearer resolution never invokes PBKDF2.
    with pytest.raises(FrozenInstanceError):
        context.group = None
    await auth.logout(context)
    with pytest.raises(InvalidCredentials):
        await auth.resolve(issued.access_token)


@pytest.mark.parametrize(
    "change",
    [
        {"slug": "default"},
        {"visible": False},
        {"is_admin": True},
        {"password_hash": None},
        {"password_hash": ""},
        {"allowed_ministries": ()},
        {"allowed_ministries": ("unknown",)},
        {"default_ministry": "masa"},
    ],
)
async def test_ineligible_groups_are_hidden_and_cannot_login(change):
    auth = service()
    auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], **change)
    assert await auth.list_groups() == ()
    with pytest.raises(InvalidCredentials):
        await auth.login("beta", "password", "client")
    assert auth.passwords.calls == [("password", None)]  # Same dummy work as absent group.
    assert not auth.sessions.rows


async def test_missing_and_wrong_password_have_same_error_and_one_check():
    auth = service()
    for slug, password in (("missing", "password"), ("beta", "wrong")):
        with pytest.raises(InvalidCredentials, match="invalid_api_key"):
            await auth.login(slug, password, "source")
    assert len(auth.passwords.calls) == 2
    assert not auth.sessions.rows


async def test_catalog_priority_then_slug_with_display_metadata():
    auth = service()
    group = auth.groups.rows["beta"]
    auth.groups.rows.update({g.slug: g for g in (replace(group, slug="aaa"), replace(group, slug="zzz", priority=0))})
    assert [g.slug for g in await auth.list_groups()] == ["zzz", "aaa", "beta"]
    assert (await auth.list_groups())[0].icon == "🏛️"


@pytest.mark.parametrize("delta", [timedelta(hours=8), timedelta(hours=9), timedelta(seconds=-1)])
async def test_session_expiry_and_future_creation_rejected(delta):
    auth = service()
    issued = await auth.login("beta", "password", "source")
    auth.clock.value += delta
    with pytest.raises(InvalidCredentials):
        await auth.resolve(issued.access_token)


@pytest.mark.parametrize("change", [{"credential_revision": 5}, {"visible": False}, {"is_admin": True}, {"password_hash": "new"}])
async def test_group_change_invalidates_existing_session(change):
    auth = service()
    issued = await auth.login("beta", "password", "source")
    auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], **change)
    with pytest.raises(InvalidCredentials):
        await auth.resolve(issued.access_token)


async def test_concurrent_groups_keep_independent_tokens_and_scopes():
    auth = service()
    auth.groups.rows["other"] = replace(auth.groups.rows["beta"], slug="other", allowed_ministries=("masa",), default_ministry="masa")
    first, second = await asyncio.gather(auth.login("beta", "password", "a"), auth.login("other", "password", "b"))
    assert first.access_token != second.access_token
    assert (await auth.resolve(first.access_token)).authorize_ministry() == "matte"
    assert (await auth.resolve(second.access_token)).authorize_ministry() == "masa"


async def test_quota_precedes_group_lookup_and_password_work():
    auth = service()

    async def denied(*args):
        raise LoginRateLimited(10)

    auth.limiter.acquire = denied
    with pytest.raises(LoginRateLimited):
        await auth.login("beta", "password", "source")
    assert not auth.groups.lookups and not auth.passwords.calls


async def test_reset_during_login_is_not_reported_as_success():
    auth = service()

    async def conflicting(*args):
        raise DatabaseConflict()

    auth.sessions.create = conflicting
    with pytest.raises(InvalidCredentials):
        await auth.login("beta", "password", "source")


@pytest.mark.parametrize("token", ["", "password", "arhs_bad", "arhs_" + "x" * 10000, "arhs_" + "é" * 43])
async def test_malformed_bearer_never_queries_storage(token):
    auth = service()
    with pytest.raises(InvalidCredentials):
        await auth.resolve(token)
    assert not auth.sessions.lookups and not auth.passwords.calls
