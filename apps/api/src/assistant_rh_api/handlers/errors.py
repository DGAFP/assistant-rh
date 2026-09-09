"""Safe OpenAI-style errors; no request bodies, passwords or database messages."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from assistant_rh_api.core.auth import InvalidCredentials, LoginRateLimited, MinistryForbidden
from assistant_rh_api.core.errors import ApplicationError, DatabaseUnavailable, MinistryConfigurationError, ModelNotFound


def error_response(status: int, code: str, message: str, *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "server_error" if status >= 500 else "invalid_request_error", "code": code}},
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def transport_error(request: Request, exc: HTTPException) -> JSONResponse:
        # Parsing errors can bypass RequestValidationError; never echo details.
        return error_response(exc.status_code, "invalid_request", "Invalid request")

    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        if isinstance(exc, InvalidCredentials):
            return error_response(401, exc.code, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
        if isinstance(exc, MinistryForbidden):
            return error_response(403, exc.code, "Ministry not permitted")
        if isinstance(exc, MinistryConfigurationError):
            return error_response(500, exc.code, "Invalid group ministry configuration")
        if isinstance(exc, ModelNotFound):
            return error_response(404, exc.code, "Model not found")
        if isinstance(exc, LoginRateLimited):
            return error_response(429, exc.code, "Too many authentication attempts", headers={"Retry-After": str(exc.retry_after)})
        if isinstance(exc, DatabaseUnavailable):
            return error_response(503, "service_unavailable", "Service unavailable")
        return error_response(500, "internal_error", "Internal server error")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default details include the submitted input, including secrets.
        return error_response(422, "invalid_request", "Invalid request")
