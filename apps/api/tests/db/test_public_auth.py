import asyncio
import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from assistant_rh_api.core.auth import AuthService, InvalidCredentials, LoginRateLimited
from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.core.models.auth import Session
from assistant_rh_api.db.auth_stores import GroupStore, SessionStore
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.login_limits import LoginLimits, PostgresLoginLimiter
from assistant_rh_api.db.pool import Database
from assistant_rh_api.gateways.auth import LegacyPasswords, SessionTokens, SystemClock
from assistant_rh_api.handlers.app import create_app

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "supabase/migrations/20260908180029_api_public_auth.sql"
ROLLBACK = ROOT / "docs/architecture/hexagonal-split/sql/rollback_b4_auth.sql"


def legacy_hash(password):
    salt = b"synthetic-salt12".ljust(16, b"0")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
    return "pbkdf2_sha256$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def test_explicit_quota_settings_and_defaults():
    assert LoginLimits.from_environment({}) == LoginLimits()
    assert LoginLimits.from_environment(
        {
            "API_AUTH_SOURCE_LIMIT": "2",
            "API_AUTH_SLUG_LIMIT": "3",
            "API_AUTH_GLOBAL_LIMIT": "10",
            "API_AUTH_WINDOW_SECONDS": "5",
        }
    ) == LoginLimits(source=2, slug=3, global_limit=10, window_seconds=5)


@pytest.mark.parametrize(
    "key,value",
    [
        ("API_AUTH_SOURCE_LIMIT", "0"),
        ("API_AUTH_SLUG_LIMIT", "-1"),
        ("API_AUTH_GLOBAL_LIMIT", "10001"),
        ("API_AUTH_WINDOW_SECONDS", "301"),
        ("API_AUTH_SOURCE_LIMIT", "secret-invalid-value"),
    ],
)
def test_invalid_quota_settings_have_safe_error(key, value):
    with pytest.raises(ValueError, match="^invalid API auth quota configuration$"):
        LoginLimits.from_environment({key: value})


async def test_real_http_lifespan_password_login_indexed_session_and_logout(repository_dsn):
    stored = legacy_hash("synthetic-password")
    with psycopg.connect(repository_dsn) as connection:
        connection.execute("DELETE FROM public.api_auth_limits")
        connection.execute("DELETE FROM public.user_groups WHERE slug = 'http-beta'")
        connection.execute("INSERT INTO public.user_groups(slug,label,password_hash) VALUES ('http-beta','HTTP Beta',%s)", (stored,))
    database = Database(DatabaseSettings(dsn=repository_dsn))
    app = create_app(database=database, environ={})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post("/v1/auth/session", json={"slug": "http-beta", "password": "synthetic-password"})
            assert login.status_code == 200, login.text
            body = login.json()
            token = body["access_token"]
            assert 28798 <= body["expires_in"] <= 28800
            headers = {"Authorization": "Bearer " + token}
            me = await client.get("/v1/auth/me", headers=headers)
            assert me.status_code == 200 and "access_token" not in me.json()
            with psycopg.connect(repository_dsn) as connection:
                row = connection.execute("SELECT token_hash, credential_hash FROM public.api_sessions WHERE group_slug='http-beta'").fetchone()
                assert row == (hashlib.sha256(token.encode()).hexdigest(), stored)
                assert "synthetic-password" not in str(row) and token not in str(row)
            assert (await client.delete("/v1/auth/session", headers=headers)).status_code == 204
            assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401
    assert database.closed and app.state.auth_service is None


async def test_legacy_reset_a_b_a_and_policy_changes_never_revive_tokens(repository_db):
    groups, sessions = GroupStore(repository_db), SessionStore(repository_db)
    now = datetime.now(UTC)
    old = Session("a" * 64, "synthetic", now, now + timedelta(hours=8), "fixture-hash")
    await sessions.create(old)
    async with repository_db.transaction() as conn:
        await conn.execute("UPDATE public.user_groups SET password_hash='new-hash' WHERE slug='synthetic'")
        await conn.execute("UPDATE public.user_groups SET password_hash='fixture-hash' WHERE slug='synthetic'")
    group = await groups.get("synthetic")
    assert group.credential_revision == 2
    assert await sessions.get_active(old.token_hash, now) is None
    with pytest.raises(DatabaseConflict):
        await sessions.create(replace(old, token_hash="b" * 64))
    fresh = replace(old, token_hash="c" * 64, credential_revision=2)
    await sessions.create(fresh)
    assert await sessions.get_active(fresh.token_hash, now) == fresh
    async with repository_db.transaction() as conn:
        await conn.execute("UPDATE public.user_groups SET label='Renamed' WHERE slug='synthetic'")
    assert (await groups.get("synthetic")).credential_revision == 2
    async with repository_db.transaction() as conn:
        await conn.execute("UPDATE public.user_groups SET allowed_ministries='[\"masa\"]', default_ministry='masa' WHERE slug='synthetic'")
        await conn.execute("UPDATE public.user_groups SET allowed_ministries='[\"matte\"]', default_ministry='matte' WHERE slug='synthetic'")
    assert await sessions.get_active(fresh.token_hash, now) is None


async def test_reset_wins_concurrent_login_before_insert(repository_db):
    sessions = SessionStore(repository_db)
    now = datetime.now(UTC)
    session = Session("d" * 64, "synthetic", now, now + timedelta(hours=8), "fixture-hash")
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.user_groups SET password_hash='reset-hash' WHERE slug='synthetic'")
        task = asyncio.create_task(sessions.create(session))
        await asyncio.sleep(0.03)
        assert not task.done()
    with pytest.raises(DatabaseConflict):
        await asyncio.wait_for(task, 2)
    assert await sessions.get_active(session.token_hash, now) is None


@pytest.mark.parametrize("scope", ["source", "slug", "global_limit"])
async def test_login_quota_is_atomic_shared_and_recovers_without_extending_lockout(repository_db, scope):
    limits = replace(LoginLimits(source=100, slug=100, global_limit=100), **{scope: 2})
    one = PostgresLoginLimiter(repository_db, limits)
    two = PostgresLoginLimiter(repository_db, limits)

    async def acquire(i):
        source = "shared" if scope == "source" else f"source-{i}"
        slug = "shared" if scope == "slug" else f"slug-{i}"
        try:
            await (one if i % 2 else two).acquire(source, slug)
            return True
        except LoginRateLimited as exc:
            assert 1 <= exc.retry_after <= 60
            return False

    assert sum(await asyncio.gather(*(acquire(i) for i in range(6)))) == 2
    async with repository_db.transaction() as conn:
        before = await (await conn.execute("SELECT scope,subject,expires_at,attempts FROM public.api_auth_limits ORDER BY scope,subject")).fetchall()
    assert not await acquire(10)
    async with repository_db.transaction() as conn:
        after = await (await conn.execute("SELECT scope,subject,expires_at,attempts FROM public.api_auth_limits ORDER BY scope,subject")).fetchall()
        assert after == before
        await conn.execute("UPDATE public.api_auth_limits SET expires_at=clock_timestamp()-interval '1 second'")
    assert await acquire(11)


async def test_corrupt_policy_fails_closed_and_does_not_become_dict_keys(repository_db):
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.user_groups SET allowed_ministries='{\"matte\":true}' WHERE slug='synthetic'")
    groups = GroupStore(repository_db)
    group = await groups.get("synthetic")
    assert group.allowed_ministries == ()
    auth = AuthService(groups, SessionStore(repository_db), LegacyPasswords(), SessionTokens(), PostgresLoginLimiter(repository_db), SystemClock())
    assert not await auth.list_groups()
    with pytest.raises(InvalidCredentials):
        await auth.login("synthetic", "wrong", "source")


async def test_auth_migration_idempotence_legacy_update_and_rollback(repository_dsn):
    with psycopg.connect(repository_dsn) as connection:
        try:
            connection.execute("DELETE FROM public.user_groups WHERE slug='migration-auth'")
            connection.execute("INSERT INTO public.user_groups(slug,label,password_hash) VALUES ('migration-auth','Migration','before')")
            connection.execute(MIGRATION.read_text())
            connection.execute(MIGRATION.read_text())
            connection.execute("UPDATE public.user_groups SET password_hash='after' WHERE slug='migration-auth'")
            assert connection.execute("SELECT credential_revision FROM public.user_groups WHERE slug='migration-auth'").fetchone() == (1,)
            connection.execute("UPDATE public.user_groups SET credential_revision=0 WHERE slug='migration-auth'")
            assert connection.execute("SELECT credential_revision FROM public.user_groups WHERE slug='migration-auth'").fetchone() == (1,)
            connection.execute(ROLLBACK.read_text())
            connection.execute("UPDATE public.user_groups SET password_hash='legacy-still-works' WHERE slug='migration-auth'")
            assert connection.execute("SELECT password_hash FROM public.user_groups WHERE slug='migration-auth'").fetchone() == (
                "legacy-still-works",
            )
            assert connection.execute("SELECT to_regclass('public.api_auth_limits')").fetchone() == (None,)
            connection.execute(MIGRATION.read_text())
            assert connection.execute("SELECT credential_revision FROM public.user_groups WHERE slug='migration-auth'").fetchone() == (0,)
        finally:
            connection.rollback()
