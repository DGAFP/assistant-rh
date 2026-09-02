"""FastAPI application factory and production wiring."""

from __future__ import annotations

from importlib.metadata import version as distribution_version

from fastapi import FastAPI

from assistant_rh_api.core.health import HealthProbe
from assistant_rh_api.db.health import PostgresHealthProbe
from assistant_rh_api.handlers.health import create_health_router


def create_app(*, health_probe: HealthProbe | None = None) -> FastAPI:
    """Create the HTTP application without opening connections or loading RAG."""
    application = FastAPI(title="Assistant RH API", version=distribution_version("assistant-rh-api"))
    application.include_router(create_health_router(health_probe or PostgresHealthProbe()))
    return application


app = create_app()
