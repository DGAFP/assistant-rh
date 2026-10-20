from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from assistant_rh_api.core.documents import DocumentService
from assistant_rh_api.core.errors import IndividualIdentityRequired
from assistant_rh_api.core.errors.documents import DocumentNotFound
from assistant_rh_api.core.models.conversations import RunSource, TraceEvent
from assistant_rh_api.db.documents import DocumentStore
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import individual_context, service
from apps.api.tests.db.test_run_feedback_stores import make_run

pytestmark = pytest.mark.anyio


async def persist(database, *, data=None):
    doc_id, legacy_id = uuid4(), uuid4() if data is not None else None
    async with database.transaction() as connection:
        if data is not None:
            await connection.execute("INSERT INTO public.documents(id, filename, data) VALUES (%s, 'unsafe.pdf', %s)", (legacy_id, data))
        await connection.execute(
            "INSERT INTO public.rag_documents(doc_id, title, legacy_doc_id, storage_path) VALUES (%s, 'PDF', %s, 'mi/private.pdf')",
            (doc_id, legacy_id),
        )
    return doc_id, legacy_id


async def test_final_sources_author_ministry_and_b4_isolation(repository_db):
    context = individual_context()
    doc_id, _ = await persist(repository_db)
    candidate_id, _ = await persist(repository_db)
    run = make_run(
        author_user_id=context.user_id,
        group_slug=context.group_slug,
        sources=(RunSource("final", "Final", "", str(doc_id)),),
        events=(TraceEvent("retrieval", output_ref={"candidate": str(candidate_id)}),),
    )
    runs = ChatRunStore(repository_db)
    await runs.finalize(run)
    documents = DocumentService(runs, DocumentStore(repository_db), None)
    assert await documents.authorize("chatcmpl-" + run.turn_id, "final", context) == doc_id
    # No shared-group ownership: second individual has the same group label/rights.
    for auth in (
        individual_context(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")),
        individual_context(ministries=()),
        individual_context(ministries=("mi",), ministry="mi"),
        individual_context(ministry="mi"),
    ):
        with pytest.raises(DocumentNotFound):
            await documents.authorize(run.turn_id, "final", auth)
    for turn_id, ref in ((run.turn_id, str(candidate_id)), (uuid4().hex, "final"), (run.turn_id, str(doc_id))):
        with pytest.raises(DocumentNotFound):
            await documents.authorize(turn_id, ref, context)
    # A valid reference to a public source also requires ownership of its run.
    public = make_run(author_user_id=context.user_id, sources=(RunSource("public", "Public", "https://www.service-public.fr/a", str(doc_id)),))
    await runs.finalize(public)
    with pytest.raises(DocumentNotFound):
        await documents.authorize(public.turn_id, "public", individual_context(uuid4()))
    group = (await service().login("beta", "password", "local")).context
    with pytest.raises(IndividualIdentityRequired):
        await documents.authorize(run.turn_id, "final", group)
    collective = replace(make_run(), sources=run.sources)
    await runs.finalize(collective)
    with pytest.raises(DocumentNotFound):
        await documents.authorize(collective.turn_id, "final", context)
    # Source access resumes when the same individual regains the ministry right.
    assert await documents.authorize(run.turn_id, "final", context) == doc_id


async def test_legacy_pdf_uses_sql_slices_stable_snapshot_and_runtime_grants(repository_db):
    data = b"%PDF-1.7\n" + b"synthetic" * 250000
    doc_id, legacy_id = await persist(repository_db, data=data)
    store = DocumentStore(repository_db)
    async with store.open(doc_id) as stored:
        assert stored.legacy is not None and stored.legacy.size == len(data)
        # Concurrent edits cannot change bytes halfway through this transfer.
        async with repository_db.transaction() as connection:
            await connection.execute("UPDATE public.documents SET data = 'changed'::bytea WHERE id = %s", (legacy_id,))
        size = 0
        async for chunk in stored.legacy.chunks:
            assert len(chunk) <= 65536 and chunk == data[size : size + len(chunk)]
            size += len(chunk)
        assert size == len(data)
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.documents SET data = %s WHERE id = %s", (data, legacy_id))
        await connection.execute("SET LOCAL ROLE arh_api")
        # The migration grants only the ID and binary data columns.
        assert (await (await connection.execute("SELECT substring(data FROM 1 FOR 5) FROM public.documents WHERE id = %s", (legacy_id,))).fetchone())[
            0
        ] == b"%PDF-"


async def test_legacy_close_releases_snapshot_and_invalid_documents_are_uniform(repository_db):
    store = DocumentStore(repository_db)
    for data in (b"<html>unsafe", b"", None):
        doc_id, _ = await persist(repository_db, data=data)
        if data is None:
            async with store.open(doc_id) as stored:
                assert stored.legacy is None and stored.storage_path == "mi/private.pdf"
        else:
            with pytest.raises(DocumentNotFound):
                async with store.open(doc_id):
                    pass
    with pytest.raises(DocumentNotFound):
        async with store.open(uuid4()):
            pass
    doc_id, _ = await persist(repository_db, data=b"%PDF-" + b"x" * 200000)
    async with store.open(doc_id) as stored:
        assert len(await anext(stored.legacy.chunks)) <= 65536
    # A partial read keeps no lease. The same database can acquire again.
    async with repository_db.transaction() as connection:
        assert await (await connection.execute("SELECT 1")).fetchone() == (1,)


async def test_slow_legacy_readers_do_not_consume_runtime_pool(repository_db):
    doc_id, _ = await persist(repository_db, data=b"%PDF-" + b"x" * 200000)
    app = create_app(database=repository_db, environ={})
    async with app.router.lifespan_context(app):
        store = app.state.document_service.documents
        assert store.database is not repository_db
        async with store.open(doc_id) as first, store.open(doc_id) as second:
            assert len(await anext(first.legacy.chunks)) <= 65536
            assert len(await anext(second.legacy.chunks)) <= 65536
            # Both dedicated leases are occupied, while runtime/auth stays usable.
            assert store.database.statistics()["pool_available"] == 0
            async with repository_db.transaction(read_only=True) as connection:
                assert await (await connection.execute("SELECT 1")).fetchone() == (1,)
    assert store.database.closed
