import asyncio
import base64
import hashlib
import threading

import anyio
import pytest
from assistant_rh_api.gateways.auth import LegacyPasswords, SessionTokens

pytestmark = pytest.mark.anyio


async def test_password_matches_legacy_streamlit_format():
    salt = b"synthetic-salt12"
    salt = salt.ljust(16, b"0")
    digest = hashlib.pbkdf2_hmac("sha256", b"synthetic-password", salt, 200_000)
    stored = "pbkdf2_sha256$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    verifier = LegacyPasswords()
    assert await verifier.verify("synthetic-password", stored)
    assert not await verifier.verify("wrong", stored)


@pytest.mark.parametrize("stored", [None, "", "broken", "pbkdf2_sha256$999999999999$YQ==$YQ==", "pbkdf2_sha256$200000$!!!$!!!"])
async def test_invalid_hash_performs_bounded_dummy_kdf(stored, monkeypatch):
    calls = []

    def kdf(algorithm, password, salt, iterations):
        calls.append((algorithm, iterations, len(salt)))
        return bytes(32)

    monkeypatch.setattr(hashlib, "pbkdf2_hmac", kdf)
    assert not await LegacyPasswords().verify("password", stored)
    assert calls == [("sha256", 200000, 16)]


async def test_tokens_have_random_entropy_and_are_digest_indexed():
    tokens = SessionTokens()
    first, second = tokens.issue(), tokens.issue()
    assert first != second and len(first) == 48
    assert len(base64.urlsafe_b64decode(first[5:] + "=")) == 32
    assert tokens.digest(first) == hashlib.sha256(first.encode()).hexdigest()


async def test_raw_repeated_cancellation_retains_worker_capacity_until_kdf_finishes(monkeypatch):
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def kdf(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            assert release.wait(timeout=3), "test did not release the synthetic KDF"
            return bytes(32)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(hashlib, "pbkdf2_hmac", kdf)
    verifier = LegacyPasswords(workers=4)
    first = [asyncio.create_task(verifier.verify("synthetic", None)) for _ in range(4)]
    second = []
    try:
        async with asyncio.timeout(2):
            while active != 4:
                await asyncio.sleep(0.001)
        for task in first:
            task.cancel()
        await asyncio.sleep(0)
        for task in first:
            task.cancel()
        second = [asyncio.create_task(verifier.verify("synthetic", None)) for _ in range(4)]
        await asyncio.sleep(0.02)
        assert peak == 4 and not any(task.done() for task in first)
    finally:
        release.set()
        outcomes = await asyncio.gather(*first, *second, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes[:4])
    assert outcomes[4:] == [False] * 4 and active == 0


async def test_anyio_cancellation_propagates_after_password_worker_cleanup(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def kdf(*args):
        started.set()
        try:
            assert release.wait(timeout=3)
            return bytes(32)
        finally:
            finished.set()

    monkeypatch.setattr(hashlib, "pbkdf2_hmac", kdf)
    with anyio.CancelScope() as scope:

        async def cancel():
            while not started.is_set():
                await asyncio.sleep(0.001)
            scope.cancel()
            release.set()

        task = asyncio.create_task(cancel())
        try:
            await LegacyPasswords().verify("synthetic", None)
            pytest.fail("cancelled verification must not return an authentication result")
        finally:
            release.set()
    await task
    assert finished.is_set()
