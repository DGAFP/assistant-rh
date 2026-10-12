"""Bounded async pool owned by one application lifespan/event loop."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self

import psycopg
from psycopg.abc import PQGen
from psycopg_pool import AsyncConnectionPool

from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.db.dsn import DatabaseSettings, validate_libpq_environment
from assistant_rh_api.db.errors import translate_database_errors


class _SafeConnection(psycopg.AsyncConnection):
    async def wait[T](self, gen: PQGen[T], interval: float = 0.1) -> T:
        # psycopg's cancellation handshake may wait forever for a lost reply.
        # Close the socket before cancelling driver I/O, then join it so the
        # original cancellation/timeout propagates and the pool drops the lease.
        operation = asyncio.create_task(super().wait(gen, interval))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await self.close()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise

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
        self._individual_feedback_schema: bool | None = None
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

    async def individual_feedback_schema(self) -> bool:
        """Cache D1 capability for this pool lifetime; partial installs fail closed."""
        if self._individual_feedback_schema is None:
            async with self.transaction(read_only=True) as connection:
                row = await (
                    await connection.execute("""
                    SELECT
                        (SELECT count(*) FROM information_schema.columns
                         WHERE table_schema = 'public' AND (table_name, column_name) IN (
                             ('chat_runs', 'author_user_id'), ('chat_feedbacks', 'api_actor_user_id'),
                             ('chat_feedback_audit', 'actor_user_id'))),
                        (SELECT count(*) FROM pg_catalog.pg_trigger
                         WHERE tgenabled IN ('O', 'A') AND NOT tgisinternal AND (
                             (tgrelid = to_regclass('public.chat_runs') AND tgname = 'api_run_author_immutable') OR
                             (tgrelid = to_regclass('public.chat_feedbacks') AND tgname = 'api_feedback_individual_guard')))
                """)
                ).fetchone()
            if row not in ((0, 0), (3, 2)):
                raise DatabaseUnavailable()
            self._individual_feedback_schema = row == (3, 2)
        return self._individual_feedback_schema

    async def close(self) -> None:
        self._individual_feedback_schema = None
        await self._pool.close()

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
        except (TimeoutError, psycopg.Error):
            await connection.close()
            raise psycopg.OperationalError("Database connection check failed") from None
        finally:
            if not check.done():
                check.cancel()
            await asyncio.gather(check, return_exceptions=True)

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
                    try:
                        yield connection
                    except asyncio.CancelledError:
                        # Cancellation between SQL calls must not start an
                        # unbounded network rollback after its deadline expired.
                        await connection.close()
                        raise
