"""FastAPI application factory and production wiring."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import version as distribution_version

import anyio
from fastapi import FastAPI

from assistant_rh_api.core.auth import AuthService
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.health import HealthProbe
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.auth_stores import GroupStore, SessionStore
from assistant_rh_api.db.dsn import DatabaseSettings, resolve_dsn
from assistant_rh_api.db.health import PostgresHealthProbe
from assistant_rh_api.db.login_limits import LoginLimits, PostgresLoginLimiter
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.session_retention import maintain_sessions
from assistant_rh_api.db.settings_stores import ConfigStore
from assistant_rh_api.gateways.auth import LegacyPasswords, SessionTokens, SystemClock
from assistant_rh_api.handlers.auth import create_auth_router
from assistant_rh_api.handlers.auth_body import AuthBodyLimit
from assistant_rh_api.handlers.errors import register_error_handlers
from assistant_rh_api.handlers.health import create_health_router
from assistant_rh_api.handlers.models import create_models_router


def create_app(
    *,
    health_probe: HealthProbe | None = None,
    database: Database | None = None,
    environ: Mapping[str, str] | None = None,
    auth_service: AuthService | None = None,
    model_service: ModelService | None = None,
    rag_configuration_service: RAGConfigurationService | None = None,
) -> FastAPI:
    """Create the HTTP application without opening connections or loading RAG."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_database = database
        environment = os.environ if environ is None else environ
        if runtime_database is None and health_probe is None:
            if environment.get("SCW_POSTGRES_DSN", "").strip():
                runtime_database = Database(DatabaseSettings(dsn=resolve_dsn(environ=environment)))
        application.state.database = runtime_database
        if runtime_database is None:
            yield
            return
        try:
            await runtime_database.open()
            if rag_configuration_service is None:
                application.state.rag_configuration_service = RAGConfigurationService(ConfigStore(runtime_database))
            sessions = SessionStore(runtime_database)
            if auth_service is None:
                application.state.auth_service = AuthService(
                    GroupStore(runtime_database),
                    sessions,
                    LegacyPasswords(),
                    SessionTokens(),
                    PostgresLoginLimiter(runtime_database, LoginLimits.from_environment(environment)),
                    SystemClock(),
                )
            if health_probe is None:
                application.state.health_probe = PostgresHealthProbe(runtime_database)
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(maintain_sessions, sessions)
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            await runtime_database.close()
            application.state.auth_service = auth_service
            application.state.health_probe = health_probe or PostgresHealthProbe()
            application.state.rag_configuration_service = rag_configuration_service

    application = FastAPI(title="Assistant RH API", version=distribution_version("assistant-rh-api"), lifespan=lifespan)
    application.state.health_probe = health_probe or PostgresHealthProbe()
    application.state.auth_service = auth_service
    application.state.model_service = model_service or ModelService()
    application.state.rag_configuration_service = rag_configuration_service
    application.add_middleware(AuthBodyLimit)
    register_error_handlers(application)
    application.include_router(create_health_router())
    application.include_router(create_auth_router())
    application.include_router(create_models_router())
    return application


app = create_app()
