"""Atomic, expiring admission quotas shared by all API workers."""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from assistant_rh_api.core.auth import LoginRateLimited
from assistant_rh_api.db.pool import Database


@dataclass(frozen=True, slots=True)
class LoginLimits:
    source: int = 20
    slug: int = 20
    global_limit: int = 200
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if any(type(v) is not int or not 1 <= v <= 10000 for v in (self.source, self.slug, self.global_limit)):
            raise ValueError("invalid login quota")
        if type(self.window_seconds) is not int or not 1 <= self.window_seconds <= 300:
            raise ValueError("invalid login quota window")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "LoginLimits":
        try:
            return cls(
                source=int(environment.get("API_AUTH_SOURCE_LIMIT", "20")),
                slug=int(environment.get("API_AUTH_SLUG_LIMIT", "20")),
                global_limit=int(environment.get("API_AUTH_GLOBAL_LIMIT", "200")),
                window_seconds=int(environment.get("API_AUTH_WINDOW_SECONDS", "60")),
            )
        except ValueError:
            raise ValueError("invalid API auth quota configuration") from None


class PostgresLoginLimiter:
    def __init__(self, database: Database, limits: LoginLimits = LoginLimits()) -> None:
        self._database = database
        self._limits = limits

    async def acquire(self, source: str, slug: str) -> None:
        limits = self._limits
        subjects = (("global", "all", limits.global_limit), ("source", source, limits.source), ("slug", slug, limits.slug))
        async with self._database.transaction() as connection:
            # Serialize only short admission transactions, never password work.
            # All instances lock in the same order, including expiry cleanup.
            await connection.execute("SELECT pg_advisory_xact_lock(456, 1)")
            clock_row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
            assert clock_row is not None
            now = clock_row[0]
            await connection.execute("DELETE FROM public.api_auth_limits WHERE expires_at <= %s", (now,))
            for scope, subject, limit in subjects:
                digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
                row = await (
                    await connection.execute(
                        "SELECT attempts, expires_at FROM public.api_auth_limits WHERE scope = %s AND subject = %s",
                        (scope, digest),
                    )
                ).fetchone()
                if row and row[0] >= limit:
                    # Raising rolls back every increment in this reservation.
                    raise LoginRateLimited(max(1, math.ceil((row[1] - now).total_seconds())))
                await connection.execute(
                    """INSERT INTO public.api_auth_limits(scope, subject, expires_at, attempts) VALUES (%s, %s, %s, 1)
                    ON CONFLICT(scope, subject) DO UPDATE SET attempts = api_auth_limits.attempts + 1""",
                    (scope, digest, now + timedelta(seconds=limits.window_seconds)),
                )
