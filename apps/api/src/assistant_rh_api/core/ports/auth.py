"""Auth boundaries; adapters own I/O and lifecycle state."""

from datetime import datetime
from typing import Protocol

from assistant_rh_api.core.models.auth import Delegation, Group, Session


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


class DelegationReplayPort(Protocol):
    async def claim(self, token_id: str, key_id: str, expires_at: datetime) -> bool:
        """Record a verified assertion ID; refuse reuse or expiry using the shared storage clock."""
        ...


class DelegationVerifierPort(Protocol):
    def verify(self, token: str, now: datetime) -> Delegation | None:
        """Return the delegation only if a pinned Conversations key signed it for this API."""
        ...
