"""Bounded legacy password verification and random opaque session tokens."""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import UTC, datetime

import anyio


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class SessionTokens:
    def issue(self) -> str:
        return "arhs_" + secrets.token_urlsafe(32)

    def digest(self, token: str) -> str | None:
        if not re.fullmatch(r"arhs_[A-Za-z0-9_-]{43}", token):
            return None
        return hashlib.sha256(token.encode("ascii")).hexdigest()


class LegacyPasswords:
    """Matches Streamlit's PBKDF2-SHA256 format, without importing Streamlit.

    Unknown/ineligible groups and malformed hashes still perform one dummy KDF.
    Worker capacity bounds CPU work; cancellation never abandons running KDFs.
    """

    def __init__(self, *, workers: int = 4) -> None:
        self._limiter = anyio.CapacityLimiter(workers)

    async def verify(self, password: str, stored_hash: str | None) -> bool:
        # Keep ownership of worker capacity until the KDF actually finishes.
        # AnyIO shielding alone does not resist raw asyncio.Task.cancel().
        working = asyncio.create_task(anyio.to_thread.run_sync(self._verify, password, stored_hash, limiter=self._limiter))
        cancelled = None
        try:
            with anyio.CancelScope(shield=True):
                while not working.done():
                    try:
                        await asyncio.shield(working)
                    except asyncio.CancelledError as exc:
                        cancelled = exc
            if cancelled is not None:
                if not working.cancelled():
                    working.exception()
                raise cancelled
            return working.result()
        finally:
            await anyio.lowlevel.checkpoint_if_cancelled()

    @staticmethod
    def _verify(password: str, stored_hash: str | None) -> bool:
        salt, expected, iterations = b"dummy-salt-00000", bytes(32), 200_000
        valid = False
        try:
            algorithm, count, encoded_salt, encoded_digest = (stored_hash or "").split("$")
            parsed_salt = base64.b64decode(encoded_salt, validate=True)
            parsed_digest = base64.b64decode(encoded_digest, validate=True)
            parsed_count = int(count)
            if algorithm == "pbkdf2_sha256" and 200_000 <= parsed_count <= 1_000_000 and len(parsed_salt) == 16 and len(parsed_digest) == 32:
                salt, expected, iterations = parsed_salt, parsed_digest, parsed_count
                valid = True
        except (ValueError, TypeError):
            pass
        try:
            encoded_password = password.encode("utf-8")
        except UnicodeEncodeError:
            encoded_password, valid = b"", False
        actual = hashlib.pbkdf2_hmac("sha256", encoded_password, salt, iterations)
        return hmac.compare_digest(actual, expected) and valid
