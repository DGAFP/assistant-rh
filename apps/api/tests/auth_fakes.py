"""In-memory ports for deterministic auth service and HTTP contract tests."""

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from assistant_rh_api.core.auth import AuthContext, AuthService
from assistant_rh_api.core.models.auth import Delegation, Group
from assistant_rh_api.gateways.auth import SessionTokens
from assistant_rh_api.gateways.delegation import SignedDelegations, load_keys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


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


def individual_context(user_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), *, ministries=("matte", "mi"), ministry="matte"):
    """Delegated principal as the #596 adapter returns it, without signing an assertion."""
    expires = Clock().now() + timedelta(minutes=1)
    return AuthContext.delegated(Delegation(user_id, ministries, ministry, expires, "test-key", "d" * 64))


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Signer:
    """Test stand-in for the Conversations backend: one Ed25519 key per kid."""

    issuer = "https://conversations.test"

    def __init__(self, kid="conv-2026-10"):
        self.kid = kid
        self.key = Ed25519PrivateKey.generate()

    def jwk(self):
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "kid": self.kid, "x": b64url(raw), "use": "sig", "alg": "EdDSA"}

    def claims(self, user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", **changes):
        now = int(Clock().now().timestamp())
        claims = {
            "iss": self.issuer,
            "aud": "assistant-rh-api",
            "sub": user_id,
            "iat": now,
            "exp": now + 60,
            "jti": "jti-" + "0" * 28,
            "models": ["assistant-rh-matte", "assistant-rh-mi"],
            "audit_session": "e" * 64,
        }
        claims.update(changes)
        return {key: value for key, value in claims.items() if value is not ...}

    def sign(self, claims=None, *, header=None, payload=None):
        header = {"alg": "EdDSA", "kid": self.kid, "typ": "JWT"} if header is None else header
        encoded_header = b64url(json.dumps(header).encode())
        encoded_payload = b64url(payload if payload is not None else json.dumps(self.claims() if claims is None else claims).encode())
        signature = self.key.sign(f"{encoded_header}.{encoded_payload}".encode("ascii"))
        return f"{encoded_header}.{encoded_payload}.{b64url(signature)}"

    def verifier(self, *others):
        return SignedDelegations(load_keys(json.dumps({"keys": [s.jwk() for s in (self, *others)]})), issuer=self.issuer)


def delegated_service(signer):
    auth = service()
    auth.delegations = signer.verifier()
    return auth
