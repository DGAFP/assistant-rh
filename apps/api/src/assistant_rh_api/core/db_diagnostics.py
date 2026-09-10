"""Safe DB diagnostic values and decision-point logging, independent of drivers.

Adapters attach diagnostics without logging: the caller owns the outcome and
can report either a recovery or a failure. Never render exception messages.
"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable


class DBOperation(StrEnum):
    TRANSACTION = "db.transaction"
    CONNECT = "db.connect"
    OPEN = "db.pool.open"
    CLOSE = "db.pool.close"
    ROLLBACK = "db.rollback"
    RESET = "db.pool.reset"
    HEALTH = "db.health"
    CONFIG_LOAD = "config.load"
    PROMPT_GET = "prompt.get"
    ACRONYM_LOAD = "acronym.load"
    GROUP_LIST = "group.list"
    GROUP_GET = "group.get"
    SESSION_CREATE = "session.create"
    SESSION_GET = "session.get_active"
    SESSION_REVOKE = "session.revoke"
    SESSION_PURGE = "session.purge_inactive"
    LOGIN_ACQUIRE = "login_limit.acquire"
    CONTENT_DOCUMENTS = "content.documents"
    CONTENT_SECTIONS = "content.sections"
    CONTENT_REFERENCES = "content.references"
    CONTENT_CHUNKS = "content.chunks"
    SEARCH = "search.search"
    RUN_FINALIZE = "run.finalize"
    RUN_GET = "run.get"
    RUN_SOURCES = "run.sources"
    FEEDBACK_GET = "feedback.get"
    FEEDBACK_SAVE = "feedback.save"
    FEEDBACK_ANALYSIS = "feedback.for_analysis"
    FEEDBACK_SAVE_ANALYSIS = "feedback.save_analysis"


DBCategory = Literal[
    "integrity",
    "serialization",
    "deadlock",
    "pool_closed",
    "pool_timeout",
    "pool_capacity",
    "connection",
    "interface",
    "statement",
    "connection_check",
    "application",
]
_correlation: ContextVar[str | None] = ContextVar("db_correlation", default=None)
logger = logging.getLogger(__name__)


@contextmanager
def diagnostic_context() -> Iterator[str]:
    """New server-generated ID for a request, startup, or background iteration."""
    correlation_id = uuid4().hex
    token = _correlation.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _correlation.reset(token)


@dataclass(slots=True)
class DatabaseDiagnostic:
    operation: DBOperation
    category: DBCategory
    code: str
    sqlstate: str | None
    correlation_id: str
    reported: bool = False

    def fields(self, *, recovered: bool) -> dict[str, str | bool | None]:
        return {
            "event": "database_recovery" if recovered else "database_failure",
            "operation": self.operation.value,
            "category": self.category,
            "code": self.code,
            "sqlstate": self.sqlstate,
            "correlation_id": self.correlation_id,
            "recovered": recovered,
        }


def attach_diagnostic(
    exc: Exception,
    *,
    operation: DBOperation,
    category: DBCategory,
    code: str,
    sqlstate: str | None = None,
) -> DatabaseDiagnostic:
    # Enum identity rejects even syntactically plausible user-provided labels.
    if not isinstance(operation, DBOperation):
        raise TypeError("DB operation must be a DBOperation")
    diagnostic = DatabaseDiagnostic(operation, category, code, sqlstate, _correlation.get() or uuid4().hex)
    exc._database_diagnostic = diagnostic  # type: ignore[attr-defined]
    return diagnostic


def database_diagnostic(exc: BaseException) -> DatabaseDiagnostic | None:
    """Follow explicit/implicit wrapping, including safe `raise ... from None`."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostic = getattr(current, "_database_diagnostic", None)
        if isinstance(diagnostic, DatabaseDiagnostic):
            return diagnostic
        current = current.__cause__ or current.__context__
    return None


def report_database_error(exc: Exception, *, operation: DBOperation = DBOperation.TRANSACTION, recovered: bool = False) -> None:
    diagnostic = database_diagnostic(exc)
    if diagnostic is None:
        for error in (DatabaseConflict, DatabaseUnavailable, DatabaseFailure):
            if isinstance(exc, error):
                diagnostic = attach_diagnostic(exc, operation=operation, category="application", code=error.code)
                break
    if diagnostic is None or diagnostic.reported:
        return
    fields = diagnostic.fields(recovered=recovered)
    # JSON remains useful with the default text formatter; extras support JSON
    # collectors. No exc_info, stack_info, repr, SQL, parameters, or driver text.
    logger.log(logging.WARNING if recovered else logging.ERROR, json.dumps(fields, sort_keys=True), extra=fields)
    diagnostic.reported = True
