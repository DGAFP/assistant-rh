"""Server-owned correlation for HTTP and lifespan; no client content in logs."""

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from assistant_rh_api.core.db_diagnostics import diagnostic_context, report_database_error


class DiagnosticContext:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "lifespan"}:
            await self.app(scope, receive, send)
            return
        with diagnostic_context() as correlation_id:
            response_started = False

            async def correlated_send(message: Message) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"x-request-id"]
                    message = {**message, "headers": [*headers, (b"x-request-id", correlation_id.encode("ascii"))]}
                await send(message)

            try:
                await self.app(scope, receive, correlated_send)
            except Exception as exc:
                report_database_error(exc)
                # Starlette's outer ServerErrorMiddleware answers unhandled errors
                # after this context ends, so its 500 would lack the correlation.
                # Send the same generic body here; it then only re-raises the
                # exception for the server to log, as it already does.
                if scope["type"] == "http" and not response_started and not getattr(scope.get("app"), "debug", False):
                    await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, correlated_send)
                raise
