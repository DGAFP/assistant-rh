"""Immutable auth values, independent of storage and transport."""

from dataclasses import dataclass, field
from datetime import datetime


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
