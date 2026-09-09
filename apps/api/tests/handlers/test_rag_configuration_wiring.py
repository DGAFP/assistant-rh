from unittest.mock import AsyncMock

import pytest
from assistant_rh_api.core.errors import RAGConfigurationError
from assistant_rh_api.core.models.configuration import Snapshot
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
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
