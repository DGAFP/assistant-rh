"""Bounded async pool owned by one application lifespan/event loop."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self

import psycopg
from psycopg_pool import AsyncConnectionPool

from assistant_rh_api.core.db_diagnostics import DBOperation, attach_diagnostic, database_diagnostic, report_database_error
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.db.dsn import DatabaseSettings, validate_libpq_environment
from assistant_rh_api.db.errors import diagnostic_category, driver_logging, driver_operation, safe_sqlstate, translate_database_errors


class _SafeConnectionFailure(psycopg.OperationalError):
    """Marker for the pool's existing connection-attempt warning."""


class _PoolDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Identify our calls by context, or our connections in worker arguments.
        # Never stringify arguments: even connection/transaction repr includes
        # conninfo. Other pools outside our calls remain untouched.
        args = record.args if isinstance(record.args, tuple) else ()
        operation = driver_operation.get()
        owned = any(
            isinstance(arg, _SafeConnection) or isinstance(arg, psycopg.AsyncTransaction) and isinstance(arg.connection, _SafeConnection)
            for arg in args
        )
        error = next((arg for arg in args if isinstance(arg, Exception)), None)
        diagnostic = database_diagnostic(error) if isinstance(error, _SafeConnectionFailure) else None
        if operation is None and not owned and diagnostic is None:
            return True
        # Debug/info driver messages can already contain interpolated conninfo
        # or exception text; they are not failure diagnostics.
        if record.levelno < logging.WARNING:
            return False
        if diagnostic is None:
            if record.name == "psycopg.transaction" or owned and "rollback" in str(record.msg):
                operation = DBOperation.ROLLBACK
            elif owned:
                operation = DBOperation.RESET
            safe = DatabaseUnavailable()
            diagnostic = attach_diagnostic(
                safe,
                operation=operation or DBOperation.RESET,
                category=diagnostic_category(error) if error else "connection",
                code=safe.code,
                sqlstate=safe_sqlstate(error) if error else None,
            )
        fields = diagnostic.fields(recovered=record.levelno < logging.ERROR)
        record.__dict__.update(fields)
        record.msg, record.args = json.dumps(fields, sort_keys=True), ()
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        diagnostic.reported = True
        return True


for _logger_name in ("psycopg.pool", "psycopg.transaction", "psycopg"):
    logging.getLogger(_logger_name).addFilter(_PoolDiagnosticFilter())


class _SafeConnection(psycopg.AsyncConnection):
    @classmethod
    async def connect(cls, *args: Any, **kwargs: Any) -> Self:
        # Pool reconnect workers log connection errors before our public boundary.
        # Scrub there too, so libpq cannot expose conninfo or server error details.
        validate_libpq_environment(os.environ)
        try:
            with driver_logging(DBOperation.CONNECT):
                return await super().connect(*args, **kwargs)
        except psycopg.Error as exc:
            safe = _SafeConnectionFailure("Database connection failed")
            attach_diagnostic(safe, operation=DBOperation.CONNECT, category="connection", code=DatabaseUnavailable.code, sqlstate=safe_sqlstate(exc))
            raise safe from None


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._pool = AsyncConnectionPool(
            conninfo=settings.dsn,
            connection_class=_SafeConnection,
            kwargs={"autocommit": True, "connect_timeout": settings.connect_timeout_seconds},
            min_size=settings.min_size,
            max_size=settings.max_size,
            max_waiting=settings.max_waiting,
            timeout=settings.timeout_seconds,
            name="assistant-rh-api",
            open=False,
        )

    @property
    def closed(self) -> bool:
        return self._pool.closed

    def statistics(self) -> dict[str, int]:
        return self._pool.get_stats()

    async def open(self) -> None:
        """Wait for initial connections; a failed/cancelled startup closes workers."""
        try:
            validate_libpq_environment(os.environ)
            with translate_database_errors(DBOperation.OPEN):
                await self._pool.open(wait=True, timeout=self._settings.timeout_seconds)
        except BaseException as exc:
            if isinstance(exc, Exception):
                report_database_error(exc, operation=DBOperation.OPEN)
            await self.close()
            raise

    async def close(self) -> None:
        try:
            with translate_database_errors(DBOperation.CLOSE):
                await self._pool.close()
        except Exception as exc:
            report_database_error(exc, operation=DBOperation.CLOSE)
            raise

    async def _check_connection(self, connection: psycopg.AsyncConnection) -> None:
        # Wait without cancelling psycopg: its cancellation handler can itself
        # wait for a lost server response. Close the socket before cancelling it.
        check = asyncio.create_task(AsyncConnectionPool.check_connection(connection))
        try:
            done, _ = await asyncio.wait({check}, timeout=self._settings.timeout_seconds)
            if not done:
                raise TimeoutError
            await check
        except asyncio.CancelledError:
            await connection.close()
            raise
        except (TimeoutError, psycopg.Error) as exc:
            await connection.close()
            safe = psycopg.OperationalError("Database connection check failed")
            attach_diagnostic(
                safe,
                operation=DBOperation.TRANSACTION,
                category="connection_check",
                code=DatabaseUnavailable.code,
                sqlstate=safe_sqlstate(exc),
            )
            raise safe from None
        finally:
            if not check.done():
                check.cancel()
            await asyncio.gather(check, return_exceptions=True)

    @asynccontextmanager
    async def transaction(
        self,
        *,
        read_only: bool = False,
        operation: DBOperation = DBOperation.TRANSACTION,
    ) -> AsyncIterator[psycopg.AsyncConnection]:
        """Commit on success, rollback on any exception/cancellation, then release.

        Pass this connection to cooperating repositories for atomic writes.
        Do not commit manually, retain the connection, or issue session-level SET.
        Nested work uses connection.transaction() savepoints, not another lease.
        """
        with translate_database_errors(operation):
            async with self._pool.connection() as connection:
                # Own the check after acquisition: the pool's callback retry loop
                # also catches CancelledError and can serve a cancelled caller.
                await self._check_connection(connection)
                async with connection.transaction():
                    if read_only:
                        await connection.execute("SET TRANSACTION READ ONLY")
                    await connection.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(self._settings.statement_timeout_ms),),
                    )
                    yield connection
