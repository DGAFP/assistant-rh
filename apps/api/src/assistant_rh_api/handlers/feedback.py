"""Feedback transport; identities and ownership never come from request fields."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.feedback import FeedbackService
from assistant_rh_api.core.models.conversations import FeedbackInput
from assistant_rh_api.handlers.auth import Authenticated


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    completion_id: str = Field(min_length=1, max_length=137)
    stars: int = Field(ge=1, le=5)
    reasons_positive: list[str] = Field(default_factory=list, max_length=32)
    reasons_negative: list[str] = Field(default_factory=list, max_length=32)
    comment: str = Field(default="", max_length=4000)


def get_feedback_service(request: Request) -> FeedbackService:
    service = request.app.state.feedback_service
    if service is None:
        raise DatabaseUnavailable()
    return service


FeedbackServiceDependency = Annotated[FeedbackService, Depends(get_feedback_service)]


def create_feedback_router() -> APIRouter:
    router = APIRouter(prefix="/v1/feedback")

    @router.post("", status_code=204)
    async def save(body: FeedbackRequest, context: Authenticated, service: FeedbackServiceDependency) -> Response:
        await service.save(
            FeedbackInput(body.completion_id, body.stars, body.comment, tuple(body.reasons_positive), tuple(body.reasons_negative)),
            context,
        )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.get("/{completion_id}")
    async def get(completion_id: str, context: Authenticated, service: FeedbackServiceDependency, response: Response) -> dict:
        value = await service.get(completion_id, context)
        response.headers["Cache-Control"] = "no-store"
        return {
            "completion_id": value.turn_id,
            "stars": value.stars,
            "comment": value.comment,
            "reasons_positive": list(value.reasons_positive),
            "reasons_negative": list(value.reasons_negative),
            "helpful": value.helpful,
        }

    return router
