"""Periodic bounded session cleanup, owned by the API lifespan."""

import logging
from datetime import UTC, datetime

import anyio

from assistant_rh_api.core.errors import ApplicationError
from assistant_rh_api.db.auth_stores import SessionStore

logger = logging.getLogger(__name__)


async def maintain_sessions(store: SessionStore, *, interval_seconds: float = 60) -> None:
    while True:
        try:
            # Finish the bounded transaction before shutdown closes the pool.
            # Level cancellation must not interrupt psycopg's rollback/return.
            with anyio.CancelScope(shield=True):
                await store.purge_inactive(datetime.now(UTC))
        except ApplicationError:
            # Retry next interval. Never log SQL, session hashes or DB details.
            logger.warning("Session cleanup unavailable; retrying next interval")
        await anyio.sleep(interval_seconds)
