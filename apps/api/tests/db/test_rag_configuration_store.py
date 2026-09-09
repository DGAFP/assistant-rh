import pytest
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.settings_stores import ConfigStore
from assistant_rh_api.handlers.app import create_app

pytestmark = pytest.mark.anyio


async def test_admin_json_update_affects_next_snapshot_only(repository_db):
    service = RAGConfigurationService(ConfigStore(repository_db))
    async with repository_db.transaction() as connection:
        await connection.execute('UPDATE public.rag_config SET config = \'{"v3_generator_model":"first"}\' WHERE id = 1')
    first = await service.load()
    async with repository_db.transaction() as connection:
        await connection.execute('UPDATE public.rag_config SET config = \'{"v3_generator_model":"second"}\' WHERE id = 1')
    second = await service.load()
    assert first.config.value.generation.model == "first"
    assert second.config.value.generation.model == "second"
    assert first.config.revision != second.config.revision


async def test_lifespan_assembles_loader_without_caching_config(repository_db):
    app = create_app(database=repository_db)
    assert app.state.rag_configuration_service is None
    async with app.router.lifespan_context(app):
        service = app.state.rag_configuration_service
        first = await service.load()
        async with repository_db.transaction() as connection:
            await connection.execute("UPDATE public.rag_config SET config = '{\"v3_token_budget\":12001}' WHERE id = 1")
        second = await service.load()
        assert second.config.value.context.token_budget == 12001
        assert first.config.value.context.token_budget != second.config.value.context.token_budget
    assert app.state.rag_configuration_service is None
