"""Translate driver errors only after transaction cleanup has completed."""

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable

logger = logging.getLogger(__name__)


@contextmanager
def translate_database_errors() -> Iterator[None]:
    try:
        yield
    except (psycopg.IntegrityError, psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
        raise DatabaseConflict() from None
    except (PoolClosed, PoolTimeout, TooManyRequests, psycopg.OperationalError, psycopg.InterfaceError) as exc:
        # Only fixed categories are logged. Driver text can contain DSNs or SQL.
        if isinstance(exc, PoolClosed):
            reason = "pool_closed"
        elif isinstance(exc, PoolTimeout):
            reason = "pool_checkout_timeout"
        elif isinstance(exc, TooManyRequests):
            reason = "pool_queue_full"
        elif isinstance(exc, psycopg.errors.QueryCanceled):
            reason = "query_cancelled"
        else:
            reason = "connection_error"
        logger.warning("Database unavailable (reason=%s)", reason)
        raise DatabaseUnavailable() from None
    except psycopg.Error:
        raise DatabaseFailure() from None
