"""Content delivery to Conversations; each HTTP request reauthenticates first."""

import asyncio
from uuid import UUID

import anyio
from fastapi import APIRouter, Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from assistant_rh_api.core.documents import DocumentService
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.errors.documents import DocumentUnavailable
from assistant_rh_api.handlers.auth import Authenticated
from assistant_rh_api.handlers.errors import error_response


class DocumentResponse(Response):
    """Own the reader until ASGI finishes, including disconnects and send failures."""

    def __init__(self, service: DocumentService, document_id: UUID, limiter: anyio.CapacityLimiter) -> None:
        super().__init__()
        self.service, self.document_id, self.limiter = service, document_id, limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            self.limiter.acquire_nowait()
        except anyio.WouldBlock:
            await error_response(503, "service_unavailable", "Service unavailable")(scope, receive, send)
            return
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            # Total deadline includes opening, reading and downstream backpressure.
            async with asyncio.timeout(120):
                async with self.service.open(self.document_id) as content:
                    response = StreamingResponse(
                        content.chunks,
                        media_type="application/pdf",
                        headers={
                            "Content-Length": str(content.size),
                            # A constant safe name reveals neither title nor storage key.
                            "Content-Disposition": 'inline; filename="document.pdf"',
                            "Cache-Control": "private, no-store",
                            "X-Content-Type-Options": "nosniff",
                            "Accept-Ranges": "none",
                        },
                    )
                    await response(scope, receive, tracked_send)
        except (TimeoutError, DocumentUnavailable, DatabaseUnavailable):
            if started:
                # Headers are committed: abort the transfer, never append a JSON error
                # to a PDF or expose the upstream exception through server logs.
                raise DocumentUnavailable() from None
            await error_response(503, "service_unavailable", "Service unavailable")(scope, receive, send)
        finally:
            self.limiter.release()


def create_documents_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/documents/{doc_ref}/content")
    async def content(doc_ref: str, completion_id: str, request: Request, auth: Authenticated) -> Response:
        service = request.app.state.document_service
        if service is None:
            raise DatabaseUnavailable()
        document_id = await service.authorize(completion_id, doc_ref, auth)
        # Range/If-Range are deliberately ignored: every request gets the entire
        # PDF with 200, after fresh authorization (RFC 9110 allows this).
        return DocumentResponse(service, document_id, request.app.state.document_limiter)

    return router
