"""Bounded async pool owned by one application lifespan/event loop."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self

import psycopg
from psycopg_pool import AsyncConnectionPool

from assistant_rh_api.db.dsn import DatabaseSettings, validate_libpq_environment
from assistant_rh_api.db.errors import translate_database_errors


class _SafeConnection(psycopg.AsyncConnection):
    @classmethod
    async def connect(cls, *args: Any, **kwargs: Any) -> Self:
        # Pool reconnect workers log connection errors before our public boundary.
        # Scrub there too, so libpq cannot expose conninfo or server error details.
        validate_libpq_environment(os.environ)
        try:
            return await super().connect(*args, **kwargs)
        except psycopg.Error:
            raise psycopg.OperationalError("Database connection failed") from None


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
            with translate_database_errors():
                await self._pool.open(wait=True, timeout=self._settings.timeout_seconds)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        await self._pool.close()

    async def _check_connection(self, connection: psycopg.AsyncConnection) -> None:
        # The pool's queue timeout does not bound its checkout-check callback.
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                await AsyncConnectionPool.check_connection(connection)
        except asyncio.CancelledError:
            await connection.close()
            raise
        except (TimeoutError, psycopg.Error):
            await connection.close()
            raise psycopg.OperationalError("Database connection check failed") from None

    @asynccontextmanager
    async def transaction(self, *, read_only: bool = False) -> AsyncIterator[psycopg.AsyncConnection]:
        """Commit on success, rollback on any exception/cancellation, then release.

        Pass this connection to cooperating repositories for atomic writes.
        Do not commit manually, retain the connection, or issue session-level SET.
        Nested work uses connection.transaction() savepoints, not another lease.
        """
        with translate_database_errors():
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
