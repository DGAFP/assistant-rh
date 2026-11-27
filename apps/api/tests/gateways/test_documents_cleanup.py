"""Exercise private PDF cleanup through HTTPX/HTTPCore without a real socket."""

import asyncio
import logging

import anyio
import httpcore
import httpx
import pytest
from assistant_rh_api.gateways.documents import DocumentStorageSettings, S3Documents

pytestmark = pytest.mark.anyio
SETTINGS = DocumentStorageSettings(access_key="synthetic", secret_key="synthetic")
# Capture before the gateway fixture disables real HTTP transports.
ORIGINAL_HANDLE = httpx.AsyncHTTPTransport.handle_async_request


class PDFNetwork:
    def __init__(self):
        self.data = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n%PDF-"
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.reading = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.pause_read = False

    async def read(self, *args, **kwargs):
        if not self.data and self.pause_read:
            self.reading.set()
            await self.disconnected.wait()
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
        logging.getLogger("httpcore.connection").debug("close private-document.pdf synthetic")
        self.closed = True
        self.disconnected.set()


class PDFBackend:
    def __init__(self, network):
        self.network = network

    async def connect_tcp(self, *args, **kwargs):
        return self.network


@pytest.fixture
async def pdf_transport(monkeypatch):
    network = PDFNetwork()
    async with httpx.AsyncHTTPTransport() as transport:
        transport._pool = httpcore.AsyncConnectionPool(network_backend=PDFBackend(network))
        # Restore only this instance: its backend cannot open a real socket.
        monkeypatch.setattr(transport, "handle_async_request", ORIGINAL_HANDLE.__get__(transport, httpx.AsyncHTTPTransport))
        try:
            yield network, transport
        finally:
            network.release.set()


async def consume(reader, phase):
    async with reader.open("mi/private-document.pdf") as content:
        if phase == "eof":
            assert b"".join([part async for part in content.chunks]) == b"%PDF-"


@pytest.mark.parametrize("phase", ["eof", "early_exit"])
@pytest.mark.parametrize("mode", ["normal", "cancel", "repeated_cancel", "timeout"])
async def test_httpcore_cleanup_finishes_before_cancellation_propagates(pdf_transport, phase, mode, caplog):
    network, transport = pdf_transport
    reader = S3Documents(transport, SETTINGS)
    caplog.set_level(logging.DEBUG)
    deadline = asyncio.timeout(None)

    async def run():
        async with deadline:
            await consume(reader, phase)

    async with asyncio.timeout(2):
        task = asyncio.create_task(run())
        try:
            await network.closing.wait()
            if mode == "timeout":
                deadline.reschedule(asyncio.get_running_loop().time())
                while not task.cancelling():
                    await asyncio.sleep(0)
            elif mode != "normal":
                task.cancel()
                await asyncio.sleep(0)
                if mode == "repeated_cancel":
                    task.cancel()
                    await asyncio.sleep(0)
            network.release.set()
            if mode == "normal":
                await task
            else:
                with pytest.raises(TimeoutError if mode == "timeout" else asyncio.CancelledError):
                    await task
            assert network.closed
            assert not transport._pool._requests
            assert not transport._pool.connections
            assert "private-document.pdf" not in caplog.text
        finally:
            network.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["eof", "early_exit"])
async def test_anyio_cancellation_during_httpcore_cleanup(pdf_transport, phase):
    network, transport = pdf_transport
    reader = S3Documents(transport, SETTINGS)
    async with asyncio.timeout(2), anyio.create_task_group() as group:

        async def cancel_at_close():
            await network.closing.wait()
            group.cancel_scope.cancel()
            network.release.set()

        group.start_soon(cancel_at_close)
        await consume(reader, phase)
        pytest.fail("cancellation during cleanup must propagate before success")
    assert network.closed
    assert not transport._pool._requests


@pytest.mark.parametrize("mode", ["cancel", "repeated_cancel", "timeout"])
async def test_cancellation_during_pending_body_read_owns_httpcore_cleanup(pdf_transport, mode):
    network, transport = pdf_transport
    network.data = b"HTTP/1.1 200 OK\r\nContent-Length: 131072\r\nConnection: close\r\n\r\n%PDF-" + b"x" * (65536 - 5)
    network.pause_read = True
    reader = S3Documents(transport, SETTINGS)
    deadline = asyncio.timeout(None)

    async def run():
        async with deadline, reader.open("mi/private-document.pdf") as content:
            async for _ in content.chunks:
                pass

    async with asyncio.timeout(2):
        task = asyncio.create_task(run())
        try:
            await network.reading.wait()
            if mode == "timeout":
                deadline.reschedule(asyncio.get_running_loop().time())
            else:
                task.cancel()
            await network.closing.wait()
            if mode == "repeated_cancel":
                task.cancel()
                await asyncio.sleep(0)
            # The cancelled reader must retain ownership until close completes.
            assert not task.done()
            network.release.set()
            with pytest.raises(TimeoutError if mode == "timeout" else asyncio.CancelledError):
                await task
            assert network.closed
            assert not transport._pool._requests
            assert not transport._pool.connections
        finally:
            network.release.set()
            network.disconnected.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_stuck_document_cleanup_is_bounded(monkeypatch):
    class StuckClose(httpx.AsyncByteStream):
        def __init__(self):
            self.closing = asyncio.Event()
            self.finished = asyncio.Event()

        async def __aiter__(self):
            yield b"%PDF-"

        async def aclose(self):
            self.closing.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.finished.set()

    body = StuckClose()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Length": "5"}, stream=body))
    reader = S3Documents(transport, SETTINGS)
    monkeypatch.setattr("assistant_rh_api.gateways.documents.CLOSE_TIMEOUT_SECONDS", 0.02)
    async with asyncio.timeout(2):
        task = asyncio.create_task(consume(reader, "eof"))
        await body.closing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.finished.is_set()
