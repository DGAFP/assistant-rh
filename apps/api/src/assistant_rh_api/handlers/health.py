"""Unauthenticated liveness/readiness endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from assistant_rh_api.core.health import HealthProbe


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    config_loaded: bool


def create_health_router() -> APIRouter:
    """Create a health router bound to an injected runtime probe."""
    router = APIRouter()

    @router.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request, response: Response) -> HealthResponse:
        probe: HealthProbe = request.app.state.health_probe
        report = await probe.check()
        if not report.is_healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="ok" if report.is_healthy else "error",
            db=report.db,
            config_loaded=report.config_loaded,
        )

    return router
