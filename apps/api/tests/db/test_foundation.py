from __future__ import annotations

import asyncio
import traceback
from dataclasses import replace

import psycopg
import pytest
from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.errors import translate_database_errors
from assistant_rh_api.db.pool import Database
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(synthetic_database_dsn: str):
    database = Database(DatabaseSettings(dsn=synthetic_database_dsn, max_size=1, timeout_seconds=0.25))
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def read_marker(database: Database) -> str:
    async with database.transaction(read_only=True) as connection:
        cursor = await connection.execute("SELECT updated_by FROM public.rag_config WHERE id = 1")
        return (await cursor.fetchone())[0]


async def test_commit_and_application_exception_rollback(database: Database) -> None:
    original = await read_marker(database)
    try:
        async with database.transaction() as connection:
            await connection.execute("UPDATE public.rag_config SET updated_by = 'synthetic-commit' WHERE id = 1")
        assert await read_marker(database) == "synthetic-commit"

        with pytest.raises(ValueError, match="synthetic business error"):
            async with database.transaction() as connection:
                await connection.execute("UPDATE public.rag_config SET updated_by = 'synthetic-rollback' WHERE id = 1")
                raise ValueError("synthetic business error")
        assert await read_marker(database) == "synthetic-commit"
    finally:
        async with database.transaction() as connection:
            await connection.execute("UPDATE public.rag_config SET updated_by = %s WHERE id = 1", (original,))


async def test_driver_failure_rolls_back_before_translation_and_reuse(database: Database) -> None:
    original = await read_marker(database)
    with pytest.raises(DatabaseConflict) as caught:
        async with database.transaction() as connection:
            await connection.execute("UPDATE public.rag_config SET updated_by = 'synthetic-rollback' WHERE id = 1")
            await connection.execute("INSERT INTO public.rag_config (id) VALUES (1)")
    assert str(caught.value) == "database_conflict"
    assert await read_marker(database) == original
    assert database.statistics()["pool_size"] == 1


async def test_read_only_and_local_session_parameters_do_not_leak(database: Database) -> None:
    async with database.transaction(read_only=True) as connection:
        await connection.execute("SELECT '[1]'::vector")
        cursor = await connection.execute("SHOW ivfflat.probes")
        original = (await cursor.fetchone())[0]
        await connection.execute("SET LOCAL ivfflat.probes = 7")
    async with database.transaction() as connection:
        cursor = await connection.execute("SHOW ivfflat.probes")
        assert (await cursor.fetchone())[0] == original
        cursor = await connection.execute("SHOW transaction_read_only")
        assert (await cursor.fetchone())[0] == "off"
        cursor = await connection.execute("SHOW statement_timeout")
        assert (await cursor.fetchone())[0] == "10s"
    with pytest.raises(DatabaseFailure):
        async with database.transaction(read_only=True) as connection:
            await connection.execute("UPDATE public.rag_config SET updated_by = 'forbidden' WHERE id = 1")


async def test_cancellation_rolls_back_and_returns_lease(database: Database) -> None:
    original = await read_marker(database)
    entered = asyncio.Event()

    async def cancelled_writer() -> None:
        async with database.transaction() as connection:
            await connection.execute("UPDATE public.rag_config SET updated_by = 'synthetic-cancelled' WHERE id = 1")
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled_writer())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await read_marker(database) == original


async def test_pool_exhaustion_and_closed_pool_are_stable_errors(database: Database) -> None:
    async with database.transaction():
        with pytest.raises(DatabaseUnavailable):
            async with database.transaction():
                pytest.fail("the sole connection is already leased")
    assert await read_marker(database)
    await database.close()
    await database.close()
    with pytest.raises(DatabaseUnavailable):
        async with database.transaction():
            pytest.fail("closed pool cannot issue a lease")


async def test_database_down_at_startup_closes_workers_without_sensitive_logs(synthetic_database_dsn: str, caplog) -> None:
    # The fixture has validated and initialized the local target. This deliberately
    # selects another, absent database on that same synthetic server.
    parameters = psycopg.conninfo.conninfo_to_dict(synthetic_database_dsn)
    parameters["dbname"] = "synthetic_missing_secret_marker"
    parameters["password"] = "synthetic_password_secret_marker"
    dsn = psycopg.conninfo.make_conninfo(**parameters)
    database = Database(DatabaseSettings(dsn=dsn, timeout_seconds=0.15))
    with pytest.raises(DatabaseUnavailable) as caught:
        await database.open()
    assert database.closed
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "synthetic_missing_secret_marker" not in rendered
    assert "synthetic_password_secret_marker" not in rendered
    assert dsn not in rendered


async def test_statement_timeout_rolls_back_and_pool_recovers(synthetic_database_dsn: str) -> None:
    settings = replace(DatabaseSettings(dsn=synthetic_database_dsn), max_size=1, statement_timeout_ms=20)
    database = Database(settings)
    await database.open()
    try:
        with pytest.raises(DatabaseUnavailable):
            async with database.transaction() as connection:
                await connection.execute("SELECT pg_sleep(1)")
        assert await read_marker(database)
    finally:
        await database.close()


async def test_unresponsive_checkout_check_is_bounded_and_connection_replaced(database: Database, monkeypatch) -> None:
    checked_connections = []
    real_check = AsyncConnectionPool.check_connection

    async def unresponsive_check(connection) -> None:
        checked_connections.append(connection)
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncConnectionPool, "check_connection", unresponsive_check)
    with pytest.raises(DatabaseUnavailable):
        # Regression guard: the pool's own queue timeout doesn't cover checks.
        await asyncio.wait_for(read_marker(database), timeout=1)
    assert checked_connections and all(connection.closed for connection in checked_connections)
    monkeypatch.setattr(AsyncConnectionPool, "check_connection", real_check)
    assert await read_marker(database)


async def test_cancellation_during_checkout_cannot_reach_business_writes(database: Database, monkeypatch) -> None:
    checking = asyncio.Event()
    checked_connections = []
    real_check = AsyncConnectionPool.check_connection

    async def check(connection) -> None:
        checked_connections.append(connection)
        if len(checked_connections) == 1:
            checking.set()
            await asyncio.Event().wait()
        else:
            await real_check(connection)

    monkeypatch.setattr(AsyncConnectionPool, "check_connection", check)
    reached_business_code = False

    async def writer() -> None:
        nonlocal reached_business_code
        async with database.transaction():
            reached_business_code = True

    task = asyncio.create_task(writer())
    await asyncio.wait_for(checking.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not reached_business_code
    assert checked_connections[0].closed
    assert await read_marker(database)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (psycopg.errors.SerializationFailure, DatabaseConflict),
        (psycopg.errors.DeadlockDetected, DatabaseConflict),
        (psycopg.errors.UndefinedTable, DatabaseFailure),
        (psycopg.OperationalError, DatabaseUnavailable),
    ],
)
async def test_error_translation_suppresses_driver_details(error, expected) -> None:
    with pytest.raises(expected) as caught:
        with translate_database_errors():
            raise error("postgresql://synthetic:secret@host/db")
    assert "postgresql" not in "".join(traceback.format_exception(caught.value))
