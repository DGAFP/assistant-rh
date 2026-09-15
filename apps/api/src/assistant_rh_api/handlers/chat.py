"""OpenAI-compatible non-stream Chat Completions backed by ChatService."""

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.handlers.auth import Authenticated
from assistant_rh_api.handlers.chat_body import ChatRequestError, read_chat_body, validate_chat
from assistant_rh_api.handlers.errors import error_response


def create_chat_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def complete(request: Request, auth: Authenticated) -> JSONResponse:
        try:
            body = validate_chat(await read_chat_body(request))
        except ChatRequestError as exc:
            return error_response(exc.status, exc.code, "Request body is too large" if exc.status == 413 else "Invalid request")
        # Authorization precedes service availability and all configuration/corpus I/O.
        request.app.state.model_service.resolve(body.model, auth.group)
        service = request.app.state.chat_service
        if service is None:
            raise DatabaseUnavailable()
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
                "x_assistant_rh": {
                    "turn_id": run.turn_id,
                    "ministry": run.selected_ministry,
                    "sources": [
                        {
                            "title": source.title,
                            "url": source.url or None,
                            "publisher": source.publisher,
                            "doc_ref": source.doc_ref,
                            "access": source.access,
                        }
                        for source in run.sources
                    ],
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
