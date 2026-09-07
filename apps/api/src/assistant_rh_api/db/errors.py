"""Translate driver errors only after transaction cleanup has completed."""

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable


@contextmanager
def translate_database_errors() -> Iterator[None]:
    try:
        yield
    except (psycopg.IntegrityError, psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
        raise DatabaseConflict() from None
    except (PoolClosed, PoolTimeout, TooManyRequests, psycopg.OperationalError, psycopg.InterfaceError):
        raise DatabaseUnavailable() from None
    except psycopg.Error:
        raise DatabaseFailure() from None
