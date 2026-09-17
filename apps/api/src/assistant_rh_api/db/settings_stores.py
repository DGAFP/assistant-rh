"""Fresh, immutable DB snapshots. No implicit cache or packaged fallback."""

from collections.abc import Mapping

from assistant_rh_api.core.errors import RAGConfigurationError
from assistant_rh_api.core.models.configuration import Acronym, ConfigValues, Prompt, Snapshot
from assistant_rh_api.core.ports.configuration import AcronymStorePort, ConfigStorePort, PromptStorePort
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.revisions import content_revision, freeze_json


class ConfigStore(ConfigStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self) -> Snapshot[ConfigValues] | None:
        async with self._database.transaction(read_only=True) as connection:
            row = await (await connection.execute("SELECT config FROM public.rag_config WHERE id = 1")).fetchone()
        if row is None:
            return None
        value = freeze_json(row[0])
        if not isinstance(value, Mapping):
            raise RAGConfigurationError()
        return Snapshot(value, content_revision(value), "database")


class PromptStore(PromptStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, name: str) -> Snapshot[Prompt] | None:
        async with self._database.transaction(read_only=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT content FROM public.system_prompts WHERE name = %s AND is_active = TRUE",
                    (name,),
                )
            ).fetchone()
        return None if row is None else Snapshot(Prompt(name, row[0]), content_revision((name, row[0])), "database")


class AcronymStore(AcronymStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self) -> Snapshot[tuple[Acronym, ...]]:
        async with self._database.transaction(read_only=True) as connection:
            # Historical admin schema has no priority; to_jsonb also reads older
            # installations that do have it without issuing speculative DDL.
            rows = await (
                await connection.execute("""
                SELECT acronym, expansion FROM public.acronyms a
                ORDER BY COALESCE((to_jsonb(a)->>'priority')::integer, 0) DESC, acronym COLLATE "C", id
            """)
            ).fetchall()
        values = tuple(Acronym(*row) for row in rows)
        return Snapshot(values, content_revision(tuple((a.short, a.expansion) for a in values)), "database")
