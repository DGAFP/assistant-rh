"""Deterministic wire fakes; real HTTP is forbidden in the gateway suite."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy

ALBERT = Endpoint("albert", "openweight-large", "https://albert.test/v1", "private-albert-key")
SCALEWAY = Endpoint("scaleway", "llama-3.1-70b-instruct", "https://scaleway.test/v1", "private-scaleway-key")
POLICY = RequestPolicy(timeout=0.1, total_timeout=0.5, max_attempts=1, retry_delay=0)


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("gateway tests must inject MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


class WireStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes | Exception, wait_after: bool = False) -> None:
        self.chunks = chunks
        self.wait_after = wait_after
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
        if self.wait_after:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True
