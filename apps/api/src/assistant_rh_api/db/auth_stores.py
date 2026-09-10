"""Existing group/role storage plus opaque hashed API sessions."""

import re
from datetime import datetime

from psycopg import AsyncConnection

from assistant_rh_api.core.db_diagnostics import DBOperation
from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.core.models.auth import Group, Session
from assistant_rh_api.core.ports.auth import GroupStorePort, SessionStorePort
from assistant_rh_api.db.pool import Database

GROUP_COLUMNS = "slug, label, priority, visible, is_admin, password_hash, allowed_ministries, default_ministry, icon, color, credential_revision"


def _group(row: tuple) -> Group:
    ministries = row[6] if isinstance(row[6], list) and all(isinstance(value, str) for value in row[6]) else ()
    return Group(row[0], row[1], row[2], row[3], row[4], row[5], tuple(ministries), row[7] or "", row[8], row[9], row[10])


class GroupStore(GroupStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_groups(self) -> tuple[Group, ...]:
        async with self._database.transaction(read_only=True, operation=DBOperation.GROUP_LIST) as connection:
            rows = await (
                await connection.execute(
                    f'SELECT {GROUP_COLUMNS} FROM public.user_groups ORDER BY priority DESC, slug COLLATE "C"',
                )
            ).fetchall()
        return tuple(_group(row) for row in rows)

    async def get(self, slug: str) -> Group | None:
        async with self._database.transaction(read_only=True, operation=DBOperation.GROUP_GET) as connection:
            row = await (await connection.execute(f"SELECT {GROUP_COLUMNS} FROM public.user_groups WHERE slug = %s", (slug,))).fetchone()
        return _group(row) if row else None


class SessionStore(SessionStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, session: Session) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", session.token_hash):
            raise ValueError("token_hash must be a SHA-256 digest")
        if session.created_at.tzinfo is None or session.expires_at.tzinfo is None or session.expires_at <= session.created_at:
            raise ValueError("session requires aware timestamps and positive lifetime")
        async with self._database.transaction(operation=DBOperation.SESSION_CREATE) as connection:
            # Lock the credential used to authenticate; a concurrent password
            # reset either precedes this comparison or invalidates the session.
            row = await (
                await connection.execute(
                    "SELECT password_hash, credential_revision FROM public.user_groups WHERE slug = %s FOR SHARE",
                    (session.group_slug,),
                )
            ).fetchone()
            if not row or not row[0] or row[0] != session.credential_hash or row[1] != session.credential_revision:
                raise DatabaseConflict()
            await self._purge_inactive(connection, session.created_at, 100)
            await connection.execute(
                """
                INSERT INTO public.api_sessions (token_hash, group_slug, created_at, expires_at, credential_hash, credential_revision)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (
                    session.token_hash,
                    session.group_slug,
                    session.created_at,
                    session.expires_at,
                    session.credential_hash,
                    session.credential_revision,
                ),
            )

    async def get_active(self, token_hash: str, now: datetime) -> Session | None:
        async with self._database.transaction(read_only=True, operation=DBOperation.SESSION_GET) as connection:
            row = await (
                await connection.execute(
                    """
                SELECT s.token_hash, s.group_slug, s.created_at, s.expires_at, s.credential_hash, s.credential_revision
                FROM public.api_sessions s JOIN public.user_groups g ON g.slug = s.group_slug
                WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > %s AND s.created_at <= %s
                  AND s.credential_hash = g.password_hash
                  AND s.credential_revision = g.credential_revision
            """,
                    (token_hash, now, now),
                )
            ).fetchone()
        return Session(*row) if row else None

    async def revoke(self, token_hash: str, now: datetime) -> None:
        async with self._database.transaction(operation=DBOperation.SESSION_REVOKE) as connection:
            await connection.execute(
                "UPDATE public.api_sessions SET revoked_at = COALESCE(revoked_at, %s) WHERE token_hash = %s",
                (now, token_hash),
            )

    async def purge_inactive(self, now: datetime, *, limit: int = 500) -> int:
        """Delete one indexed batch; active or concurrently locked rows survive."""
        if now.tzinfo is None or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("cleanup requires an aware timestamp and a batch of 1..1000")
        async with self._database.transaction(operation=DBOperation.SESSION_PURGE) as connection:
            return await self._purge_inactive(connection, now, limit)

    @staticmethod
    async def _purge_inactive(connection: AsyncConnection, now: datetime, limit: int) -> int:
        cursor = await connection.execute(
            """WITH expired AS (
                SELECT token_hash FROM public.api_sessions
                WHERE LEAST(expires_at, COALESCE(revoked_at, expires_at)) <= %s
                ORDER BY LEAST(expires_at, COALESCE(revoked_at, expires_at)), token_hash
                LIMIT %s FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.api_sessions s USING expired e WHERE s.token_hash = e.token_hash""",
            (now, limit),
        )
        return cursor.rowcount
