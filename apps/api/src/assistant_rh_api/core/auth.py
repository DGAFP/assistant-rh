"""Public session use cases. No transport, crypto implementation, or environment."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from assistant_rh_api.core.errors import DatabaseConflict, InvalidCredentials, MinistryForbidden
from assistant_rh_api.core.errors import LoginRateLimited as LoginRateLimited
from assistant_rh_api.core.ministry_policy import MINISTRIES, valid_ministry_policy, validate_ministry_policy
from assistant_rh_api.core.models.auth import Delegation, Group, Session
from assistant_rh_api.core.ports.auth import (
    DelegationReplayPort,
    DelegationVerifierPort,
    GroupStorePort,
    LoginLimiterPort,
    PasswordVerifierPort,
    SessionStorePort,
    SessionTokenPort,
)
from assistant_rh_api.core.ports.system import ClockPort

SESSION_LIFETIME = timedelta(hours=8)
# Audit label of delegated runs. "@" is outside the B4 slug alphabet, so it never names a group.
DELEGATED_PRINCIPAL = "@conversations"


def eligible_group(group: Group | None) -> bool:
    return bool(group is not None and group.slug != "default" and group.visible and not group.is_admin and group.password_hash)


def public_group(group: Group | None) -> bool:
    return eligible_group(group) and group is not None and valid_ministry_policy(group)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Exactly one principal: a B4 group session, or a delegated Conversations user (#596).

    The two kinds never convert into each other; only a delegation carries a user ID.
    """

    group: Group | None
    session: Session | None
    # Only a separately derived audit pseudonym may leave the auth boundary.
    audit_session_hash: str = field(default="", repr=False)
    delegation: Delegation | None = None

    def __post_init__(self) -> None:
        if self.delegation is None:
            valid = self.group is not None and self.session is not None
        else:
            valid = self.group is None and self.session is None
        if not valid:
            raise ValueError("an auth context is either a group session or a delegation")

    @classmethod
    def delegated(cls, delegation: Delegation) -> "AuthContext":
        return cls(None, None, delegation.audit_session_hash, delegation)

    @property
    def principal(self) -> Group | Delegation:
        principal = self.delegation or self.group
        assert principal is not None
        return principal

    @property
    def user_id(self) -> UUID | None:
        # Never read from an HTTP field: only a verified delegation names a user.
        return None if self.delegation is None else self.delegation.user_id

    @property
    def group_slug(self) -> str:
        return self.group.slug if self.group is not None else DELEGATED_PRINCIPAL

    @property
    def allowed_ministries(self) -> tuple[str, ...]:
        """Ministries whose runs, feedbacks and sources this request may reach."""
        if self.delegation is None:
            assert self.group is not None
            return self.group.allowed_ministries
        ministry = self.delegation.ministry
        allowed = self.delegation.allowed_ministries
        return allowed if ministry is None else tuple(m for m in allowed if m == ministry)

    def group_session(self) -> tuple[Group, Session]:
        """B4 session routes only; a delegation is not an API session."""
        if self.group is None or self.session is None:
            raise InvalidCredentials()
        return self.group, self.session

    def authorize_ministry(self, ministry: str | None = None) -> str:
        principal = self.principal
        default = principal.ministry if isinstance(principal, Delegation) else principal.default_ministry
        selected = default if ministry is None else ministry
        if selected not in MINISTRIES or selected not in self.allowed_ministries:
            raise MinistryForbidden()
        return selected


@dataclass(frozen=True, slots=True)
class IssuedSession:
    access_token: str = field(repr=False)
    context: AuthContext

    @property
    def session(self) -> Session:
        return self.context.group_session()[1]


class AuthService:
    def __init__(
        self,
        groups: GroupStorePort,
        sessions: SessionStorePort,
        passwords: PasswordVerifierPort,
        tokens: SessionTokenPort,
        limiter: LoginLimiterPort,
        clock: ClockPort,
        *,
        delegations: DelegationVerifierPort | None = None,
        replays: DelegationReplayPort | None = None,
    ) -> None:
        self.groups = groups
        self.sessions = sessions
        self.passwords = passwords
        self.tokens = tokens
        self.limiter = limiter
        self.clock = clock
        # None keeps delegation disabled: only B4 group sessions authenticate.
        self.delegations = delegations
        if delegations is not None and replays is None:
            raise ValueError("delegations require replay protection")
        self.replays = replays

    async def list_groups(self) -> tuple[Group, ...]:
        return tuple(sorted((g for g in await self.groups.list_groups() if public_group(g)), key=lambda g: (-g.priority, g.slug)))

    async def login(self, slug: str, password: str, source: str) -> IssuedSession:
        await self.limiter.acquire(source, slug)
        group = await self.groups.get(slug)
        eligible = public_group(group)
        valid = await self.passwords.verify(password, group.password_hash if eligible and group else None)
        if not valid or not eligible or group is None:
            raise InvalidCredentials()
        # Start the eight-hour lifetime after password work, not before it.
        now = self.clock.now()
        token = self.tokens.issue()
        digest = self.tokens.digest(token)
        if digest is None:
            raise RuntimeError("token issuer returned invalid session token")
        session = Session(digest, group.slug, now, now + SESSION_LIFETIME, group.password_hash or "", group.credential_revision)
        try:
            await self.sessions.create(session)
        except DatabaseConflict:
            # A concurrent password/policy change must not issue a stale session.
            raise InvalidCredentials() from None
        return IssuedSession(token, AuthContext(group, session))

    async def resolve(self, token: str) -> AuthContext:
        digest = self.tokens.digest(token)
        now = self.clock.now()
        if digest is None:
            return await self.resolve_delegation(token, now)
        session = await self.sessions.get_active(digest, now)
        if session is None or not session.created_at <= now < session.expires_at:
            raise InvalidCredentials()
        group = await self.groups.get(session.group_slug)
        if (
            not eligible_group(group)
            or group is None
            or group.credential_revision != session.credential_revision
            or group.password_hash != session.credential_hash
        ):
            raise InvalidCredentials()
        # Authenticate first: stale/revoked sessions remain 401 even with bad policy.
        validate_ministry_policy(group)
        return AuthContext(group, session)

    async def resolve_delegation(self, token: str, now: datetime) -> AuthContext:
        # Rights are those delegated now, never stored, so a withdrawal applies to the next request.
        delegation = None if self.delegations is None else self.delegations.verify(token, now)
        if delegation is None or not now < delegation.expires_at or not delegation.token_id:
            raise InvalidCredentials()
        # Claim only after verification: unsigned callers cannot fill the replay table.
        assert self.replays is not None
        if not await self.replays.claim(delegation.token_id, delegation.key_id, delegation.expires_at):
            raise InvalidCredentials()
        return AuthContext.delegated(delegation)

    async def logout(self, context: AuthContext) -> None:
        _, session = context.group_session()
        await self.sessions.revoke(session.token_hash, self.clock.now())

    def remaining(self, expires_at: datetime) -> int:
        return max(0, int((expires_at - self.clock.now()).total_seconds()))
