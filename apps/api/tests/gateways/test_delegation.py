import json
from datetime import timedelta
from uuid import UUID

import pytest
from assistant_rh_api.gateways.delegation import SignedDelegations, load_keys

from apps.api.tests.auth_fakes import Clock, Signer, b64url

NOW = Clock().now()


def test_valid_assertion_yields_scope_without_exposing_audit_hash():
    signer = Signer()
    delegation = signer.verifier().verify(signer.sign(signer.claims(ministry="mi")), NOW)
    assert delegation.user_id == UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert delegation.allowed_ministries == ("matte", "mi")
    assert delegation.ministry == "mi"
    assert delegation.expires_at == NOW + timedelta(seconds=60)
    assert delegation.key_id == signer.kid
    assert delegation.audit_session_hash == "e" * 64
    assert "e" * 64 not in repr(delegation)


def test_empty_grant_is_a_valid_delegation_without_corpus():
    signer = Signer()
    delegation = signer.verifier().verify(signer.sign(signer.claims(models=[], audit_session=...)), NOW)
    assert delegation.allowed_ministries == () and delegation.ministry is None and delegation.audit_session_hash == ""


def test_audience_may_be_a_list_and_nbf_tolerates_small_signer_clock_advance():
    signer = Signer()
    now = int(NOW.timestamp())
    claims = signer.claims(aud=["other", "assistant-rh-api"], iat=now + 20, nbf=now + 25, exp=now + 80)
    assert signer.verifier().verify(signer.sign(claims), NOW) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.test"},
        {"iss": ...},
        {"aud": "other-api"},
        {"aud": ["other-api"]},
        {"aud": ...},
        {"exp": int(NOW.timestamp())},  # expiry is strict
        {"exp": int(NOW.timestamp()) - 1},
        {"exp": int(NOW.timestamp()) + 121},  # lifetime above two minutes
        {"exp": ...},
        {"iat": ...},
        {"iat": int(NOW.timestamp()) + 31, "exp": int(NOW.timestamp()) + 90},  # issued in the future
        {"nbf": int(NOW.timestamp()) + 31},
        {"exp": True},
        {"exp": float(NOW.timestamp() + 60)},
        {"exp": str(int(NOW.timestamp()) + 60)},
        {"jti": ...},
        {"jti": "short"},
        {"jti": "x" * 129},
        {"sub": ...},
        {"sub": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
        {"sub": "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"},
        {"sub": "user@example.org"},
        {"sub": 12},
        {"models": ...},
        {"models": "assistant-rh-matte"},
        {"models": ["assistant-rh-matte", "assistant-rh-matte"]},
        {"models": ["assistant-rh-unknown"]},
        {"models": ["assistant-rh"]},
        {"models": [{"id": "assistant-rh-matte"}]},
        {"models": ["assistant-rh-matte"] + [f"m{i}" for i in range(16)]},
        {"ministry": "masa"},
        {"ministry": "unknown"},
        {"ministry": 1},
        {"audit_session": "E" * 64},
        {"audit_session": "e" * 63},
        {"audit_session": 1},
    ],
)
def test_invalid_claims_are_refused(changes):
    signer = Signer()
    assert signer.verifier().verify(signer.sign(signer.claims(**changes)), NOW) is None


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "kid": "conv-2026-10"},
        {"alg": "HS256", "kid": "conv-2026-10"},
        {"alg": "ES256", "kid": "conv-2026-10"},
        {"alg": "EdDSA"},
        {"alg": "EdDSA", "kid": "unknown"},
        {"alg": "EdDSA", "kid": 1},
        {"alg": "EdDSA", "kid": "conv-2026-10", "typ": "at+jwt"},
        {"alg": "EdDSA", "kid": "conv-2026-10", "crit": ["exp"]},
        {"alg": "EdDSA", "kid": "conv-2026-10", "jku": "https://attacker.test/jwks"},
    ],
)
def test_headers_outside_the_pinned_profile_are_refused(header):
    signer = Signer()
    assert signer.verifier().verify(signer.sign(header=header), NOW) is None


def test_tampered_foreign_revoked_and_malformed_assertions_are_refused():
    signer, other = Signer(), Signer()
    token = signer.sign()
    head, payload, signature = token.split(".")
    forged = b64url(json.dumps(signer.claims(models=["assistant-rh-masa"])).encode())
    for candidate in (
        f"{head}.{forged}.{signature}",  # scope changed after signing
        other.sign(),  # same kid, key not pinned
        f"{head}.{payload}.{signature}=",
        f"{head}.{payload}",
        f"{head}.{payload}.{signature}.x",
        "",
        "a" * 5000,
        "arhs_" + "a" * 43,
        f"{head}.{payload}.{b64url(b'x' * 63)}",
        f"{head}.é{payload}.{signature}",
    ):
        assert signer.verifier().verify(candidate, NOW) is None
    # Rotation: both keys valid together, then removing a kid revokes it.
    rotated = Signer("conv-2026-11")
    assert signer.verifier(rotated).verify(rotated.sign(), NOW) is not None
    assert rotated.verifier().verify(token, NOW) is None


def test_duplicate_members_cannot_smuggle_a_second_scope():
    signer = Signer()
    claims = json.dumps(signer.claims())[:-1] + ', "models": ["assistant-rh-masa"]}'
    assert signer.verifier().verify(signer.sign(payload=claims.encode()), NOW) is None


@pytest.mark.parametrize(
    "jwks",
    [
        "{}",
        '{"keys": []}',
        '{"keys": [{"kty": "RSA", "kid": "a", "n": "x", "e": "AQAB"}]}',
        '{"keys": [{"kty": "OKP", "crv": "X25519", "kid": "a", "x": "' + "A" * 43 + '"}]}',
        '{"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "' + "A" * 43 + '"}]}',
        '{"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "a", "x": "AAAA"}]}',
        '{"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "a", "x": "' + "A" * 43 + '", "use": "enc"}]}',
    ],
)
def test_invalid_key_sets_fail_at_startup(jwks):
    with pytest.raises(ValueError):
        load_keys(jwks)


def test_private_or_duplicate_keys_fail_at_startup():
    signer = Signer()
    with pytest.raises(ValueError, match="private"):
        load_keys(json.dumps({"keys": [{**signer.jwk(), "d": "secret"}]}))
    with pytest.raises(ValueError):
        load_keys(json.dumps({"keys": [signer.jwk(), signer.jwk()]}))


def test_environment_enables_delegation_only_when_fully_configured():
    signer = Signer()
    jwks = json.dumps({"keys": [signer.jwk()]})
    assert SignedDelegations.from_environment({}) is None
    with pytest.raises(ValueError):
        SignedDelegations.from_environment({"CONVERSATIONS_DELEGATION_JWKS": jwks})
    with pytest.raises(ValueError):
        SignedDelegations.from_environment({"CONVERSATIONS_DELEGATION_ISSUER": signer.issuer})
    verifier = SignedDelegations.from_environment({"CONVERSATIONS_DELEGATION_JWKS": jwks, "CONVERSATIONS_DELEGATION_ISSUER": signer.issuer})
    assert verifier.verify(signer.sign(), NOW) is not None
    custom = SignedDelegations.from_environment(
        {"CONVERSATIONS_DELEGATION_JWKS": jwks, "CONVERSATIONS_DELEGATION_ISSUER": signer.issuer, "ASSISTANT_RH_API_AUDIENCE": "staging-api"}
    )
    assert custom.verify(signer.sign(), NOW) is None
    assert custom.verify(signer.sign(signer.claims(aud="staging-api")), NOW) is not None
