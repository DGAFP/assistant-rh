"""Real adapter/HTTP/pool paths with injected driver failures; no remote DB."""

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import psycopg
import pytest
from assistant_rh_api.core.auth import InvalidCredentials
from assistant_rh_api.core.db_diagnostics import DBOperation, database_diagnostic, diagnostic_context, report_database_error
from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.errors import translate_database_errors
from assistant_rh_api.db.health import PostgresHealthProbe
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.settings_stores import AcronymStore, ConfigStore
from assistant_rh_api.handlers.app import create_app
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

SECRET = "postgresql://private:password@secret-host/private-db SELECT email FROM people; Bearer private-token alice@example.org\nforged-log"
SETTINGS_DSN = "postgresql://synthetic:synthetic@127.0.0.1/assistant_rh_api_test"


def records(caplog):
    return [record for record in caplog.records if getattr(record, "event", None) in {"database_failure", "database_recovery"}]


def assert_safe(caplog):
    for record in caplog.records:
        assert record.exc_info is None
        assert "password" not in repr(record.__dict__)
        assert "private-token" not in repr(record.__dict__)
        assert "alice@example.org" not in repr(record.__dict__)
        assert "forged-log" not in repr(record.__dict__)


@pytest.mark.parametrize(
    "driver,expected,category,sqlstate",
    [
        (psycopg.errors.UniqueViolation, DatabaseConflict, "integrity", "23505"),
        (psycopg.errors.SerializationFailure, DatabaseConflict, "serialization", "40001"),
        (psycopg.errors.DeadlockDetected, DatabaseConflict, "deadlock", "40P01"),
        (psycopg.errors.UndefinedTable, DatabaseFailure, "statement", "42P01"),
        (psycopg.errors.QueryCanceled, DatabaseUnavailable, "statement", "57014"),
        (psycopg.OperationalError, DatabaseUnavailable, "connection", None),
        (psycopg.InterfaceError, DatabaseUnavailable, "interface", None),
        (PoolClosed, DatabaseUnavailable, "pool_closed", None),
        (PoolTimeout, DatabaseUnavailable, "pool_timeout", None),
        (TooManyRequests, DatabaseUnavailable, "pool_capacity", None),
    ],
)
def test_translated_diagnostic_is_safe_and_logged_once_at_decision(caplog, driver, expected, category, sqlstate):
    with diagnostic_context() as correlation_id:
        with pytest.raises(expected) as caught:
            with translate_database_errors(DBOperation.PROMPT_GET):
                raise driver(SECRET)
        assert not caplog.records  # Recovery/failure is still the caller's decision.
        report_database_error(caught.value)
        report_database_error(caught.value)
    assert str(caught.value) == expected.code
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    [record] = records(caplog)
    assert (record.operation, record.code, record.category, record.sqlstate) == ("prompt.get", expected.code, category, sqlstate)
    assert record.correlation_id == correlation_id
    assert record.levelno == logging.ERROR
    assert json.loads(record.getMessage())["sqlstate"] == sqlstate
    assert_safe(caplog)


@pytest.mark.parametrize("sqlstate", [SECRET, "TOKEN", "42p01", "23505\n", None, 23505])
def test_untrusted_sqlstate_is_discarded(caplog, sqlstate):
    class HostileError(psycopg.Error):
        pass

    HostileError.sqlstate = sqlstate
    with pytest.raises(DatabaseFailure) as caught:
        with translate_database_errors(DBOperation.SEARCH):
            raise HostileError(SECRET)
    report_database_error(caught.value)
    assert records(caplog)[0].sqlstate is None
    assert_safe(caplog)


def test_operation_requires_enum_not_user_text():
    for value in (SECRET, "config.load"):
        with pytest.raises(TypeError, match="DBOperation"):
            with translate_database_errors(value):
                pytest.fail("invalid operation must be rejected before database work")


class FailingPool:
    @asynccontextmanager
    async def connection(self):
        raise psycopg.errors.UndefinedTable(SECRET)
        yield  # pragma: no cover


def failing_database():
    from assistant_rh_api.db.dsn import DatabaseSettings

    database = Database(DatabaseSettings(dsn=SETTINGS_DSN))
    database._pool = FailingPool()
    return database


@pytest.mark.anyio
async def test_repository_http_path_preserves_response_and_correlates_concurrent_failures(caplog):
    application = create_app(environ={})
    store = ConfigStore(failing_database())

    @application.get("/diagnostic-test")
    async def fail():
        await asyncio.sleep(0)
        await store.load()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        responses = await asyncio.gather(
            *[client.get("/diagnostic-test", headers={"X-Request-ID": "private-token", "Authorization": "Bearer private-token"}) for _ in range(2)]
        )
    assert all(response.status_code == 500 for response in responses)
    assert responses[0].json() == {"error": {"message": "Internal server error", "type": "server_error", "code": "internal_error"}}
    ids = {response.headers["x-request-id"] for response in responses}
    assert len(ids) == 2 and all(len(value) == 32 for value in ids)
    assert len(records(caplog)) == 2
    assert {record.correlation_id for record in records(caplog)} == ids
    assert all(record.operation == "config.load" and record.sqlstate == "42P01" for record in records(caplog))
    assert_safe(caplog)


@pytest.mark.anyio
async def test_configuration_recovery_emits_one_warning_and_preserves_defaults(caplog):
    with diagnostic_context() as correlation_id:
        value = await RAGConfigurationService(ConfigStore(failing_database())).load()
    assert value.fallback == "database_failure"
    assert value.config.origin == "default"
    [record] = records(caplog)
    assert record.operation == "config.load" and record.sqlstate == "42P01"
    assert record.correlation_id == correlation_id and record.levelno == logging.WARNING
    assert_safe(caplog)


@pytest.mark.anyio
async def test_health_failure_is_logged_once_even_when_converted_to_report(caplog):
    report = await PostgresHealthProbe(failing_database()).check()
    assert report.db == "error" and not report.config_loaded
    [record] = records(caplog)
    assert record.operation == "db.health" and record.levelno == logging.ERROR


@pytest.mark.anyio
async def test_existing_acronym_decision_warning_has_no_adapter_duplicate(caplog):
    # This is the #545 decision contract, not a claim that C6 is wired.
    try:
        await AcronymStore(failing_database()).load()
    except DatabaseFailure as exc:
        assert database_diagnostic(exc).operation == DBOperation.ACRONYM_LOAD
        logging.getLogger("query_processor").warning("Acronym loading failed (%s); continuing query processing without acronyms", exc.code)
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    assert_safe(caplog)


@pytest.mark.anyio
async def test_wrapped_conflict_keeps_diagnostic_and_401_contract(caplog):
    application = create_app(environ={})

    @application.get("/wrapped-conflict")
    async def fail():
        try:
            with translate_database_errors(DBOperation.SESSION_CREATE):
                raise psycopg.errors.UniqueViolation(SECRET)
        except DatabaseConflict:
            raise InvalidCredentials() from None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/wrapped-conflict")
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key"
    [record] = records(caplog)
    assert record.operation == "session.create" and record.sqlstate == "23505"
    assert_safe(caplog)


@pytest.mark.anyio
async def test_pool_connection_attempt_and_terminal_timeout_are_distinct_safe_events(caplog, monkeypatch):
    from assistant_rh_api.db.dsn import DatabaseSettings

    # Exercise real pool workers/retry logging; the network connect is replaced.
    connect = AsyncMock(side_effect=psycopg.errors.InvalidPassword(SECRET))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    database = Database(DatabaseSettings(dsn=SETTINGS_DSN, timeout_seconds=0.05))
    with diagnostic_context() as correlation_id:
        with pytest.raises(DatabaseUnavailable) as caught:
            await database.open()
        report_database_error(caught.value)  # An outer lifespan must not duplicate.
    assert database.closed
    assert connect.await_count == 1
    assert len(caplog.records) == 2
    retry, failure = records(caplog)
    assert (retry.operation, retry.sqlstate, retry.levelno) == ("db.connect", "28P01", logging.WARNING)
    assert (failure.operation, failure.category, failure.levelno) == ("db.pool.open", "pool_timeout", logging.ERROR)
    assert retry.correlation_id == failure.correlation_id == correlation_id
    assert_safe(caplog)


@pytest.mark.anyio
async def test_checkout_failure_keeps_repository_operation_and_sqlstate(caplog, monkeypatch):
    from assistant_rh_api.db.dsn import DatabaseSettings
    from psycopg_pool import AsyncConnectionPool

    connection = AsyncMock()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield connection

    database = Database(DatabaseSettings(dsn=SETTINGS_DSN))
    database._pool = Pool()
    monkeypatch.setattr(AsyncConnectionPool, "check_connection", AsyncMock(side_effect=psycopg.errors.ConnectionFailure(SECRET)))
    value = await RAGConfigurationService(ConfigStore(database)).load()
    assert value.fallback == "database_unavailable"
    connection.close.assert_awaited_once()
    [record] = records(caplog)
    assert (record.operation, record.category, record.sqlstate) == ("config.load", "connection_check", "08006")
    assert_safe(caplog)


@pytest.mark.anyio
async def test_cancellation_and_unexpected_errors_are_not_translated_or_logged(caplog):
    for error in (asyncio.CancelledError, ValueError):
        with pytest.raises(error):
            with translate_database_errors(DBOperation.CONFIG_LOAD):
                raise error()
    assert not caplog.records


@pytest.mark.anyio
async def test_pool_background_rollback_does_not_leak_exception_or_connection(caplog, monkeypatch):
    from assistant_rh_api.db.dsn import DatabaseSettings
    from assistant_rh_api.db.pool import _SafeConnection
    from psycopg.pq import TransactionStatus

    pgconn = Mock(transaction_status=TransactionStatus.INERROR)
    connection = _SafeConnection(pgconn)
    monkeypatch.setattr(connection, "rollback", AsyncMock(side_effect=psycopg.OperationalError(SECRET)))
    monkeypatch.setattr(connection, "close", AsyncMock())
    database = Database(DatabaseSettings(dsn=SETTINGS_DSN))
    # No HTTP/driver context: the background pool recognizes its connection.
    await database._pool._reset_connection(connection)
    connection.close.assert_awaited_once()
    assert len(records(caplog)) == 2  # abnormal return, then failed rollback
    assert records(caplog)[-1].operation == "db.rollback"
    assert_safe(caplog)


def test_transaction_secondary_rollback_error_is_sanitized_without_clobbering_original(caplog, monkeypatch):
    from assistant_rh_api.db.pool import _SafeConnection

    transaction = psycopg.AsyncTransaction(_SafeConnection(Mock()))

    def broken_rollback(exc):
        raise psycopg.errors.ConnectionFailure(SECRET)
        yield  # pragma: no cover

    monkeypatch.setattr(transaction, "_rollback_gen", broken_rollback)
    original = ValueError("business-error")
    with pytest.raises(StopIteration) as result:
        next(transaction._exit_gen(ValueError, original, None))
    assert result.value.value is False  # original exception remains unsuppressed
    [record] = records(caplog)
    assert record.operation == "db.rollback" and record.sqlstate == "08006"
    assert_safe(caplog)


def test_driver_debug_output_inside_owned_call_cannot_expose_details(caplog):
    caplog.set_level(logging.DEBUG)
    with translate_database_errors(DBOperation.CONFIG_LOAD):
        logging.getLogger("psycopg").debug("connection failed: %s", SECRET)
        logging.getLogger("psycopg.transaction").debug(SECRET, exc_info=True)
    assert not caplog.records
