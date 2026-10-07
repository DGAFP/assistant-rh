"""#599 guards with real roles: only arh_api writes individual feedback content."""

import psycopg
import pytest
from assistant_rh_api.core.models.conversations import FeedbackInput
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.run_store import ChatRunStore
from psycopg import errors

from apps.api.tests.db.test_individual_feedback import AUTHOR
from apps.api.tests.db.test_run_feedback_stores import make_run

pytestmark = pytest.mark.anyio
LOGINS = {"test_rt_api": "arh_api", "test_rt_streamlit": "arh_streamlit", "test_rt_analysis": "arh_analysis"}


@pytest.fixture
def logins(repository_dsn):
    with psycopg.connect(repository_dsn, autocommit=True) as connection:
        for login, role in LOGINS.items():
            if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)).fetchone():
                connection.execute(f"CREATE ROLE {login} LOGIN")
            # Stand-in for Scaleway's permission: schema usage, reads and direct writes.
            connection.execute(f"GRANT USAGE ON SCHEMA public TO {login}")
            connection.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {login}")
            connection.execute("SELECT public.api_attach_runtime_user(%s, %s)", (login, role))
    return repository_dsn


def as_login(dsn, login, sql, params=None):
    with psycopg.connect(dsn) as connection:
        connection.execute(f"SET ROLE {login}")
        return connection.execute(sql, params).rowcount


async def individual_run(repository_db):
    run = make_run(author_user_id=AUTHOR)
    await ChatRunStore(repository_db).finalize(run)
    value = FeedbackInput(run.turn_id, 4, "Merci", ("Clair",), (), True)
    await FeedbackStore(repository_db).save(value, "@conversations", "e" * 64, run.timestamp, user_id=AUTHOR, ministries=("matte",))
    return run


async def test_only_the_api_role_writes_individual_feedback_content(repository_db, logins):
    run = await individual_run(repository_db)
    for login in ("test_rt_streamlit", "test_rt_analysis"):
        with pytest.raises((errors.InsufficientPrivilege, errors.RaiseException)):
            as_login(logins, login, "UPDATE public.chat_feedbacks SET comment = 'forged' WHERE turn_id = %s", (run.turn_id,))
    with pytest.raises(errors.InsufficientPrivilege):
        as_login(logins, "test_rt_streamlit", "DELETE FROM public.chat_feedbacks WHERE turn_id = %s", (run.turn_id,))
    with pytest.raises(errors.InsufficientPrivilege):
        as_login(logins, "test_rt_streamlit", "INSERT INTO public.chat_feedbacks (turn_id, stars, comment) VALUES (%s, 4, 'forged')", (run.turn_id,))
    for login in LOGINS:
        with pytest.raises(errors.InsufficientPrivilege):
            as_login(logins, login, "TRUNCATE public.chat_feedbacks")
        with pytest.raises(errors.InsufficientPrivilege):
            as_login(logins, login, "ALTER TABLE public.chat_feedbacks DISABLE TRIGGER api_feedback_individual_guard")
    # Annotation and analysis columns stay open to their roles.
    assert as_login(logins, "test_rt_streamlit", "UPDATE public.chat_feedbacks SET theme = 'congé' WHERE turn_id = %s", (run.turn_id,)) == 1
    assert as_login(logins, "test_rt_analysis", "UPDATE public.chat_feedbacks SET ai_reason = 'r' WHERE turn_id = %s", (run.turn_id,)) == 1
    assert as_login(logins, "test_rt_api", "UPDATE public.chat_feedbacks SET comment = 'API' WHERE turn_id = %s", (run.turn_id,)) == 1
    with pytest.raises(errors.InsufficientPrivilege):
        as_login(logins, "test_rt_streamlit", "SELECT * FROM public.api_sessions")


async def test_collective_history_stays_writable_by_streamlit(repository_db, logins):
    run = make_run()
    await ChatRunStore(repository_db).finalize(run)
    sql = "INSERT INTO public.chat_feedbacks (turn_id, stars, comment, question, answer) VALUES (%s, 3, %s, 'Q', 'A')"
    assert as_login(logins, "test_rt_streamlit", sql, (run.turn_id, "first")) == 1
    # The legacy trigger archives the previous version as the invoker.
    as_login(logins, "test_rt_streamlit", sql, (run.turn_id, "second"))
    async with repository_db.transaction(read_only=True) as connection:
        comment = await (await connection.execute("SELECT comment FROM public.chat_feedbacks WHERE turn_id = %s", (run.turn_id,))).fetchone()
        audited = await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit WHERE turn_id = %s", (run.turn_id,))).fetchone()
    assert comment == ("second",) and audited == (1,)


def test_attach_refuses_owners_and_unknown_roles(logins):
    with psycopg.connect(logins, autocommit=True) as connection:
        with pytest.raises(errors.RaiseException):
            connection.execute("SELECT public.api_attach_runtime_user('test_rt_api', 'arh_owner')")
        owner = connection.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'public.chat_feedbacks'::regclass").fetchone()[0]
        with pytest.raises(errors.RaiseException):
            connection.execute("SELECT public.api_attach_runtime_user(%s, 'arh_api')", (owner,))
