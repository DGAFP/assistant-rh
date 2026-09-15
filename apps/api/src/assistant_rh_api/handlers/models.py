"""OpenAI-compatible model catalogue protected by the common bearer resolver."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.handlers.auth import Authenticated


class ModelResponse(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: Literal["assistant-rh"] = "assistant-rh"


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelResponse]


def get_model_service(request: Request) -> ModelService:
    return request.app.state.model_service


ModelServiceDependency = Annotated[ModelService, Depends(get_model_service)]


def create_models_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/models", response_model=ModelListResponse)
    async def models(context: Authenticated, service: ModelServiceDependency, response: Response) -> ModelListResponse:
        response.headers["Cache-Control"] = "no-store"
        return ModelListResponse(data=[ModelResponse(id=model.id, created=model.created) for model in service.list_models(context.group)])

    return router
