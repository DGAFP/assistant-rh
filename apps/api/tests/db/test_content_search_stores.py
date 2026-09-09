from dataclasses import replace

import pytest
from assistant_rh_api.core.models.retrieval import SearchRequest
from assistant_rh_api.db.content_store import TABLES, ContentStore
from assistant_rh_api.db.search_store import SearchStore
from psycopg import sql

pytestmark = pytest.mark.anyio
DOC = "00000000-0000-0000-0000-000000000001"
SEC1 = "00000000-0000-0000-0000-000000000011"
SEC2 = "00000000-0000-0000-0000-000000000012"
MISSING = "00000000-0000-0000-0000-000000000099"


@pytest.fixture
async def corpus(repository_db):
    async with repository_db.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO public.rag_documents VALUES (%s, 'F1', 'Congés', 'https://example.invalid/guide', 'Synthetic', 'Guide entier', 10)
        """,
            (DOC,),
        )
        # Reverse insertion order; section lookup must break ties by UUID.
        for sid in (SEC2, SEC1):
            await connection.execute(
                """
                INSERT INTO public.rag_sections VALUES (%s, %s, 'Congés', 'Guide > Congés', 'Section synthétique', '[{"number":"1"}]')
            """,
                (sid, DOC),
            )
        for source, (table, id_col, _) in TABLES.items():
            for cid in ("b", "a"):
                await connection.execute(
                    sql.SQL("""
                    INSERT INTO {} ({}, chunk_text, embedding_m3, embedding_bge_scw)
                    VALUES (%s, 'Congés annuels', '[1,0,0]', '[0,1,0]')
                """).format(sql.Identifier("public", table), sql.Identifier(id_col)),
                    (cid,),
                )
            if source == "service_public":
                await connection.execute("UPDATE public.rag_chunks_service_public SET short_id = 'F1', section_path = 'Guide > Congés'")
            elif source != "dgafp":
                await connection.execute(sql.SQL("UPDATE {} SET section_id = %s").format(sql.Identifier("public", table)), (SEC1,))
        await connection.execute(
            "UPDATE public.rag_chunks_dgafp SET number = '1', cid = 'CID', url = 'https://example.invalid/law', full_title = 'Loi'"
        )
    return repository_db


async def test_content_present_absent_dedup_order_and_legal_refs(corpus):
    store = ContentStore(corpus)
    docs = await store.documents((DOC, DOC, MISSING))
    assert len(docs) == 1 and docs[0].markdown == "Guide entier"
    sections = await store.sections((SEC2, SEC1, MISSING, SEC1))
    assert [s.section_id for s in sections] == [SEC1, SEC2]
    assert sections[0].document == docs[0]
    assert sections[0].legal_references[0]["number"] == "1"
    refs = await store.references(("1", "1", "absent"))
    assert len(refs) == 1 and refs[0].cid == "CID"
    chunks = await store.chunks("service_public", ("b", "a", "absent", "a"))
    assert [c.chunk_id for c in chunks] == ["a", "b"]
    assert all(c.section_id == SEC1 for c in chunks)
    assert await store.documents(()) == await store.sections(()) == await store.references(()) == ()
    assert await store.chunks("matte", ()) == ()
    assert await store.documents((MISSING,)) == await store.sections((MISSING,)) == ()


@pytest.mark.parametrize("source", list(TABLES))
@pytest.mark.parametrize("mode", ["vector", "lexical", "heading"])
async def test_search_raw_lanes_and_deterministic_ties(corpus, source, mode):
    request = SearchRequest(source, mode, query="congés", embedding=(1.0, 0.0, 0.0))
    results = await SearchStore(corpus).search(request)
    if source == "dgafp" and mode == "heading":
        assert results == ()
    else:
        assert [c.chunk_id for c in results] == ["a", "b"]
        assert [c.rank for c in results] == [1, 2]
        assert results[0].score == results[1].score
        assert [c.chunk_id for c in await SearchStore(corpus).search(replace(request, limit=1))] == ["a"]
    if mode != "vector":
        assert await SearchStore(corpus).search(replace(request, query="introuvable")) == ()


async def test_vector_provider_columns_and_transaction_local_probes(corpus):
    store = SearchStore(corpus)
    request = SearchRequest("matte", "vector", embedding=(0.0, 1.0, 0.0), probes=17)
    assert (await store.search(request))[0].score == 0.0
    assert (await store.search(replace(request, embedding_model="bge_scaleway")))[0].score == 1.0
    assert (await store.search(replace(request, probes=0)))[0].score == 0.0
    async with corpus.transaction() as connection:
        cursor = await connection.execute("SHOW ivfflat.probes")
        assert (await cursor.fetchone())[0] == "1"


async def test_legacy_and_partial_service_public_relations(corpus):
    store = SearchStore(corpus)
    request = SearchRequest("service_public", "heading", query="congés")
    assert all(c.section_id == SEC1 for c in await store.search(request))
    try:
        async with corpus.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_chunks_service_public ADD COLUMN section_id UUID")
            await connection.execute("UPDATE public.rag_chunks_service_public SET section_id = %s WHERE hash_id = 'b'", (SEC2,))
        assert [c.section_id for c in await store.search(request)] == [SEC1, SEC2]
    finally:
        async with corpus.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_chunks_service_public DROP COLUMN section_id")


async def test_legacy_section_markdown_column(corpus):
    try:
        async with corpus.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_sections RENAME COLUMN section_markdown TO markdown_content")
        assert (await ContentStore(corpus).sections((SEC1,)))[0].markdown == "Section synthétique"
    finally:
        async with corpus.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_sections RENAME COLUMN markdown_content TO section_markdown")


async def test_document_metadata_survives_an_unresolved_legacy_section(corpus):
    async with corpus.transaction() as connection:
        await connection.execute("UPDATE public.rag_chunks_service_public SET section_path = 'Missing section'")
    store = SearchStore(corpus)
    for mode in ("vector", "lexical"):
        chunks = await store.search(SearchRequest("service_public", mode, query="congés", embedding=(1.0, 0.0, 0.0)))
        assert chunks[0].section_id is None
        assert chunks[0].metadata["doc_title"] == "Congés"
        assert chunks[0].metadata["doc_url"] == "https://example.invalid/guide"
        assert chunks[0].metadata["doc_id"] == DOC
        assert chunks[0].metadata["doc_short_id"] == "F1"


async def test_document_date_available_for_context_rendering(corpus):
    try:
        async with corpus.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_documents ADD COLUMN last_updated_date DATE")
            await connection.execute("UPDATE public.rag_documents SET last_updated_date = '2026-09-01'")
        store = ContentStore(corpus)
        assert (await store.documents((DOC,)))[0].updated_date == "2026-09-01"
        assert (await store.sections((SEC1,)))[0].document.updated_date == "2026-09-01"
    finally:
        async with corpus.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_documents DROP COLUMN last_updated_date")


async def test_search_input_cannot_inject_sql(corpus):
    store = SearchStore(corpus)
    with pytest.raises(ValueError, match="source"):
        await store.search(SearchRequest("matte; DROP TABLE rag_config", "lexical"))
    with pytest.raises(ValueError):
        await store.search(SearchRequest("matte", "vector", embedding=(float("nan"),)))
    with pytest.raises(ValueError):
        await store.search(SearchRequest("matte", "lexical", limit=-1))
    assert await store.search(SearchRequest("matte", "lexical", query="'; DROP TABLE rag_config; --")) == ()
    async with corpus.transaction() as connection:
        assert await (await connection.execute("SELECT count(*) FROM public.rag_config")).fetchone() == (1,)
