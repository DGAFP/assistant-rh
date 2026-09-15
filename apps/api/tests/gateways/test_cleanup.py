"""Exercise HTTPX EOF cleanup through HTTPCore without opening a socket."""

import asyncio
import json
from dataclasses import replace

import anyio
import httpcore
import httpx
import pytest
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.inference import ChatRequest, Message
from assistant_rh_api.gateways.chat import ChatGateway

from .conftest import ALBERT, POLICY, WireStream

pytestmark = pytest.mark.anyio
REQUEST = ChatRequest((Message("user", "question"),))
# Capture before the autouse fixture disables real HTTP transports.
ORIGINAL_HANDLE = httpx.AsyncHTTPTransport.handle_async_request


class ClosingNetwork:
    def __init__(self, streaming):
        payload = (
            b'data: {"choices": [{"delta": {"content": "answer"}}]}\n\ndata: [DONE]\n\n'
            if streaming
            else json.dumps({"choices": [{"message": {"content": "answer"}}]}).encode()
        )
        self.data = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload
        self.closed = False
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def read(self, *args, **kwargs):
        data, self.data = self.data, b""
        return data

    async def write(self, *args, **kwargs):
        pass

    async def start_tls(self, *args, **kwargs):
        return self

    def get_extra_info(self, name):
        return None

    async def aclose(self):
        self.closing.set()
        await self.release.wait()
        self.closed = True


class NetworkBackend:
    def __init__(self, network):
        self.network = network

    async def connect_tcp(self, *args, **kwargs):
        return self.network


@pytest.mark.parametrize(
    "mode,streaming", [("timeout", False), ("cancel", False), ("repeated_cancel", False), ("cancel", True), ("repeated_cancel", True)]
)
async def test_cleanup_releases_httpcore_request_before_propagating_cancellation(mode, streaming, monkeypatch):
    network = ClosingNetwork(streaming)
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(network_backend=NetworkBackend(network))
    # Restore only this instance, whose backend above cannot open a real socket.
    monkeypatch.setattr(transport, "handle_async_request", ORIGINAL_HANDLE.__get__(transport, httpx.AsyncHTTPTransport))
    policy = replace(POLICY, timeout=0.5, total_timeout=0.1 if mode == "timeout" else 1)
    async with httpx.AsyncClient(transport=transport) as client:
        gateway = ChatGateway(client, ALBERT, policy=policy)

        async def run():
            if streaming:
                async with gateway.stream(REQUEST) as events:
                    return [event async for event in events]
            return await gateway.complete(REQUEST)

        task = asyncio.create_task(run())
        await network.closing.wait()
        if mode == "timeout":
            await asyncio.sleep(0.12)
        else:
            task.cancel()
            await asyncio.sleep(0)
            if mode == "repeated_cancel":
                task.cancel()
                await asyncio.sleep(0)
        network.release.set()
        if mode == "timeout":
            with pytest.raises(InferenceFailure) as caught:
                await task
            assert caught.value.attempts[-1].error == "timeout"
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert network.closed
        assert not transport._pool._requests


@pytest.mark.parametrize("streaming", [False, True])
async def test_asgi_cancellation_during_close_finishes_cleanup(streaming):
    class AwaitedClose(WireStream):
        def __init__(self, *chunks):
            super().__init__(*chunks)
            self.closing = asyncio.Event()

        async def aclose(self):
            self.closing.set()
            await anyio.sleep(0.01)
            self.closed = True

    raw = (
        b'data: {"choices": [{"delta": {"content": "answer"}}]}\n\ndata: [DONE]\n\n'
        if streaming
        else b'{"choices": [{"message": {"content": "answer"}}]}'
    )
    body = AwaitedClose(raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = ChatGateway(client, ALBERT, policy=POLICY)
        async with anyio.create_task_group() as group:

            async def cancel_at_close():
                await body.closing.wait()
                group.cancel_scope.cancel()

            group.start_soon(cancel_at_close)
            if streaming:
                async with gateway.stream(REQUEST) as events:
                    async for _ in events:
                        pass
            else:
                await gateway.complete(REQUEST)
            pytest.fail("cancellation during cleanup must propagate before returning success")
        assert body.closed


async def test_cancelled_stuck_cleanup_finishes_within_its_budget():
    class StuckClose(WireStream):
        def __init__(self):
            super().__init__(b'{"choices": [{"message": {"content": "answer"}}]}')
            self.closing = asyncio.Event()
            self.finished = asyncio.Event()

        async def aclose(self):
            self.closing.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.finished.set()

    body = StuckClose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = ChatGateway(client, ALBERT, policy=replace(POLICY, timeout=0.02))
        async with asyncio.timeout(1):
            task = asyncio.create_task(gateway.complete(REQUEST))
            await body.closing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert body.finished.is_set()
