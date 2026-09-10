"""Server-owned correlation for HTTP and lifespan; no client content in logs."""

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

            async def correlated_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"x-request-id"]
                    message = {**message, "headers": [*headers, (b"x-request-id", correlation_id.encode("ascii"))]}
                await send(message)

            try:
                await self.app(scope, receive, correlated_send)
            except Exception as exc:
                report_database_error(exc)
                raise
