"""Signed server-side S3 reads. No presigning, redirects, or user-supplied URLs."""

import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from assistant_rh_api.core.errors.documents import DocumentNotFound, DocumentUnavailable
from assistant_rh_api.core.ports.documents import DocumentContent
from assistant_rh_api.gateways.http import CancellationSafeStream

CHUNK_BYTES = 64 * 1024
CLOSE_TIMEOUT_SECONDS = 5.0
_PRIVATE_TRANSFER = ContextVar("private_document_transfer", default=False)


class _PrivateTransferLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_TRANSFER.get()


# SDK DEBUG logs include canonical paths/signatures; network DEBUG logs can
# include upstream headers. Suppress only this transfer's task-local records,
# without changing logging levels or hiding other concurrent provider requests.
for _logger in ("botocore.auth", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
    logging.getLogger(_logger).addFilter(_PrivateTransferLogs())


@contextmanager
def _private_transfer():
    token = _PRIVATE_TRANSFER.set(True)
    try:
        yield
    finally:
        _PRIVATE_TRANSFER.reset(token)


@dataclass(frozen=True, slots=True)
class DocumentStorageSettings:
    region: str = "fr-par"
    bucket: str = field(default="assistant-rh-sources-pdf", repr=False)
    access_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "DocumentStorageSettings":
        return cls(
            environment.get("SCW_DEFAULT_REGION", "fr-par"),
            environment.get("SCW_BUCKET_SOURCES_PDF", "assistant-rh-sources-pdf"),
            environment.get("SCW_ACCESS_KEY", ""),
            environment.get("SCW_SECRET_KEY", ""),
        )


class S3Documents:
    def __init__(self, transport: httpx.AsyncBaseTransport, settings: DocumentStorageSettings) -> None:
        self.transport = transport
        self.settings = settings

    @asynccontextmanager
    async def open(self, storage_path: str) -> AsyncIterator[DocumentContent]:
        with _private_transfer():
            # Only the PDF dropzone key form is supported, never an external URI/path.
            if (
                len(storage_path) > 1024
                or not re.fullmatch(r"(?:matte|mi|masa|mso)/[^\x00-\x1f\x7f]+\.[pP][dD][fF]", storage_path)
                or any(part in ("", ".", "..") for part in storage_path.split("/"))
                or "\\" in storage_path
            ):
                raise DocumentNotFound()
            settings = self.settings
            if (
                settings.region not in ("fr-par", "nl-ams", "pl-waw")
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", settings.bucket)
                or not settings.access_key
                or not settings.secret_key
            ):
                raise DocumentUnavailable()
            url = f"https://s3.{settings.region}.scw.cloud/{settings.bucket}/{quote(storage_path, safe='/')}"
            signed = AWSRequest(method="GET", url=url, headers={"Accept-Encoding": "identity"})
            S3SigV4Auth(Credentials(settings.access_key, settings.secret_key), "s3", settings.region).add_auth(signed)
            request = httpx.Request(
                "GET",
                url,
                headers=dict(signed.headers),
                extensions={
                    "timeout": {"connect": 5.0, "read": 15.0, "write": 5.0, "pool": 5.0},
                },
            )
            response = None
            try:
                # Direct transport avoids AsyncClient's INFO log of the private object URL.
                # The transport has no cookies, redirects, retries or environment proxy.
                response = await self.transport.handle_async_request(request)
                # HTTPX also closes at EOF, before our finally block. Protect
                # that path as well as explicit close on errors/early exit.
                assert isinstance(response.stream, httpx.AsyncByteStream)
                response.stream = CancellationSafeStream(response.stream, CLOSE_TIMEOUT_SECONDS)
                if response.status_code == 404:
                    raise DocumentNotFound()
                if response.status_code != 200:
                    raise DocumentUnavailable()
                try:
                    size = int(response.headers["Content-Length"])
                except (KeyError, ValueError):
                    raise DocumentUnavailable() from None
                if size < 5 or response.headers.get("Content-Encoding", "identity") != "identity":
                    raise DocumentUnavailable()
                stream = response.aiter_raw(CHUNK_BYTES)
                first = await anext(stream, b"")
                if not first.startswith(b"%PDF-"):
                    raise DocumentNotFound()

                async def chunks() -> AsyncIterator[bytes]:
                    received = len(first)
                    if received > size:
                        raise DocumentUnavailable()
                    yield first
                    async for part in stream:
                        received += len(part)
                        if received > size:
                            raise DocumentUnavailable()
                        yield part
                    if received != size:
                        raise DocumentUnavailable()

                yield DocumentContent(size, chunks())
            except httpx.HTTPError:
                raise DocumentUnavailable() from None
            finally:
                if response is not None:
                    await response.aclose()
