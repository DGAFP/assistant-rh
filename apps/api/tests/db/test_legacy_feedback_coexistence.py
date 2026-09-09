"""Exercise the actual legacy analyzer and INSERTs alongside the API stores."""

import asyncio
from datetime import datetime, timezone

import psycopg
import pytest
from assistant_rh_api.core.models.conversations import ChatRun, FeedbackInput
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.gateways.ids import CompletionIds
from assistant_rh_rag_pipeline import feedback_analyzer
from sqlalchemy import create_engine

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def make_run():
    return ChatRun(CompletionIds().new_id(), "synthetic", NOW, "synthetic", "a" * 64, "conversation", "Question", "Answer", "matte", "synthetic")


@pytest.fixture
def legacy_engine(repository_dsn):
    engine = create_engine(repository_dsn.replace("postgresql://", "postgresql+psycopg://"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("writer", ["legacy", "api"])
@pytest.mark.parametrize("restore_original", [False, True], ids=["replacement", "a-b-a"])
async def test_legacy_analysis_checks_the_generation_read_before_llm(repository_db, legacy_engine, monkeypatch, writer, restore_original):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    store = FeedbackStore(repository_db)

    async def submit(comment):
        if writer == "api":
            await store.save(FeedbackInput(run.turn_id, 1, comment), run.group_slug, run.session_hash, NOW)
        else:
            async with repository_db.transaction() as connection:
                await connection.execute(
                    """
                    INSERT INTO public.chat_feedbacks(turn_id, ts, stars, comment, question, answer)
                    VALUES (%s, %s, 0, %s, 'Question', 'Answer')
                """,
                    (run.turn_id, NOW.replace(tzinfo=None), comment),
                )

    await submit("Original")
    pending = feedback_analyzer._get_unanalyzed_feedbacks(legacy_engine)
    assert len(pending) == 1 and pending[0]["api_revision"] == 0
    # The analyzer has captured its payload before a concurrent replacement.
    await submit("Replacement")
    if restore_original:
        await submit("Original")
    monkeypatch.setattr(feedback_analyzer, "_extract_markers", lambda feedback: ("", []))
    monkeypatch.setattr(feedback_analyzer, "_call_albert", lambda *args: ("retrieval_issue", "Synthetic analysis"))
    with pytest.raises(RuntimeError, match="échec de persistance"):
        feedback_analyzer.analyze_single(pending[0], engine=legacy_engine)
    current = await store.get(run.turn_id)
    assert current.value.comment == ("Original" if restore_original else "Replacement")
    assert current.analysis_reason is None and current.analysis_category is None
    fresh = feedback_analyzer._get_unanalyzed_feedbacks(legacy_engine)
    assert fresh[0]["id"] == pending[0]["id"]
    assert fresh[0]["api_revision"] == (2 if restore_original else 1)
    assert feedback_analyzer.analyze_single(fresh[0], engine=legacy_engine) == ("retrieval_issue", "Synthetic analysis")
    # Another worker holding the same generation cannot overwrite an analysis.
    assert not feedback_analyzer._save_analysis(legacy_engine, current.id, "other", "Duplicate", fresh[0]["api_revision"])
    assert feedback_analyzer._get_unanalyzed_feedbacks(legacy_engine) == []


async def test_legacy_analyzer_works_before_revision_migration(repository_db, repository_dsn):
    # A separate synthetic schema exercises the actual SQL without api_revision,
    # while the rest of the repository tests continue using the migrated schema.
    async with repository_db.transaction() as connection:
        await connection.execute("""
            CREATE SCHEMA synthetic_pre_b2;
            CREATE TABLE synthetic_pre_b2.chat_feedbacks (LIKE public.chat_feedbacks INCLUDING ALL);
            CREATE TABLE synthetic_pre_b2.chat_runs (LIKE public.chat_runs INCLUDING ALL);
            ALTER TABLE synthetic_pre_b2.chat_feedbacks DROP COLUMN api_revision;
            INSERT INTO synthetic_pre_b2.chat_feedbacks(turn_id, stars, question, answer)
            VALUES ('legacy', 0, 'Question', 'Answer');
        """)
    engine = create_engine(
        repository_dsn.replace("postgresql://", "postgresql+psycopg://"),
        connect_args={"options": "-csearch_path=synthetic_pre_b2,public"},
    )
    try:
        pending = feedback_analyzer._get_unanalyzed_feedbacks(engine)
        assert len(pending) == 1 and pending[0]["api_revision"] == 0
        assert feedback_analyzer._save_analysis(engine, pending[0]["id"], "retrieval_issue", "Synthetic", 0)
        assert not feedback_analyzer._save_analysis(engine, pending[0]["id"], "other", "Duplicate", 0)
        assert not feedback_analyzer._save_analysis(engine, -1, "other", "Missing", 0)
        assert feedback_analyzer._get_unanalyzed_feedbacks(engine) == []
    finally:
        engine.dispose()
        async with repository_db.transaction() as connection:
            await connection.execute("DROP SCHEMA synthetic_pre_b2 CASCADE")


async def test_legacy_and_api_first_feedback_with_historical_foreign_key(repository_db, repository_dsn):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    async with repository_db.transaction() as connection:
        await connection.execute("""
            ALTER TABLE public.chat_feedbacks ADD CONSTRAINT synthetic_run_fk
            FOREIGN KEY(turn_id) REFERENCES public.chat_runs(turn_id)
        """)
    legacy = await psycopg.AsyncConnection.connect(repository_dsn)
    observer = await psycopg.AsyncConnection.connect(repository_dsn, autocommit=True)
    task = None
    try:
        await legacy.execute("SET LOCAL statement_timeout = '5s'")
        # Pause at the legacy trigger's advisory lock, before the implicit FK
        # check. The API must be able to serialize without blocking KEY SHARE.
        await legacy.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 454))", (run.turn_id,))
        task = asyncio.create_task(FeedbackStore(repository_db).save(FeedbackInput(run.turn_id, 1, "API"), "synthetic", "b" * 64, NOW))
        async with asyncio.timeout(5):
            while True:
                # The API waits for this legacy transaction's advisory lock.
                blocked = await (
                    await observer.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid)))",
                        (legacy.info.backend_pid,),
                    )
                ).fetchone()
                if blocked[0]:
                    break
                await asyncio.sleep(0.01)
        await legacy.execute(
            "INSERT INTO public.chat_feedbacks(turn_id, ts, stars, comment) VALUES (%s, %s, 0, 'LEGACY')",
            (run.turn_id, NOW.replace(tzinfo=None)),
        )
        await legacy.commit()
        result = await task
        assert result.value.comment == "API"
        async with repository_db.transaction(read_only=True) as connection:
            assert await (await connection.execute("SELECT count(*) FROM public.chat_feedbacks")).fetchone() == (1,)
            assert await (await connection.execute("SELECT record->>'comment' FROM public.chat_feedback_audit")).fetchone() == ("LEGACY",)
    finally:
        await legacy.close()
        await observer.close()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.chat_feedbacks DROP CONSTRAINT synthetic_run_fk")
