"""In-memory ports for deterministic auth service and HTTP contract tests."""

from datetime import UTC, datetime

from assistant_rh_api.core.auth import AuthService
from assistant_rh_api.core.runtime import Group
from assistant_rh_api.gateways.auth import SessionTokens


class Clock:
    value = datetime(2026, 9, 8, tzinfo=UTC)

    def now(self):
        return self.value

    def monotonic(self):
        return self.value.timestamp()


class Groups:
    def __init__(self):
        self.rows = {"beta": Group("beta", "Beta", 1, True, False, "hash-password", ("matte", "mi"), "matte", "🏛️", "#0053b3", 3)}
        self.lookups = []

    async def get(self, slug):
        self.lookups.append(slug)
        return self.rows.get(slug)

    async def list_groups(self):
        return tuple(self.rows.values())


class Sessions:
    def __init__(self):
        self.rows = {}
        self.lookups = []

    async def create(self, session):
        self.rows[session.token_hash] = session

    async def get_active(self, token_hash, now):
        self.lookups.append(token_hash)
        return self.rows.get(token_hash)

    async def revoke(self, token_hash, now):
        self.rows.pop(token_hash, None)


class Passwords:
    def __init__(self):
        self.calls = []

    async def verify(self, password, stored_hash):
        self.calls.append((password, stored_hash))
        return password == "password" and stored_hash == "hash-password"


class Limiter:
    def __init__(self):
        self.calls = []

    async def acquire(self, source, slug):
        self.calls.append((source, slug))


def service():
    return AuthService(Groups(), Sessions(), Passwords(), SessionTokens(), Limiter(), Clock())
