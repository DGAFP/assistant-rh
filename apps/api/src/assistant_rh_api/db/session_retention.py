"""Periodic bounded session cleanup, owned by the API lifespan."""

from datetime import UTC, datetime

import anyio

from assistant_rh_api.core.db_diagnostics import DBOperation, diagnostic_context, report_database_error
from assistant_rh_api.core.errors import ApplicationError
from assistant_rh_api.db.auth_stores import SessionStore


async def maintain_sessions(store: SessionStore, *, interval_seconds: float = 60) -> None:
    while True:
        with diagnostic_context():
            try:
                # Finish the bounded transaction before shutdown closes the pool.
                # Level cancellation must not interrupt psycopg's rollback/return.
                with anyio.CancelScope(shield=True):
                    await store.purge_inactive(datetime.now(UTC))
            except ApplicationError as exc:
                # Retrying next interval; the transaction itself does not log.
                report_database_error(exc, operation=DBOperation.SESSION_PURGE, recovered=True)
        await anyio.sleep(interval_seconds)
