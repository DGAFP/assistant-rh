"""Explicit database configuration, with no environment lookup at import."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite

import psycopg
from psycopg.conninfo import conninfo_to_dict

from assistant_rh_api.core.errors import DatabaseConfigurationError


def validate_libpq_environment(environ: Mapping[str, str]) -> None:
    """Fail closed if libpq could use ambient target-selection parameters.

    Run at pool startup and on every connection attempt (including reconnects).
    Never mutate process-wide environment in an async application.
    """
    target_variables = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE", "PGSERVICEFILE")
    if any(environ.get(key) for key in target_variables):
        raise DatabaseConfigurationError()


def resolve_dsn(*, dsn: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    """An explicit DSN wins, including an explicit empty value (which fails).

    Require host/database/user in the supplied string: libpq's ambient defaults
    and service files must not silently select another database target.
    """
    candidate = dsn if dsn is not None else (environ or {}).get("SCW_POSTGRES_DSN", "")
    candidate = candidate.strip()
    try:
        parameters = conninfo_to_dict(candidate)
    except psycopg.Error:
        raise DatabaseConfigurationError() from None
    if not all(parameters.get(key) for key in ("host", "dbname", "user")) or "service" in parameters:
        raise DatabaseConfigurationError()
    return candidate


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    dsn: str = field(repr=False)
    min_size: int = 1
    max_size: int = 4
    max_waiting: int = 16
    timeout_seconds: float = 2.0
    connect_timeout_seconds: int = 2
    statement_timeout_ms: int = 10_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "dsn", resolve_dsn(dsn=self.dsn))
        if (
            not 1 <= self.min_size <= self.max_size
            or self.max_waiting < 1
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.connect_timeout_seconds < 1
            or self.statement_timeout_ms < 1
        ):
            raise DatabaseConfigurationError()
