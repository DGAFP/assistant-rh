"""Lazy Postgres readiness probe."""

from __future__ import annotations

import os

import psycopg

from assistant_rh_api.core.health import HealthReport


class PostgresHealthProbe:
    """Check Postgres and the runtime configuration on demand."""

    def __init__(self, dsn: str | None = None, *, connect_timeout_seconds: int = 2) -> None:
        self._dsn = dsn
        self._connect_timeout_seconds = connect_timeout_seconds

    async def check(self) -> HealthReport:
        dsn = self._dsn or os.getenv("SCW_POSTGRES_DSN", "").strip()
        if not dsn:
            return HealthReport(db="error", config_loaded=False)

        try:
            async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=self._connect_timeout_seconds) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT to_regclass('public.rag_config')")
                    row = await cursor.fetchone()
                    if not row or row[0] is None:
                        return HealthReport(db="ok", config_loaded=False)
                    await cursor.execute("SELECT EXISTS (SELECT 1 FROM public.rag_config WHERE id = 1)")
                    config_row = await cursor.fetchone()
                    return HealthReport(db="ok", config_loaded=bool(config_row and config_row[0]))
        except psycopg.Error:
            return HealthReport(db="error", config_loaded=False)
