"""OpenAI-compatible Chat Completions backed by ChatService."""

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.handlers.auth import Authenticated
from assistant_rh_api.handlers.chat_body import ChatRequestError, read_chat_body, validate_chat
from assistant_rh_api.handlers.chat_stream import extension
from assistant_rh_api.handlers.errors import error_response


def create_chat_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def complete(request: Request, auth: Authenticated) -> Response:
        try:
            payload = await read_chat_body(request)
            body = validate_chat(payload)
        except ChatRequestError as exc:
            return error_response(exc.status, exc.code, "Request body is too large" if exc.status == 413 else "Invalid request")
        # Authorization precedes service availability and all configuration/corpus I/O.
        model = request.app.state.model_service.resolve(body.model, auth.group)
        service = request.app.state.chat_service
        if service is None:
            raise DatabaseUnavailable()
        if payload.get("stream", False):
            return request.app.state.stream_workers.response(
                service, body, auth, model, include_usage=(payload.get("stream_options") or {}).get("include_usage", False),
            )
        run, result = await service.complete(body, auth)
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
