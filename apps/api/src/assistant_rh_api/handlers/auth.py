"""Public login transport and one reusable bearer dependency for protected routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from assistant_rh_api.core.auth import AuthContext, AuthService
from assistant_rh_api.core.errors import DatabaseUnavailable, InvalidCredentials


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def valid_password_text(cls, value: SecretStr) -> SecretStr:
        try:
            value.get_secret_value().encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid password text") from None
        return value


def get_auth_service(request: Request) -> AuthService:
    service = request.app.state.auth_service
    if service is None:
        raise DatabaseUnavailable()
    return service


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


async def resolve_bearer(request: Request, service: AuthServiceDependency) -> AuthContext:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise InvalidCredentials()
    scheme, separator, token = values[0].partition(" ")
    if not separator or scheme.lower() != "bearer":
        raise InvalidCredentials()
    return await service.resolve(token)


Authenticated = Annotated[AuthContext, Depends(resolve_bearer)]


def group_policy(context: AuthContext) -> dict:
    group = context.group
    return {
        "slug": group.slug,
        "allowed_ministries": list(group.allowed_ministries),
        "default_ministry": group.default_ministry,
        "credential_revision": group.credential_revision,
    }


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/v1/auth")

    @router.get("/groups")
    async def groups(service: AuthServiceDependency, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return {"data": [{"slug": g.slug, "label": g.label, "icon": g.icon, "color": g.color} for g in await service.list_groups()]}

    @router.post("/session")
    async def login(body: LoginRequest, request: Request, response: Response, service: AuthServiceDependency) -> dict:
        # The canonical Uvicorn entrypoint disables proxy-header rewriting.
        # Never use arbitrary Forwarded/X-Forwarded-For values as quota identities.
        source = request.client.host if request.client else "unknown"
        issued = await service.login(body.slug, body.password.get_secret_value(), source)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {
            "access_token": issued.access_token,
            "token_type": "bearer",
            "expires_in": service.remaining(issued.context.session.expires_at),
            "expires_at": issued.context.session.expires_at,
            "group": group_policy(issued.context),
        }

    @router.get("/me")
    async def me(context: Authenticated, service: AuthServiceDependency, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return {
            "group": group_policy(context),
            "expires_at": context.session.expires_at,
            "expires_in": service.remaining(context.session.expires_at),
        }

    @router.delete("/session", status_code=204)
    async def logout(context: Authenticated, service: AuthServiceDependency) -> Response:
        await service.logout(context)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return router
