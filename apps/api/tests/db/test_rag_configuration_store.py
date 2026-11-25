from pathlib import Path

import pytest
from assistant_rh_api.core.errors import RAGConfigurationError
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.settings_stores import ConfigStore
from assistant_rh_api.handlers.app import create_app

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(("previous", "expected"), [("{}", 40), ('{"v3_rerank_input_k":20}', 40), ('{"v3_rerank_input_k":30}', 30)])
async def test_rerank_default_migration_preserves_explicit_nondefault_values(repository_db, previous, expected):
    path = Path(__file__).resolve().parents[4] / "supabase/migrations/20261124173000_raise_rag_rerank_input_default.sql"
    migration = path.read_text()
    async with repository_db.transaction() as connection:
        async with connection.transaction(force_rollback=True):
            await connection.execute(
                """UPDATE public.rag_config SET config = %s::jsonb || '{"v3_enable_selector":false}'::jsonb WHERE id = 1""",
                (previous,),
            )
            await connection.execute(migration)
            await connection.execute(migration)
            row = await (await connection.execute("SELECT config FROM public.rag_config WHERE id = 1")).fetchone()
            assert row[0] == {"v3_rerank_input_k": expected, "v3_enable_selector": False}


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


@pytest.mark.parametrize(
    "invalid_json",
    [
        '{"v3_token_budget":"invalid"}',
        "[]",
        "null",
        '"secret"',
        "42",
        "false",
        '{"v3_tables":false}',
        '{"v3_tables":0}',
        '{"v3_tables":""}',
        '{"v3_tables":{}}',
    ],
)
async def test_invalid_initial_db_config_closes_pool_and_allows_restart(repository_db, repository_dsn, invalid_json):
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.rag_config SET config = %s::jsonb WHERE id = 1", (invalid_json,))
    database = Database(DatabaseSettings(dsn=repository_dsn))
    app = create_app(database=database)
    try:
        with pytest.raises(RAGConfigurationError):
            async with app.router.lifespan_context(app):
                pytest.fail("Startup must validate the database configuration")
        assert app.state.rag_configuration_service is None
        assert database.closed
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute("UPDATE public.rag_config SET config = '{}' WHERE id = 1")
    app = create_app(database=Database(DatabaseSettings(dsn=repository_dsn)))
    async with app.router.lifespan_context(app):
        assert app.state.rag_configuration_service is not None
