"""Postgres readiness probe using the application-owned transaction boundary."""

from __future__ import annotations

from assistant_rh_api.core.errors import ApplicationError
from assistant_rh_api.core.health import HealthReport
from assistant_rh_api.db.pool import Database


class PostgresHealthProbe:
    """Check Postgres and the runtime configuration on demand."""

    def __init__(self, database: Database | None = None) -> None:
        self._database = database

    async def check(self) -> HealthReport:
        if self._database is None:
            return HealthReport(db="error", config_loaded=False)

        try:
            async with self._database.transaction(read_only=True) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT to_regclass('public.rag_config')")
                    row = await cursor.fetchone()
                    if not row or row[0] is None:
                        return HealthReport(db="ok", config_loaded=False)
                    await cursor.execute("SELECT EXISTS (SELECT 1 FROM public.rag_config WHERE id = 1)")
                    config_row = await cursor.fetchone()
                    return HealthReport(db="ok", config_loaded=bool(config_row and config_row[0]))
        except ApplicationError:
            return HealthReport(db="error", config_loaded=False)
