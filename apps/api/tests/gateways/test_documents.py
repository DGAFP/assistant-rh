import asyncio
import logging

import httpx
import pytest
from assistant_rh_api.core.errors.documents import DocumentNotFound, DocumentUnavailable
from assistant_rh_api.gateways.documents import DocumentStorageSettings, S3Documents

pytestmark = pytest.mark.anyio
SETTINGS = DocumentStorageSettings(access_key="test-access-key", secret_key="test-secret-key")


class GeneratedPDF(httpx.AsyncByteStream):
    """Large virtual object: the fixture itself never allocates the whole PDF."""

    def __init__(self, size=32 * 1024 * 1024, *, block=False):
        self.size, self.block = size, block
        self.closed = False
        self.read = 0

    async def __aiter__(self):
        for offset in range(0, self.size, 8192):
            if offset and self.block:
                await asyncio.Event().wait()
            size = min(8192, self.size - offset)
            self.read += size
            yield (b"%PDF-" + b"x" * (size - 5)) if offset == 0 else b"x" * size

    async def aclose(self):
        self.closed = True


async def test_large_pdf_is_incremental_bounded_and_never_logs_private_request(caplog):
    stream = GeneratedPDF()

    async def s3(request):
        assert "test-access-key" in request.headers["authorization"]
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Length": str(stream.size)}, stream=stream)

    reader = S3Documents(httpx.MockTransport(s3), SETTINGS)
    caplog.set_level(logging.DEBUG)
    async with reader.open("matte/confidential.pdf") as content:
        assert stream.read <= 65536 and content.size == stream.size
        count = 0
        async for chunk in content.chunks:
            assert len(chunk) <= 65536
            count += len(chunk)
        assert count == stream.size
    assert stream.closed
    assert all(secret not in caplog.text for secret in ("confidential.pdf", "test-access-key", "test-secret-key", "AWS4-HMAC", "s3.fr-par"))
    logging.getLogger("botocore.auth").debug("unrelated signing diagnostic")
    assert "unrelated signing diagnostic" in caplog.text


@pytest.mark.parametrize(
    "key",
    [
        "https://evil.test/a.pdf",
        "s3://bucket/mi/a.pdf",
        "/mi/a.pdf",
        "mi/../a.pdf",
        "mi//a.pdf",
        "mi/a.docx",
        "mi/a.pdf\r\nHeader",
        "mi/a\\b.pdf",
        "unknown/a.pdf",
        "mi/" + "a" * 1024 + ".pdf",
    ],
)
async def test_untrusted_locations_never_reach_transport(key):
    async def forbidden(_):
        raise AssertionError("Invalid location reached transport")

    with pytest.raises(DocumentNotFound):
        async with S3Documents(httpx.MockTransport(forbidden), SETTINGS).open(key):
            pass


@pytest.mark.parametrize(
    "status,error", [(302, DocumentUnavailable), (403, DocumentUnavailable), (404, DocumentNotFound), (500, DocumentUnavailable)]
)
async def test_storage_failure_and_redirect_never_followed_and_body_closed(status, error):
    stream = GeneratedPDF()
    calls = []

    async def s3(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/secret"}, stream=stream)

    with pytest.raises(error):
        async with S3Documents(httpx.MockTransport(s3), SETTINGS).open("mi/a.pdf"):
            pass
    assert stream.closed and stream.read == 0 and len(calls) == 1


@pytest.mark.parametrize(
    "headers", [{}, {"Content-Length": "invalid"}, {"Content-Length": "-1"}, {"Content-Length": "50", "Content-Encoding": "gzip"}]
)
async def test_invalid_storage_headers_rejected(headers):
    stream = GeneratedPDF(50)

    async def s3(_):
        return httpx.Response(200, headers=headers, stream=stream)

    with pytest.raises(DocumentUnavailable):
        async with S3Documents(httpx.MockTransport(s3), SETTINGS).open("mi/a.pdf"):
            pass
    assert stream.closed


async def test_non_pdf_signature_rejected():
    async def s3(_):
        return httpx.Response(200, headers={"Content-Length": "6"}, stream=httpx.ByteStream(b"<html>"))

    with pytest.raises(DocumentNotFound):
        async with S3Documents(httpx.MockTransport(s3), SETTINGS).open("mi/a.pdf"):
            pass


async def test_cancellation_and_timeout_close_upstream():
    for timeout in (False, True):
        stream = GeneratedPDF(block=True)

        async def s3(_):
            return httpx.Response(200, headers={"Content-Length": str(stream.size)}, stream=stream)

        async def read():
            async with S3Documents(httpx.MockTransport(s3), SETTINGS).open("mi/a.pdf") as content:
                async for _ in content.chunks:
                    pass

        if timeout:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    await read()
        else:
            task = asyncio.create_task(read())
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed


@pytest.mark.parametrize("declared_size", [10, 200000])
async def test_wrong_content_length_aborts_and_closes(declared_size):
    stream = GeneratedPDF(100000)

    async def s3(_):
        return httpx.Response(200, headers={"Content-Length": str(declared_size)}, stream=stream)

    with pytest.raises(DocumentUnavailable):
        async with S3Documents(httpx.MockTransport(s3), SETTINGS).open("mi/a.pdf") as content:
            async for _ in content.chunks:
                pass
    assert stream.closed
