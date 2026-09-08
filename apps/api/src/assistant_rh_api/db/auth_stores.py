"""Existing group/role storage plus opaque hashed API sessions."""

import re
from datetime import datetime

from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.core.ports import GroupStorePort, SessionStorePort
from assistant_rh_api.core.runtime import Group, Session
from assistant_rh_api.db.pool import Database

GROUP_COLUMNS = "slug, label, priority, visible, is_admin, password_hash, allowed_ministries, default_ministry"


def _group(row: tuple) -> Group:
    return Group(row[0], row[1], row[2], row[3], row[4], row[5], tuple(row[6] or ()), row[7] or "")


class GroupStore(GroupStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_groups(self) -> tuple[Group, ...]:
        async with self._database.transaction(read_only=True) as connection:
            rows = await (
                await connection.execute(
                    f'SELECT {GROUP_COLUMNS} FROM public.user_groups ORDER BY priority, slug COLLATE "C"',
                )
            ).fetchall()
        return tuple(_group(row) for row in rows)

    async def get(self, slug: str) -> Group | None:
        async with self._database.transaction(read_only=True) as connection:
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
        async with self._database.transaction() as connection:
            # Lock the credential used to authenticate; a concurrent password
            # reset either precedes this comparison or invalidates the session.
            row = await (
                await connection.execute(
                    "SELECT password_hash FROM public.user_groups WHERE slug = %s FOR SHARE",
                    (session.group_slug,),
                )
            ).fetchone()
            if not row or not row[0] or row[0] != session.credential_hash:
                raise DatabaseConflict()
            await connection.execute(
                """
                INSERT INTO public.api_sessions (token_hash, group_slug, created_at, expires_at, credential_hash)
                VALUES (%s, %s, %s, %s, %s)
            """,
                (session.token_hash, session.group_slug, session.created_at, session.expires_at, session.credential_hash),
            )

    async def get_active(self, token_hash: str, now: datetime) -> Session | None:
        async with self._database.transaction(read_only=True) as connection:
            row = await (
                await connection.execute(
                    """
                SELECT s.token_hash, s.group_slug, s.created_at, s.expires_at, s.credential_hash
                FROM public.api_sessions s JOIN public.user_groups g ON g.slug = s.group_slug
                WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > %s AND s.created_at <= %s
                  AND s.credential_hash = g.password_hash
            """,
                    (token_hash, now, now),
                )
            ).fetchone()
        return Session(*row) if row else None

    async def revoke(self, token_hash: str, now: datetime) -> None:
        async with self._database.transaction() as connection:
            await connection.execute(
                "UPDATE public.api_sessions SET revoked_at = COALESCE(revoked_at, %s) WHERE token_hash = %s",
                (now, token_hash),
            )
