from pathlib import Path

import psycopg

MIGRATION = Path(__file__).resolve().parents[4] / "supabase/migrations/20260908094542_api_runtime_repositories.sql"


def test_migration_deduplicates_losslessly_ties_idempotence_and_legacy_inserts(repository_dsn):
    # Transactional DDL rollback keeps the surrounding fixture intact, including
    # the compatibility trigger disabled only to construct the pre-B2 shape.
    with psycopg.connect(repository_dsn) as connection:
        try:
            connection.execute("TRUNCATE public.chat_feedbacks, public.chat_feedback_audit RESTART IDENTITY")
            connection.execute("DROP TRIGGER api_legacy_feedback_insert ON public.chat_feedbacks")
            connection.execute("DROP INDEX public.chat_feedbacks_turn_unique")
            connection.execute("""
                INSERT INTO public.chat_feedbacks(turn_id, ts, stars, comment, beta_scope, theme)
                VALUES ('legacy', '2026-09-01', 0, 'older', 'human', 'theme'),
                       ('legacy', '2026-09-02', 1, 'same-time-lower-id', 'human', 'theme'),
                       ('legacy', '2026-09-02', 4, 'winner', 'human', 'theme');
            """)
            original = connection.execute("SELECT id, to_jsonb(f) FROM public.chat_feedbacks f ORDER BY id").fetchall()
            connection.execute(MIGRATION.read_text())
            assert connection.execute("SELECT id, stars, comment FROM public.chat_feedbacks").fetchall() == [(3, 4, "winner")]
            archived = connection.execute("SELECT feedback_id, record FROM public.chat_feedback_audit ORDER BY feedback_id").fetchall()
            assert archived == original[:2]
            connection.execute(MIGRATION.read_text())
            assert connection.execute("SELECT count(*) FROM public.chat_feedback_audit").fetchone() == (2,)
            # Exact legacy INSERT shape remains accepted with uniqueness enabled.
            connection.execute("""
                INSERT INTO public.chat_feedbacks(turn_id, ts, stars, comment)
                VALUES ('legacy', '2026-09-03', 2, 'replacement')
            """)
            assert connection.execute("SELECT stars, comment, beta_scope, theme FROM public.chat_feedbacks").fetchone() == (
                2,
                "replacement",
                "human",
                "theme",
            )
            assert connection.execute("SELECT count(*) FROM public.chat_feedback_audit").fetchone() == (3,)
            connection.execute("INSERT INTO public.chat_feedbacks(turn_id, ts, stars) VALUES ('legacy', '2026-08-01', 0)")
            assert connection.execute("SELECT stars FROM public.chat_feedbacks").fetchone() == (2,)
            assert connection.execute("SELECT count(*) FROM public.chat_feedback_audit").fetchone() == (4,)
            # Sequence allocation can precede lock acquisition: id 20 arrives
            # before id 19 at the same timestamp. The former must remain current.
            connection.execute("""
                INSERT INTO public.chat_feedbacks(id, turn_id, ts, stars) VALUES (20, 'legacy', '2026-09-04', 4);
                INSERT INTO public.chat_feedbacks(id, turn_id, ts, stars) VALUES (19, 'legacy', '2026-09-04', 0);
            """)
            assert connection.execute("SELECT stars FROM public.chat_feedbacks").fetchone() == (4,)
        finally:
            connection.rollback()


def test_identifier_widening_with_legacy_foreign_keys(repository_dsn):
    with psycopg.connect(repository_dsn) as connection:
        try:
            connection.execute("TRUNCATE public.chat_runs, public.chat_run_sources, public.chat_feedbacks")
            connection.execute("DROP TABLE public.chat_run_sources")
            connection.execute("ALTER TABLE public.chat_runs ALTER COLUMN turn_id TYPE VARCHAR(8)")
            connection.execute("ALTER TABLE public.chat_feedbacks ALTER COLUMN turn_id TYPE VARCHAR(8)")
            connection.execute(
                "ALTER TABLE public.chat_feedbacks ADD CONSTRAINT synthetic_run_fk FOREIGN KEY(turn_id) REFERENCES public.chat_runs(turn_id)"
            )
            connection.execute("CREATE TABLE public.chat_reviews (turn_id VARCHAR(8) PRIMARY KEY REFERENCES public.chat_runs(turn_id), notes TEXT)")
            connection.execute(MIGRATION.read_text())
            turn_id = "chatcmpl-" + "a" * 32
            connection.execute("INSERT INTO public.chat_runs(turn_id) VALUES (%s)", (turn_id,))
            connection.execute("INSERT INTO public.chat_feedbacks(turn_id, stars) VALUES (%s, 4)", (turn_id,))
            connection.execute("INSERT INTO public.chat_reviews(turn_id) VALUES (%s)", (turn_id,))
            assert connection.execute("SELECT turn_id FROM public.chat_reviews").fetchone() == (turn_id,)
            connection.execute(MIGRATION.read_text())
        finally:
            connection.rollback()
