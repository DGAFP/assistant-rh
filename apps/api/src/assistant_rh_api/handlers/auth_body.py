"""Bound login and feedback bodies before JSON parsing, including chunked requests."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from assistant_rh_api.handlers.errors import error_response


class AuthBodyLimit:
    def __init__(self, app: ASGIApp, maximum: int | None = None) -> None:
        self.app = app
        self.maximum = 16 * 1024 if maximum is None else maximum
        self.feedback_maximum = 64 * 1024 if maximum is None else maximum

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") not in ("/v1/auth/session", "/v1/feedback"):
            await self.app(scope, receive, send)
            return
        # 4,000 escaped supplementary Unicode characters occupy 48 KB alone.
        maximum = self.feedback_maximum if scope["path"].rstrip("/") == "/v1/feedback" else self.maximum
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await error_response(400, "invalid_request", "Invalid request")(scope, receive, send)
            return
        body = bytearray()
        if length > maximum:
            await error_response(413, "request_too_large", "Request too large")(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > maximum:
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
