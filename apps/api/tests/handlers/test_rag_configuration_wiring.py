from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from assistant_rh_api.core.errors import RAGConfigurationError
from assistant_rh_api.core.models.configuration import Snapshot
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.settings_stores import ConfigStore
from assistant_rh_api.handlers.app import create_app


@pytest.mark.anyio
async def test_injected_loader_is_validated_at_startup_and_retained():
    store = AsyncMock()
    store.load.return_value = None
    service = RAGConfigurationService(store)
    app = create_app(environ={}, rag_configuration_service=service)
    async with app.router.lifespan_context(app):
        assert app.state.rag_configuration_service is service
        store.load.assert_awaited_once()
    assert app.state.rag_configuration_service is service


@pytest.mark.anyio
async def test_invalid_configuration_prevents_startup():
    store = AsyncMock()
    store.load.return_value = Snapshot({"v3_temperature": "invalid"}, "r1", "database")
    app = create_app(environ={}, rag_configuration_service=RAGConfigurationService(store))
    with pytest.raises(RAGConfigurationError):
        async with app.router.lifespan_context(app):
            pytest.fail("Invalid configuration must prevent startup")


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_value", [[], None, "secret", 42, False])
async def test_invalid_stored_structure_prevents_startup(invalid_value):
    class DatabaseStub:
        @asynccontextmanager
        async def transaction(self, *, read_only):
            cursor = AsyncMock()
            cursor.fetchone.return_value = (invalid_value,)
            connection = AsyncMock()
            connection.execute.return_value = cursor
            yield connection

    service = RAGConfigurationService(ConfigStore(DatabaseStub()))
    app = create_app(environ={}, rag_configuration_service=service)
    with pytest.raises(RAGConfigurationError, match="^rag_configuration_error$"):
        async with app.router.lifespan_context(app):
            pytest.fail("Invalid stored structure must prevent startup")
