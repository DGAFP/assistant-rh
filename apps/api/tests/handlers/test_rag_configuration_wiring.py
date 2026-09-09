from unittest.mock import AsyncMock

import pytest
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.handlers.app import create_app


@pytest.mark.anyio
async def test_injected_loader_is_retained_without_startup_config_read():
    store = AsyncMock()
    service = RAGConfigurationService(store)
    app = create_app(environ={}, rag_configuration_service=service)
    async with app.router.lifespan_context(app):
        assert app.state.rag_configuration_service is service
        store.load.assert_not_awaited()
    assert app.state.rag_configuration_service is service
