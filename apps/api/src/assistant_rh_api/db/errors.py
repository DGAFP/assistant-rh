"""Translate driver errors only after transaction cleanup has completed."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from assistant_rh_api.core.db_diagnostics import DBCategory, DBOperation, attach_diagnostic, database_diagnostic
from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable

driver_operation: ContextVar[DBOperation | None] = ContextVar("db_driver_operation", default=None)


@contextmanager
def driver_logging(operation: DBOperation) -> Iterator[None]:
    """Identify logs emitted inside our driver calls, including secondary errors."""
    token = driver_operation.set(operation)
    try:
        yield
    finally:
        driver_operation.reset(token)


def safe_sqlstate(exc: Exception) -> str | None:
    value = getattr(exc, "sqlstate", None)
    # Only known PostgreSQL codes, never arbitrary five-character server text.
    if type(value) is str:
        try:
            known = psycopg.errors.lookup(value)
        except KeyError:
            pass
        else:
            return value if known.sqlstate == value else None
    return None


def diagnostic_category(exc: Exception) -> DBCategory:
    for error, category in (
        (psycopg.errors.SerializationFailure, "serialization"),
        (psycopg.errors.DeadlockDetected, "deadlock"),
        (psycopg.IntegrityError, "integrity"),
        (PoolClosed, "pool_closed"),
        (PoolTimeout, "pool_timeout"),
        (TooManyRequests, "pool_capacity"),
        (psycopg.InterfaceError, "interface"),
        (psycopg.OperationalError, "connection"),
    ):
        if isinstance(exc, error):
            if category == "connection" and (state := safe_sqlstate(exc)) is not None and not state.startswith("08"):
                return "statement"
            return cast(DBCategory, category)
    return "statement"


@contextmanager
def translate_database_errors(operation: DBOperation = DBOperation.TRANSACTION) -> Iterator[None]:
    if not isinstance(operation, DBOperation):
        raise TypeError("DB operation must be a DBOperation")
    try:
        with driver_logging(operation):
            yield
    except (DatabaseConflict, DatabaseUnavailable, DatabaseFailure) as exc:
        if database_diagnostic(exc) is None:
            if isinstance(exc, DatabaseConflict):
                code = DatabaseConflict.code
            elif isinstance(exc, DatabaseUnavailable):
                code = DatabaseUnavailable.code
            else:
                code = DatabaseFailure.code
            attach_diagnostic(exc, operation=operation, category="application", code=code)
        raise
    except (PoolClosed, PoolTimeout, TooManyRequests, psycopg.Error) as exc:
        translated: DatabaseConflict | DatabaseUnavailable | DatabaseFailure
        if isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)):
            translated = DatabaseConflict()
        elif isinstance(exc, (PoolClosed, PoolTimeout, TooManyRequests, psycopg.OperationalError, psycopg.InterfaceError)):
            translated = DatabaseUnavailable()
        else:
            translated = DatabaseFailure()
        previous = database_diagnostic(exc)
        attach_diagnostic(
            translated,
            operation=operation,
            category=previous.category if previous else diagnostic_category(exc),
            code=translated.code,
            sqlstate=previous.sqlstate if previous else safe_sqlstate(exc),
        )
        raise translated from None
