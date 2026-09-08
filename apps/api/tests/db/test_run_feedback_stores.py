import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.core.runtime import ChatRun, FeedbackInput, RunSource, TraceEvent
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.gateways.ids import CompletionIds

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def make_run(**changes):
    run = ChatRun(
        CompletionIds().new_id(),
        "f" * 32,
        NOW,
        "synthetic",
        "a" * 64,
        "conversation",
        "Question",
        "Réponse",
        "matte",
        "synthetic-model",
        (RunSource("doc-2", "Second", "https://example.invalid/2"), RunSource("doc-1", "First", "https://example.invalid/1")),
        (TraceEvent("query", metrics={"tokens": 1}), TraceEvent("generation")),
        {"context": ("synthetic",)},
    )
    return replace(run, **changes)


async def test_run_atomic_roundtrip_collision_and_source_ownership(repository_db):
    store = ChatRunStore(repository_db)
    run = make_run()
    assert await store.get(run.turn_id) is None
    await store.finalize(run)
    assert await store.get(run.turn_id) == run
    assert await store.sources(run.turn_id, run.group_slug) == run.sources
    assert await store.sources(run.turn_id, "other") == ()
    with pytest.raises(DatabaseConflict):
        await store.finalize(replace(run, answer="overwrite", sources=()))
    assert await store.get(run.turn_id) == run
    # Failure after writing the run and first source must roll all writes back.
    broken = make_run(sources=(run.sources[0], run.sources[0]))
    with pytest.raises(DatabaseConflict):
        await store.finalize(broken)
    assert await store.get(broken.turn_id) is None
    async with repository_db.transaction() as connection:
        assert await (await connection.execute("SELECT count(*) FROM public.chat_run_sources WHERE turn_id = %s", (broken.turn_id,))).fetchone() == (
            0,
        )
    ids = {CompletionIds().new_id() for _ in range(1000)}
    assert len(ids) == 1000 and all(len(i) == 41 for i in ids)
    broken_trace = make_run(events=(TraceEvent("query"), TraceEvent(None)))
    with pytest.raises(DatabaseConflict):
        await store.finalize(broken_trace)
    assert await store.get(broken_trace.turn_id) is None
    async with repository_db.transaction() as connection:
        assert await (
            await connection.execute("SELECT count(*) FROM public.rag_trace_events WHERE turn_id = %s", (broken_trace.turn_id,))
        ).fetchone() == (0,)


async def test_legacy_run_still_readable_without_source_authority(repository_db):
    async with repository_db.transaction() as connection:
        await connection.execute(
            "INSERT INTO public.chat_runs(turn_id, ts, question, answer) VALUES ('old12345', %s, 'Old', 'Answer')",
            (NOW.replace(tzinfo=None),),
        )
    run = await ChatRunStore(repository_db).get("old12345")
    assert run.question == "Old" and run.sources == () and run.timestamp == NOW


@pytest.mark.parametrize("stars", [1, 2, 3, 4, 5, None])
async def test_feedback_storage_encoding_and_idempotence(repository_db, stars):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    store = FeedbackStore(repository_db)
    assert await store.get(run.turn_id) is None
    value = FeedbackInput(run.turn_id, stars, "Comment", helpful=True)
    saved = await store.save(value, run.group_slug, run.session_hash, NOW)
    assert saved.value == value
    assert await store.get(run.turn_id) == saved
    assert await store.save(value, run.group_slug, run.session_hash, NOW) == saved
    async with repository_db.transaction() as connection:
        assert await (await connection.execute("SELECT stars FROM public.chat_feedbacks")).fetchone() == (stars - 1 if stars else None,)
        assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (0,)


async def test_feedback_concurrent_first_submission_and_replacement_preserves_human_annotations(repository_db):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    store = FeedbackStore(repository_db)
    value = FeedbackInput(run.turn_id, 1, "Initial")
    results = await asyncio.gather(*(store.save(value, run.group_slug, run.session_hash, NOW) for _ in range(4)))
    assert len({r.id for r in results}) == 1
    old = results[0]
    assert await store.save_analysis(old.id, old.revision, "retrieval", "Reason", NOW)
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.chat_feedbacks SET beta_scope = 'human', theme = 'congé'")
    revised = await store.save(replace(value, stars=2), run.group_slug, run.session_hash, NOW)
    assert revised.annotations == {"beta_scope": "human", "theme": "congé"}
    assert revised.analysis_category is None and revised.analysis_reason is None
    assert not await store.save_analysis(old.id, old.revision, "stale", "Old result", NOW)
    # Even with the same timestamp and original input, old analysis is stale.
    final = await store.save(value, run.group_slug, run.session_hash, NOW)
    assert final.revision != old.revision
    assert not await store.save_analysis(old.id, old.revision, "stale", "Old result", NOW)
    pending = await store.for_analysis(3, 10)
    assert len(pending) == 1 and pending[0].run_context["api_record"]["diagnostics"]["context"] == ("synthetic",)
    assert await store.save_analysis(final.id, final.revision, "retrieval", "Current", NOW)
    assert await store.for_analysis(3, 10) == ()
    async with repository_db.transaction() as connection:
        rows = await (await connection.execute("SELECT record FROM public.chat_feedback_audit ORDER BY audit_id")).fetchall()
        assert len(rows) == 2 and rows[0][0]["ai_reason"] == "Reason"
    # A different session in the same group may edit; audit records that actor,
    # while the archived JSON retains the author of the previous version.
    await store.save(replace(value, stars=5), run.group_slug, "b" * 64, NOW)
    async with repository_db.transaction() as connection:
        actor = await (
            await connection.execute("""
            SELECT group_slug, audit_session_hash, record->>'api_session_hash'
            FROM public.chat_feedback_audit ORDER BY audit_id DESC LIMIT 1
        """)
        ).fetchone()
        assert actor == (run.group_slug, "b" * 64, run.session_hash)


async def test_feedback_denied_missing_and_rollback(repository_db):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    store = FeedbackStore(repository_db)
    value = FeedbackInput(run.turn_id, 1)
    assert await store.save(value, "other", run.session_hash, NOW) is None
    assert await store.save(replace(value, turn_id="missing"), run.group_slug, run.session_hash, NOW) is None
    original = await store.save(value, run.group_slug, run.session_hash, NOW)
    try:
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.chat_feedbacks ADD CONSTRAINT synthetic_no_three CHECK(stars <> 2)")
        with pytest.raises(DatabaseConflict):
            await store.save(replace(value, stars=3), run.group_slug, run.session_hash, NOW)
        assert await store.get(run.turn_id) == original
        async with repository_db.transaction() as connection:
            assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (0,)
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.chat_feedbacks DROP CONSTRAINT synthetic_no_three")


async def test_legacy_feedback_analysis_without_run(repository_db):
    async with repository_db.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO public.chat_feedbacks(turn_id, ts, stars, helpful, question, answer)
            VALUES ('legacy', %s, 0, FALSE, 'Legacy question', 'Legacy answer')
        """,
            (NOW.replace(tzinfo=None),),
        )
    store = FeedbackStore(repository_db)
    legacy = await store.get("legacy")
    assert legacy.value.stars == 1 and legacy.value.helpful is False
    pending = await store.for_analysis(3, 10)
    assert pending[0].run_context is None and pending[0].question == "Legacy question"


async def test_nullable_historical_timestamps(repository_db):
    async with repository_db.transaction() as connection:
        await connection.execute("INSERT INTO public.chat_runs(turn_id, question) VALUES ('nullts', 'Old question')")
        await connection.execute("INSERT INTO public.chat_feedbacks(turn_id, stars) VALUES ('nullts', 0)")
    assert (await ChatRunStore(repository_db).get("nullts")).timestamp is None
    assert (await FeedbackStore(repository_db).get("nullts")).timestamp is None


async def test_feedback_reason_collections_roundtrip_and_legacy_read(repository_db):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    store = FeedbackStore(repository_db)
    value = FeedbackInput(run.turn_id, 4, reasons_positive=("Clair", "Utile"), reasons_negative=("Incomplet",))
    saved = await store.save(value, run.group_slug, run.session_hash, NOW)
    assert saved.value == value
    assert await store.get(run.turn_id) == saved
    assert await store.save(value, run.group_slug, run.session_hash, NOW) == saved
    async with repository_db.transaction() as connection:
        row = await (await connection.execute("SELECT reasons_positive, reasons_negative FROM public.chat_feedbacks")).fetchone()
        assert row == ("Clair; Utile", "Incomplet")
        assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (0,)
        await connection.execute("""
            INSERT INTO public.chat_feedbacks(turn_id, stars, reasons_positive, reasons_negative)
            VALUES ('legacy-reasons', 2, NULL, ' Confus ; Incomplet; ')
        """)
    legacy = await store.get("legacy-reasons")
    assert legacy.value.reasons_positive == ()
    assert legacy.value.reasons_negative == ("Confus", "Incomplet")
    cleared = await store.save(replace(value, reasons_positive=(), reasons_negative=()), run.group_slug, run.session_hash, NOW)
    assert cleared.value.reasons_positive == cleared.value.reasons_negative == ()
    assert cleared.revision != saved.revision


@pytest.mark.parametrize("reasons", [("Clair; Utile",), (" Clair",), ("",), "Clair"])
async def test_feedback_rejects_reasons_that_cannot_roundtrip(repository_db, reasons):
    value = FeedbackInput("missing", 4, reasons_positive=reasons)
    with pytest.raises(ValueError, match="reasons must be a tuple"):
        await FeedbackStore(repository_db).save(value, "synthetic", "a" * 64, NOW)
