"""FastAPI application factory and production wiring."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version as distribution_version

import anyio
import httpx
from fastapi import FastAPI
from starlette.types import ASGIApp

from assistant_rh_api.bootstrap import create_chat_service
from assistant_rh_api.core.auth import AuthService
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.chat import ChatService
from assistant_rh_api.core.documents import DocumentService
from assistant_rh_api.core.feedback import FeedbackService
from assistant_rh_api.core.health import HealthProbe
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.auth_stores import DelegationReplayStore, GroupStore, SessionStore
from assistant_rh_api.db.documents import DocumentStore
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.health import PostgresHealthProbe
from assistant_rh_api.db.login_limits import LoginLimits, PostgresLoginLimiter
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.db.session_retention import maintain_sessions
from assistant_rh_api.db.settings_stores import ConfigStore
from assistant_rh_api.gateways.auth import LegacyPasswords, SessionTokens, SystemClock
from assistant_rh_api.gateways.delegation import SignedDelegations
from assistant_rh_api.gateways.documents import DocumentStorageSettings, S3Documents
from assistant_rh_api.handlers.auth import create_auth_router
from assistant_rh_api.handlers.auth_body import AuthBodyLimit
from assistant_rh_api.handlers.chat import NonStreamRequests, create_chat_router
from assistant_rh_api.handlers.chat_stream import StreamSettings, StreamWorkers
from assistant_rh_api.handlers.correlation import RequestCorrelation
from assistant_rh_api.handlers.documents import create_documents_router
from assistant_rh_api.handlers.errors import register_error_handlers
from assistant_rh_api.handlers.feedback import create_feedback_router
from assistant_rh_api.handlers.health import create_health_router
from assistant_rh_api.handlers.models import create_models_router


class CorrelatedFastAPI(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        # Include ServerErrorMiddleware so unexpected 500 responses are correlated too.
        return RequestCorrelation(super().build_middleware_stack())


def create_app(
    *,
    health_probe: HealthProbe | None = None,
    database: Database | None = None,
    environ: Mapping[str, str] | None = None,
    auth_service: AuthService | None = None,
    model_service: ModelService | None = None,
    rag_configuration_service: RAGConfigurationService | None = None,
    chat_service: ChatService | None = None,
    feedback_service: FeedbackService | None = None,
    document_service: DocumentService | None = None,
    stream_settings: StreamSettings | None = None,
) -> FastAPI:
    """Create the HTTP application without opening connections or loading RAG."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_database = database
        environment = os.environ if environ is None else environ
        if runtime_database is None and health_probe is None:
            if environment.get("SCW_POSTGRES_DSN", "").strip():
                runtime_database = Database(DatabaseSettings.from_environment(environment))
        application.state.database = runtime_database
        if runtime_database is None:
            if rag_configuration_service is not None:
                await rag_configuration_service.load()
            yield
            return
        try:
            await runtime_database.open()
            if rag_configuration_service is None:
                application.state.rag_configuration_service = RAGConfigurationService(ConfigStore(runtime_database))
            # Validate a first snapshot without pinning dynamic admin settings.
            # Future requests must load their own snapshot from the same service.
            await application.state.rag_configuration_service.load()
            sessions = SessionStore(runtime_database)
            if auth_service is None:
                application.state.auth_service = AuthService(
                    GroupStore(runtime_database),
                    sessions,
                    LegacyPasswords(),
                    SessionTokens(),
                    PostgresLoginLimiter(runtime_database, LoginLimits.from_environment(environment)),
                    SystemClock(),
                    delegations=SignedDelegations.from_environment(environment),
                    replays=DelegationReplayStore(runtime_database),
                )
            if feedback_service is None:
                application.state.feedback_service = FeedbackService(FeedbackStore(runtime_database), SystemClock())
            if health_probe is None:
                application.state.health_probe = PostgresHealthProbe(runtime_database)
            async with AsyncExitStack() as resources, anyio.create_task_group() as tasks:
                if document_service is None:
                    document_database = runtime_database.separate_pool(max_size=2)
                    resources.push_async_callback(document_database.close)
                    await document_database.open()
                    document_transport = await resources.enter_async_context(
                        httpx.AsyncHTTPTransport(
                            trust_env=False,
                            retries=0,
                            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                        )
                    )
                    application.state.document_service = DocumentService(
                        ChatRunStore(runtime_database),
                        DocumentStore(document_database),
                        S3Documents(document_transport, DocumentStorageSettings.from_environment(environment)),
                    )
                if chat_service is None:
                    client = await resources.enter_async_context(httpx.AsyncClient(trust_env=False))
                    application.state.chat_service = create_chat_service(
                        runtime_database,
                        application.state.rag_configuration_service,
                        client,
                        environment,
                        models=application.state.model_service,
                    )
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
            application.state.chat_service = chat_service
            application.state.feedback_service = feedback_service
            application.state.document_service = document_service

    @asynccontextmanager
    async def managed_lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with lifespan(application):
            try:
                yield
            finally:
                await asyncio.gather(application.state.stream_workers.aclose(), application.state.non_stream_requests.aclose())

    application = CorrelatedFastAPI(title="Assistant RH API", version=distribution_version("assistant-rh-api"), lifespan=managed_lifespan)
    application.state.health_probe = health_probe or PostgresHealthProbe()
    application.state.auth_service = auth_service
    application.state.model_service = model_service or ModelService()
    application.state.rag_configuration_service = rag_configuration_service
    application.state.chat_service = chat_service
    application.state.feedback_service = feedback_service
    application.state.document_service = document_service
    application.state.document_limiter = anyio.CapacityLimiter(8)
    application.state.non_stream_requests = NonStreamRequests()
    application.state.stream_workers = StreamWorkers(stream_settings or StreamSettings.from_environment(os.environ if environ is None else environ))
    application.add_middleware(AuthBodyLimit)
    register_error_handlers(application)
    application.include_router(create_health_router())
    application.include_router(create_auth_router())
    application.include_router(create_models_router())
    application.include_router(create_chat_router())
    application.include_router(create_feedback_router())
    application.include_router(create_documents_router())
    return application


app = create_app()
