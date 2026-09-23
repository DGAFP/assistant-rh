"""OpenAI-compatible Chat Completions backed by ChatService."""

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.chat import ChatService
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.models.chat import ChatInput, PipelineResult
from assistant_rh_api.core.models.conversations import ChatRun
from assistant_rh_api.handlers.auth import Authenticated
from assistant_rh_api.handlers.chat_body import ChatRequestError, read_chat_body, validate_chat, validate_stream
from assistant_rh_api.handlers.chat_stream import extension
from assistant_rh_api.handlers.errors import ChatUnavailable, error_response


class NonStreamRequests:
    """Join non-stream executions before their provider and DB resources close."""

    def __init__(self) -> None:
        self.active: set[asyncio.Task[tuple[ChatRun, PipelineResult]]] = set()
        self.closed = False

    async def complete(self, service: ChatService, request: ChatInput, auth: AuthContext) -> tuple[ChatRun, PipelineResult]:
        if self.closed:
            raise ChatUnavailable()
        working = asyncio.create_task(service.complete(request, auth))
        self.active.add(working)
        try:
            # Caller cancellation reaches the core, which owns finalization.
            return await working
        finally:
            self.active.discard(working)

    async def aclose(self) -> None:
        self.closed = True
        pending = tuple(self.active)
        for working in pending:
            if not working.cancelling():
                working.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def create_chat_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def complete(request: Request, auth: Authenticated) -> Response:
        try:
            payload = await read_chat_body(request)
            body = validate_chat(payload)
            transport = validate_stream(payload)
        except ChatRequestError as exc:
            return error_response(exc.status, exc.code, "Request body is too large" if exc.status == 413 else "Invalid request")
        # Authorization precedes service availability and all configuration/corpus I/O.
        model = request.app.state.model_service.resolve(body.model, auth.group)
        service = request.app.state.chat_service
        if service is None:
            raise DatabaseUnavailable()
        if transport.stream:
            return request.app.state.stream_workers.response(service, body, auth, model, include_usage=transport.include_usage)
        run, result = await request.app.state.non_stream_requests.complete(service, body, auth)
        assert run.timestamp is not None
        return JSONResponse(
            {
                "id": "chatcmpl-" + run.turn_id,
                "object": "chat.completion",
                "created": int(run.timestamp.timestamp()),
                "model": run.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": run.answer}, "finish_reason": "stop"}],
                "usage": asdict(result.usage),
                "x_assistant_rh": extension(run),
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
