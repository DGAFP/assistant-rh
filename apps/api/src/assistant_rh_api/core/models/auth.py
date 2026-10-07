"""Immutable auth values, independent of storage and transport."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Group:
    slug: str
    label: str
    priority: int
    visible: bool
    is_admin: bool
    password_hash: str | None = field(repr=False)
    allowed_ministries: tuple[str, ...]
    default_ministry: str
    icon: str = ""
    color: str = ""
    credential_revision: int = 0

    @property
    def roles(self) -> tuple[str, ...]:
        return ("admin",) if self.is_admin else ("user",)


@dataclass(frozen=True, slots=True)
class Session:
    token_hash: str = field(repr=False)
    group_slug: str
    created_at: datetime
    expires_at: datetime
    credential_hash: str = field(repr=False)
    credential_revision: int = 0


@dataclass(frozen=True, slots=True)
class Delegation:
    """One request delegated by the authenticated Conversations backend (#596).

    Verified per request and never stored: the API keeps no user registry or group membership.
    """

    user_id: UUID
    # Ministries of the models currently granted by Conversations; may be empty.
    allowed_ministries: tuple[str, ...]
    # Ministry fixed at conversation creation; absent when loading the catalogue.
    ministry: str | None
    expires_at: datetime
    key_id: str
    audit_session_hash: str = field(default="", repr=False)
