"""Auth boundaries; adapters own I/O and lifecycle state."""

from datetime import datetime
from typing import Protocol

from assistant_rh_api.core.models.auth import Group, Session


class GroupStorePort(Protocol):
    async def list_groups(self) -> tuple[Group, ...]: ...
    async def get(self, slug: str) -> Group | None: ...


class SessionStorePort(Protocol):
    async def create(self, session: Session) -> None: ...
    async def get_active(self, token_hash: str, now: datetime) -> Session | None: ...
    async def revoke(self, token_hash: str, now: datetime) -> None: ...


class PasswordVerifierPort(Protocol):
    async def verify(self, password: str, stored_hash: str | None) -> bool: ...


class SessionTokenPort(Protocol):
    def issue(self) -> str: ...

    def digest(self, token: str) -> str | None: ...


class LoginLimiterPort(Protocol):
    async def acquire(self, source: str, slug: str) -> None:
        """Reserve a password attempt or raise LoginRateLimited before hashing."""
        ...
