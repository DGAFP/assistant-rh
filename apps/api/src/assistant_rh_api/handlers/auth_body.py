"""Bound login bodies before JSON parsing, including requests without a length."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from assistant_rh_api.handlers.errors import error_response


class AuthBodyLimit:
    def __init__(self, app: ASGIApp, maximum: int = 16 * 1024) -> None:
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != "/v1/auth/session":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await error_response(400, "invalid_request", "Invalid request")(scope, receive, send)
            return
        body = bytearray()
        if length > self.maximum:
            await error_response(413, "request_too_large", "Request too large")(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.maximum:
                await error_response(413, "request_too_large", "Request too large")(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
