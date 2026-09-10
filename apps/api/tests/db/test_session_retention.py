import asyncio
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.models.auth import Session
from assistant_rh_api.db.auth_stores import SessionStore
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.session_retention import maintain_sessions
from assistant_rh_api.handlers.app import create_app
from psycopg.pq import TransactionStatus

pytestmark = pytest.mark.anyio


async def seed_sessions(database, now):
    async with database.transaction() as connection:
        for index, expires, revoked in (
            (1, now - timedelta(seconds=1), None),
            (2, now, None),
            (3, now + timedelta(hours=1), now - timedelta(seconds=1)),
            (4, now + timedelta(hours=1), None),
        ):
            await connection.execute(
                """INSERT INTO public.api_sessions(token_hash,group_slug,credential_hash,created_at,expires_at,revoked_at)
                VALUES (%s,'synthetic','fixture-hash',%s,%s,%s)""",
                (f"{index:064x}", now - timedelta(hours=1), expires, revoked),
            )


async def test_cleanup_is_bounded_skips_locked_rows_and_preserves_active_sessions(repository_db):
    now = datetime.now(UTC)
    await seed_sessions(repository_db, now)
    store = SessionStore(repository_db)
    async with repository_db.transaction() as connection:
        await connection.execute("SELECT token_hash FROM public.api_sessions WHERE token_hash=%s FOR UPDATE", (f"{1:064x}",))
        assert await asyncio.wait_for(store.purge_inactive(now, limit=1), 1) == 1
        assert await asyncio.wait_for(store.purge_inactive(now, limit=10), 1) == 1
        assert await store.purge_inactive(now) == 0
    assert await store.purge_inactive(now) == 1
    assert await store.get_active(f"{4:064x}", now) is not None
    assert await store.purge_inactive(now) == 0


async def test_concurrent_cleanup_workers_do_not_double_count_or_remove_live_rows(repository_db):
    now = datetime.now(UTC)
    await seed_sessions(repository_db, now)
    results = await asyncio.gather(*(SessionStore(repository_db).purge_inactive(now, limit=1) for _ in range(3)))
    assert sum(results) == 3
    assert await SessionStore(repository_db).get_active(f"{4:064x}", now) is not None


async def test_successful_login_cleans_old_sessions_in_the_same_transaction(repository_db):
    now = datetime.now(UTC)
    await seed_sessions(repository_db, now)
    store = SessionStore(repository_db)
    await store.create(Session("a" * 64, "synthetic", now, now + timedelta(hours=8), "fixture-hash"))
    async with repository_db.transaction(read_only=True) as connection:
        rows = await (await connection.execute("SELECT token_hash FROM public.api_sessions ORDER BY token_hash")).fetchall()
    assert rows == [(f"{4:064x}",), ("a" * 64,)]


async def test_lifespan_cleans_without_login_and_closes_worker_before_pool(repository_db, repository_dsn):
    now = datetime.now(UTC)
    await seed_sessions(repository_db, now)
    database = Database(DatabaseSettings(dsn=repository_dsn))
    app = create_app(database=database, environ={})
    async with app.router.lifespan_context(app):
        with anyio.fail_after(2):
            while True:
                async with repository_db.transaction(read_only=True) as connection:
                    row = await (await connection.execute("SELECT count(*) FROM public.api_sessions")).fetchone()
                if row == (1,):
                    break
                await anyio.sleep(0.001)
        assert await SessionStore(repository_db).get_active(f"{4:064x}", now) is not None
    assert database.closed


async def test_retention_worker_retries_safely_and_cancels(caplog):
    calls = 0
    recovered = anyio.Event()

    class Store:
        async def purge_inactive(self, now):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DatabaseUnavailable() from RuntimeError("must-not-be-logged")
            recovered.set()

    async with anyio.create_task_group() as tasks:

        async def run():
            await maintain_sessions(Store(), interval_seconds=0.001)

        tasks.start_soon(run)
        with anyio.fail_after(1):
            await recovered.wait()
        tasks.cancel_scope.cancel()
    assert calls >= 2 and "must-not-be-logged" not in caplog.text
    records = [record for record in caplog.records if getattr(record, "event", None) == "database_recovery"]
    assert len(records) == 1
    assert records[0].operation == "session.purge_inactive"
    assert records[0].code == "database_unavailable"
    assert records[0].levelname == "WARNING"


async def test_shutdown_finishes_inflight_transaction_without_forcing_connection_closed(repository_db, monkeypatch, caplog):
    entered = anyio.Event()
    release = anyio.Event()
    connections = []
    purge = SessionStore._purge_inactive

    async def paused_purge(connection, now, limit):
        connections.append(connection)
        await connection.execute("SELECT 1")
        entered.set()
        await release.wait()
        return await purge(connection, now, limit)

    monkeypatch.setattr(SessionStore, "_purge_inactive", staticmethod(paused_purge))
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(maintain_sessions, SessionStore(repository_db))
        with anyio.fail_after(2):
            await entered.wait()
        tasks.cancel_scope.cancel()
        with anyio.CancelScope(shield=True):
            try:
                await anyio.sleep(0.01)
                assert not connections[0].closed
            finally:
                release.set()
    assert not connections[0].closed
    assert connections[0].info.transaction_status == TransactionStatus.IDLE
    assert "rolled_back_with_error" not in caplog.text
    assert "another command is already in progress" not in caplog.text
