import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest
from assistant_rh_api.core.documents import DocumentService
from assistant_rh_api.core.errors.documents import DocumentNotFound, DocumentUnavailable
from assistant_rh_api.core.models.conversations import RunSource
from assistant_rh_api.core.ports.documents import DocumentContent, StoredDocument
from assistant_rh_api.gateways.documents import DocumentStorageSettings, S3Documents
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.auth import resolve_bearer
from assistant_rh_api.handlers.documents import DocumentResponse

from apps.api.tests.auth_fakes import Signer, delegated_service, individual_context

pytestmark = pytest.mark.anyio
DOC_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
OWNER = individual_context().user_id
PDF = b"%PDF-1.7\n" + b"synthetic" * 16384
PATH = "/v1/documents/final/content?completion_id=chatcmpl-run"


class S3Stream(httpx.AsyncByteStream):
    def __init__(self, data=PDF):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.data), 8192):
            yield self.data[offset : offset + 8192]

    async def aclose(self):
        self.closed = True


class Runs:
    async def require_individual_schema(self):
        pass

    async def sources(self, turn_id, group_slug, *, ministries, user_id):
        if turn_id == "run" and user_id == OWNER and "matte" in ministries:
            return (RunSource("final", "Title", "", str(DOC_ID)),)
        return ()


class Documents:
    def __init__(self, *, s3=False):
        self.opened = self.closed = self.read = 0
        self.s3 = s3

    @asynccontextmanager
    async def open(self, document_id):
        assert document_id == DOC_ID
        self.opened += 1

        async def chunks():
            for offset in range(0, len(PDF), 65536):
                self.read += 1
                yield PDF[offset : offset + 65536]

        try:
            yield StoredDocument("../../secret\r\nTitle", "matte/private-key.pdf", None if self.s3 else DocumentContent(len(PDF), chunks()))
        finally:
            self.closed += 1


def application(*, signer=None, documents=None, objects=None):
    documents = documents or Documents()
    app = create_app(
        document_service=DocumentService(Runs(), documents, objects),
        auth_service=delegated_service(signer) if signer else None,
    )
    if signer is None:
        app.dependency_overrides[resolve_bearer] = individual_context
    return app, documents


@pytest.mark.parametrize("range_header", [None, "bytes=0-10", "bytes=999999999999-", "bytes=0-1,4-5", "malformed"])
async def test_pdf_headers_full_content_and_ignored_ranges(range_header):
    app, documents = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, headers={"Range": range_header} if range_header else {})
    assert response.status_code == 200 and response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(PDF))
    assert response.headers["content-disposition"] == 'inline; filename="document.pdf"'
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["accept-ranges"] == "none"
    assert "location" not in response.headers
    assert "private-key" not in str(response.headers) and "secret" not in str(response.headers)
    assert documents.opened == documents.closed == 1 and documents.read == 3


async def test_fresh_delegation_each_read_and_withdrawal_same_known_link():
    signer = Signer()
    app, documents = application(signer=signer)
    token = signer.sign()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(PATH, headers={"Authorization": "Bearer " + token})).status_code == 200
        assert (await client.get(PATH, headers={"Authorization": "Bearer " + token})).status_code == 401
        for claims in (
            signer.claims(models=[]),
            signer.claims(user_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            signer.claims(ministry="mi"),
            signer.claims(models=["assistant-rh-mi"]),
        ):
            denied = await client.get(PATH, headers={"Authorization": "Bearer " + signer.sign(claims), "Range": "bytes=0-10"})
            assert denied.status_code == 404 and denied.json()["error"]["code"] == "document_not_found"
        # Expired delegation and no session/assertion cannot reopen a known link.
        expired = signer.claims(iat=int(signer.claims()["iat"]) - 120, exp=int(signer.claims()["iat"]) - 1)
        assert (await client.get(PATH, headers={"Authorization": "Bearer " + signer.sign(expired)})).status_code == 401
        assert (await client.get(PATH)).status_code == 401
    assert documents.opened == 1


@pytest.mark.parametrize(
    "path",
    [
        PATH.replace("/final/", "/candidate/"),
        PATH.replace("chatcmpl-run", "chatcmpl-other"),
        PATH.replace("chatcmpl-run", "bad/id"),
        PATH.replace("/final/", "/s3-key.pdf/"),
    ],
)
async def test_absent_forbidden_and_substituted_references_are_uniform(path):
    app, documents = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404 and response.json()["error"]["code"] == "document_not_found"
    assert documents.opened == 0


async def test_b4_cannot_open_individual_source_or_old_capability_routes():
    from apps.api.tests.auth_fakes import service

    auth = service()
    issued = await auth.login("beta", "password", "local")
    app, documents = application()
    app.dependency_overrides.clear()
    app.state.auth_service = auth
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, headers={"Authorization": "Bearer " + issued.access_token})
        assert response.status_code == 403
        assert (await client.get("/v1/documents/access/old-capability")).status_code == 404
        assert (await client.post("/v1/documents/final/access-url")).status_code == 404
    assert documents.opened == 0


@pytest.mark.parametrize("error,status", [(DocumentNotFound, 404), (DocumentUnavailable, 503), (TimeoutError, 503)])
async def test_storage_errors_are_safe_before_headers(error, status):
    class BrokenDocuments:
        @asynccontextmanager
        async def open(self, _):
            raise error()
            yield

    app, _ = application(documents=BrokenDocuments())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH)
    assert response.status_code == status and "storage" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert app.state.document_limiter.borrowed_tokens == 0


async def test_s3_content_transfers_through_backend_without_redirect():
    requests = []

    async def s3(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Length": str(len(PDF))}, stream=S3Stream())

    objects = S3Documents(httpx.MockTransport(s3), DocumentStorageSettings(access_key="synthetic", secret_key="synthetic"))
    app, _ = application(documents=Documents(s3=True), objects=objects)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH)
    assert response.status_code == 200 and response.content == PDF and "location" not in response.headers
    assert len(requests) == 1 and requests[0].headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert str(requests[0].url).endswith("/matte/private-key.pdf") and "?" not in str(requests[0].url)


@pytest.mark.parametrize("event", ["disconnect", "send_error", "cancel"])
async def test_reader_and_admission_released_on_interrupted_transfer(event):
    app, documents = application()
    response = DocumentResponse(app.state.document_service, DOC_ID, app.state.document_limiter)
    begun = asyncio.Event()
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            begun.set()
            if event == "send_error":
                raise OSError("client closed")
            await asyncio.Event().wait()

    async def receive():
        await begun.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
    task = asyncio.create_task(response(scope, receive, send))
    await asyncio.wait_for(begun.wait(), 2)
    if event == "cancel":
        task.cancel()
    try:
        await asyncio.wait_for(task, 2)
    except (asyncio.CancelledError, OSError, ExceptionGroup):
        pass
    assert documents.opened == documents.closed == 1
    assert documents.read < 3 and app.state.document_limiter.borrowed_tokens == 0


async def test_admission_saturation_rejects_without_opening_storage():
    app, documents = application()
    limiter = app.state.document_limiter
    borrowers = [object() for _ in range(8)]
    for borrower in borrowers:
        limiter.acquire_on_behalf_of_nowait(borrower)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(PATH)
            assert response.status_code == 503
        assert documents.opened == 0
    finally:
        for borrower in borrowers:
            limiter.release_on_behalf_of(borrower)
