"""Public session use cases. No transport, crypto implementation, or environment."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from assistant_rh_api.core.errors import ApplicationError, DatabaseConflict
from assistant_rh_api.core.ministry_policy import MINISTRIES, valid_ministry_policy, validate_ministry_policy
from assistant_rh_api.core.models.auth import Group, Session
from assistant_rh_api.core.ports.auth import GroupStorePort, LoginLimiterPort, PasswordVerifierPort, SessionStorePort, SessionTokenPort
from assistant_rh_api.core.ports.system import ClockPort

SESSION_LIFETIME = timedelta(hours=8)


class InvalidCredentials(ApplicationError):
    code = "invalid_api_key"


class MinistryForbidden(ApplicationError):
    code = "ministry_forbidden"


class LoginRateLimited(ApplicationError):
    code = "rate_limit_exceeded"

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after


def eligible_group(group: Group | None) -> bool:
    return bool(group is not None and group.slug != "default" and group.visible and not group.is_admin and group.password_hash)


def public_group(group: Group | None) -> bool:
    return eligible_group(group) and group is not None and valid_ministry_policy(group)


@dataclass(frozen=True, slots=True)
class AuthContext:
    group: Group
    session: Session

    def authorize_ministry(self, ministry: str | None = None) -> str:
        selected = self.group.default_ministry if ministry is None else ministry
        if selected not in MINISTRIES or selected not in self.group.allowed_ministries:
            raise MinistryForbidden()
        return selected


@dataclass(frozen=True, slots=True)
class IssuedSession:
    access_token: str = field(repr=False)
    context: AuthContext


class AuthService:
    def __init__(
        self,
        groups: GroupStorePort,
        sessions: SessionStorePort,
        passwords: PasswordVerifierPort,
        tokens: SessionTokenPort,
        limiter: LoginLimiterPort,
        clock: ClockPort,
    ) -> None:
        self.groups = groups
        self.sessions = sessions
        self.passwords = passwords
        self.tokens = tokens
        self.limiter = limiter
        self.clock = clock

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
        if digest is None:
            raise InvalidCredentials()
        now = self.clock.now()
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

    async def logout(self, context: AuthContext) -> None:
        await self.sessions.revoke(context.session.token_hash, self.clock.now())

    def remaining(self, expires_at: datetime) -> int:
        return max(0, int((expires_at - self.clock.now()).total_seconds()))
