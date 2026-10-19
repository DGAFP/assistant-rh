"""Correlate backend calls without logging identity, bearer or delegation claims."""

from __future__ import annotations

import logging
import re
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)
REQUEST_ID = re.compile(rb"[0-9a-f]{32}")


class RequestCorrelation:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        candidates = [value for name, value in scope.get("headers", ()) if name == b"x-client-request-id"]
        request_id = candidates[0].decode("ascii") if len(candidates) == 1 and REQUEST_ID.fullmatch(candidates[0]) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def correlated_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(name, value) for name, value in message.get("headers", ()) if name.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii"))]}
                logger.info("api_request request_id=%s status=%s", request_id, message["status"])
            await send(message)

        try:
            await self.app(scope, receive, correlated_send)
        except Exception:
            # Starlette re-raises after sending a safe 500. Keep Uvicorn's error
            # handling (including closing incomplete streams), without the original
            # exception message, traceback or chained exceptions in its logs.
            raise RuntimeError(f"API request failed (request_id={request_id})") from None
