"""Per-request delegations signed by the Conversations backend (#596).

A compact JWS (RFC 7515) restricted to EdDSA/Ed25519 with pinned public keys:
no algorithm negotiation, no key URL, no symmetric secret shared with the API.
Removing a ``kid`` from the configured key set revokes it at the next restart.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from assistant_rh_api.core.catalog import model_ministry
from assistant_rh_api.core.models.auth import Delegation

MAX_TOKEN_LENGTH = 4096
# Conversations signs one assertion per API call; a longer window only widens replay.
MAX_LIFETIME_SECONDS = 120
# Tolerated advance of the signer's clock for iat/nbf. Expiry itself is strict.
CLOCK_SKEW_SECONDS = 30
MAX_MODELS = 16
DEFAULT_AUDIENCE = "assistant-rh-api"

SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
TOKEN_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")
AUDIT_HASH = re.compile(r"[0-9a-f]{64}")


def b64url(segment: str) -> bytes:
    if not SEGMENT.fullmatch(segment):
        raise ValueError("invalid base64url segment")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Duplicate members would let two parsers disagree on the delegated scope.
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def json_object(segment: str) -> dict[str, object]:
    value = json.loads(b64url(segment), object_pairs_hook=unique_object, parse_constant=lambda _: None)
    if not isinstance(value, dict):
        raise ValueError("JWS member is not an object")
    return value


def timestamp(value: object) -> int:
    # bool is an int subclass; floats are refused to keep a single canonical form.
    if type(value) is not int or value < 0:
        raise ValueError("invalid NumericDate")
    return value


def load_keys(jwks: str) -> dict[str, Ed25519PublicKey]:
    """Parse a JWKS of Ed25519 public keys. Private members are a configuration error."""
    document = json.loads(jwks, object_pairs_hook=unique_object)
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("delegation JWKS must contain keys")
    keys: dict[str, Ed25519PublicKey] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kty") != "OKP" or entry.get("crv") != "Ed25519":
            raise ValueError("delegation keys must be Ed25519 OKP keys")
        if "d" in entry:
            raise ValueError("delegation JWKS must not contain private keys")
        if entry.get("use", "sig") != "sig" or entry.get("alg", "EdDSA") != "EdDSA":
            raise ValueError("delegation keys must be EdDSA signature keys")
        kid, x = entry.get("kid"), entry.get("x")
        if not isinstance(kid, str) or not KEY_ID.fullmatch(kid) or kid in keys or not isinstance(x, str):
            raise ValueError("delegation keys need unique valid kid values")
        raw = b64url(x)
        if len(raw) != 32:
            raise ValueError("invalid Ed25519 public key")
        keys[kid] = Ed25519PublicKey.from_public_bytes(raw)
    return keys


class SignedDelegations:
    def __init__(self, keys: Mapping[str, Ed25519PublicKey], *, issuer: str, audience: str = DEFAULT_AUDIENCE) -> None:
        if not keys or not issuer or not audience:
            raise ValueError("delegation verification needs keys, an issuer and an audience")
        self._keys = dict(keys)
        self._issuer = issuer
        self._audience = audience

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "SignedDelegations | None":
        jwks = environ.get("CONVERSATIONS_DELEGATION_JWKS", "").strip()
        issuer = environ.get("CONVERSATIONS_DELEGATION_ISSUER", "").strip()
        if not jwks and not issuer:
            return None
        if not jwks or not issuer:
            raise ValueError("CONVERSATIONS_DELEGATION_JWKS and CONVERSATIONS_DELEGATION_ISSUER are configured together")
        audience = environ.get("ASSISTANT_RH_API_AUDIENCE", "").strip() or DEFAULT_AUDIENCE
        return cls(load_keys(jwks), issuer=issuer, audience=audience)

    def verify(self, token: str, now: datetime) -> Delegation | None:
        try:
            return self._verify(token, now)
        except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, InvalidSignature):
            # One indistinct refusal: the reason is never echoed to the caller or logged.
            return None

    def _verify(self, token: str, now: datetime) -> Delegation | None:
        if len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            return None
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json_object(encoded_header)
        if set(header) - {"alg", "kid", "typ"} or header.get("alg") != "EdDSA" or header.get("typ", "JWT") != "JWT":
            return None
        key = self._keys.get(header.get("kid")) if isinstance(header.get("kid"), str) else None
        signature = b64url(encoded_signature)
        if key is None or len(signature) != 64:
            return None
        # Verify before trusting any claim.
        key.verify(signature, f"{encoded_header}.{encoded_payload}".encode("ascii"))
        claims = json_object(encoded_payload)
        return self._delegation(claims, str(header["kid"]), now)

    def _delegation(self, claims: dict[str, object], kid: str, now: datetime) -> Delegation | None:
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if claims.get("iss") != self._issuer or self._audience not in audiences:
            return None
        issued, expires = timestamp(claims.get("iat")), timestamp(claims.get("exp"))
        not_before = timestamp(claims["nbf"]) if "nbf" in claims else issued
        current = int(now.timestamp())
        if not (issued < expires <= issued + MAX_LIFETIME_SECONDS) or current >= expires:
            return None
        if max(issued, not_before) > current + CLOCK_SKEW_SECONDS:
            return None
        jti = claims.get("jti")
        if not isinstance(jti, str) or not TOKEN_ID.fullmatch(jti):
            return None
        subject = claims.get("sub")
        # The stable Conversations user ID, in canonical form only.
        if not isinstance(subject, str) or str(UUID(subject)) != subject:
            return None
        models = claims.get("models")
        if not isinstance(models, list) or len(models) > MAX_MODELS or len(set(map(str, models))) != len(models):
            return None
        ministries = tuple(model_ministry(model) if isinstance(model, str) else None for model in models)
        if None in ministries:
            # Fail closed on catalogue drift rather than guessing what an unknown model grants.
            return None
        ministry = claims.get("ministry")
        if ministry is not None and ministry not in ministries:
            return None
        audit = claims.get("audit_session", "")
        if not isinstance(audit, str) or (audit and not AUDIT_HASH.fullmatch(audit)):
            return None
        return Delegation(
            user_id=UUID(subject),
            allowed_ministries=tuple(sorted(m for m in ministries if m is not None)),
            ministry=ministry if isinstance(ministry, str) else None,
            expires_at=datetime.fromtimestamp(expires, UTC),
            key_id=kid,
            audit_session_hash=audit,
        )
