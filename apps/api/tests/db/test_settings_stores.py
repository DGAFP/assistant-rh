import pytest
from assistant_rh_api.db.settings_stores import AcronymStore, ConfigStore, PromptStore

pytestmark = pytest.mark.anyio


async def test_config_present_absent_revision_and_no_stale_cache(repository_db):
    store = ConfigStore(repository_db)
    first = await store.load()
    assert first.origin == "database"
    async with repository_db.transaction() as connection:
        await connection.execute('UPDATE public.rag_config SET config = \'{"nested":{"values":[1,2]}}\' WHERE id = 1')
    second = await store.load()
    assert second.revision != first.revision
    assert second.value["nested"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        second.value["nested"]["values"] = ()
    with pytest.raises(RuntimeError):
        async with repository_db.transaction() as connection:
            await connection.execute("DELETE FROM public.rag_config WHERE id = 1")
            raise RuntimeError()
    assert await store.load() == second
    async with repository_db.transaction() as connection:
        await connection.execute("DELETE FROM public.rag_config WHERE id = 1")
    assert await store.load() is None
    async with repository_db.transaction() as connection:
        await connection.execute("INSERT INTO public.rag_config(id, config) VALUES (1, '{}')")


async def test_prompts_inactive_absent_and_revision(repository_db):
    store = PromptStore(repository_db)
    assert await store.get("missing") is None
    async with repository_db.transaction() as connection:
        await connection.execute("INSERT INTO public.system_prompts(name, content) VALUES ('system', 'Hello {today}')")
    first = await store.get("system")
    assert first.value.content == "Hello {today}"  # No rendering in DB.
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.system_prompts SET content = 'Changed' WHERE name = 'system'")
    assert (await store.get("system")).revision != first.revision
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.system_prompts SET is_active = FALSE")
    assert await store.get("system") is None


async def test_acronyms_both_historical_schemas_and_ties(repository_db):
    store = AcronymStore(repository_db)
    assert (await store.load()).value == ()
    async with repository_db.transaction() as connection:
        await connection.execute("INSERT INTO public.acronyms(acronym, expansion) VALUES ('ZZ', 'Last'), ('AA', 'First')")
    first = await store.load()
    assert [a.short for a in first.value] == ["AA", "ZZ"]
    try:
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.acronyms ADD COLUMN priority INTEGER DEFAULT 0")
            await connection.execute("UPDATE public.acronyms SET priority = 5 WHERE acronym = 'ZZ'")
        second = await store.load()
        assert [a.short for a in second.value] == ["ZZ", "AA"]
        assert first.revision != second.revision
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.acronyms DROP COLUMN priority")
