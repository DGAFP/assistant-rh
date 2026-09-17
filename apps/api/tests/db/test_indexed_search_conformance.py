"""Vector indexes must not change the retained semantic candidate selection."""

import random
from contextlib import asynccontextmanager
from dataclasses import replace

import anyio
import psycopg
import pytest
from assistant_rh_api.core.models.retrieval import SearchRequest
from assistant_rh_api.db.search_catalog import search_catalog
from assistant_rh_api.db.search_store import SearchStore
from assistant_rh_rag_pipeline.config import CHUNK_TABLES, RetrievalConfig
from assistant_rh_rag_pipeline.retriever import Retriever
from psycopg.conninfo import make_conninfo

pytestmark = pytest.mark.anyio
TABLE = "api_semantic_ann_fixture"


async def test_semantic_candidates_match_legacy_with_an_approximate_index(repository_db, repository_dsn):
    rng = random.Random(560)
    vector = tuple(rng.uniform(-1, 1) for _ in range(1024))
    try:
        with psycopg.connect(repository_dsn) as connection:
            connection.execute("""
                CREATE TABLE api_semantic_ann_fixture (
                    hash_id TEXT PRIMARY KEY, chunk_text TEXT, section_id UUID, short_id TEXT,
                    section_path TEXT, source_name TEXT, embedding_m3 VECTOR(1024)
                )
            """)
            connection.execute("""
                INSERT INTO public.rag_documents(doc_id, short_id, title, source_url)
                VALUES ('00000000-0000-0000-0000-000000000560', 'ann-synthetic', 'Synthetic guide', 'https://example.invalid/test')
            """)
            with connection.cursor().copy(
                "COPY api_semantic_ann_fixture(hash_id, chunk_text, short_id, section_path, source_name, embedding_m3) FROM STDIN"
            ) as copy:
                for index in range(2500):
                    embedding = [rng.uniform(-1, 1) for _ in range(1024)]
                    copy.write_row((str(index), "Synthetic paragraph. " * 80, "ann-synthetic", "Guide", "Synthetic", str(embedding)))
            connection.execute("""
                CREATE INDEX api_semantic_ann_index ON api_semantic_ann_fixture
                USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 50)
            """)
            connection.execute("ANALYZE api_semantic_ann_fixture")

        class PlannerSettings:
            @asynccontextmanager
            async def transaction(self, **kwargs):
                async with repository_db.transaction(**kwargs) as connection:
                    # Reproduce the plan transition observed on the staging corpus.
                    await connection.execute("SELECT set_config('random_page_cost', '16', true)")
                    yield connection

        legacy = Retriever(
            RetrievalConfig(ivfflat_probes=1),
            dsn=make_conninfo(repository_dsn, options="-c random_page_cost=16"),
        )
        expected = await anyio.to_thread.run_sync(
            lambda: legacy._search_table_semantic(replace(CHUNK_TABLES["matte"], name=TABLE), list(vector), "albert", top_k=30, strict_errors=True)
        )
        table = next(table for table in search_catalog() if table.source.key == "matte")
        table = replace(table, source=replace(table.source, name=TABLE))
        actual = await SearchStore(PlannerSettings(), (table,)).search(SearchRequest("matte", "vector", embedding=vector, limit=30, probes=1))
        assert [(row.chunk_id, row.text, row.score, row.section_id) for row in actual] == [
            (row.chunk_id, row.text, row.score, str(row.section_id) if row.section_id else None) for row in expected
        ]
        assert [row.rank for row in actual] == list(range(1, 31))
    finally:
        with psycopg.connect(repository_dsn) as connection:
            connection.execute("DROP TABLE IF EXISTS api_semantic_ann_fixture")
