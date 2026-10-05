"""D1 proofs use synthetic principals; verified individual login remains #596."""

import asyncio
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.core.feedback import FeedbackService
from assistant_rh_api.core.models.conversations import FeedbackInput
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.auth import resolve_bearer

from apps.api.tests.auth_fakes import Clock, individual_context, service
from apps.api.tests.db.conftest import FEEDBACK_MIGRATION
from apps.api.tests.db.test_run_feedback_stores import NOW, make_run

pytestmark = pytest.mark.anyio
AUTHOR = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.fixture
async def individual_http(repository_db):
    runs, store = ChatRunStore(repository_db), FeedbackStore(repository_db)
    run = make_run(author_user_id=AUTHOR)
    run = replace(run, turn_id=run.turn_id.removeprefix("chatcmpl-"))
    historical = make_run()
    historical = replace(historical, turn_id=historical.turn_id.removeprefix("chatcmpl-"))
    await runs.finalize(run)
    await runs.finalize(historical)
    auth = individual_context()
    app = create_app(feedback_service=FeedbackService(store, Clock()))

    async def resolved():
        return auth

    app.dependency_overrides[resolve_bearer] = resolved
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, run, historical, auth, store, runs


async def test_api_roundtrip_normalization_retry_and_audit(individual_http, repository_db):
    client, run, _, auth, store, _ = individual_http
    assert (await client.get("/v1/feedback/" + run.turn_id)).status_code == 404
    body = {"completion_id": run.turn_id, "stars": 4, "comment": "  Merci  ", "reasons_positive": ["Utile", "Clair", "Utile"]}
    responses = await asyncio.gather(*(client.post("/v1/feedback", json=body) for _ in range(4)))
    assert all(r.status_code == 204 and r.content == b"" and r.headers["cache-control"] == "no-store" for r in responses)
    first = await store.get(run.turn_id)
    assert first.value == FeedbackInput(run.turn_id, 4, "Merci", ("Clair", "Utile"), helpful=True)
    async with repository_db.transaction() as connection:
        before = await (
            await connection.execute("SELECT xmin::text, ctid::text, api_revision, api_actor_user_id FROM public.chat_feedbacks")
        ).fetchone()
        assert before[2:] == (0, AUTHOR)
        assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (0,)
    # Different spelling/order, equivalent canonical payload: strictly no UPDATE.
    body.update(completion_id="chatcmpl-" + run.turn_id, comment="Merci", reasons_positive=["Clair", "Utile"])
    assert (await client.post("/v1/feedback", json=body)).status_code == 204
    async with repository_db.transaction() as connection:
        after = await (
            await connection.execute("SELECT xmin::text, ctid::text, api_revision, api_actor_user_id FROM public.chat_feedbacks")
        ).fetchone()
        assert before == after
        await connection.execute("UPDATE public.chat_feedbacks SET beta_scope = 'human', theme = 'congé'")
    assert await store.save_analysis(first.id, first.revision, "retrieval", "Obsolete", NOW)
    body.update(stars=1, reasons_positive=[], reasons_negative=["Sources manquantes"])
    responses = await asyncio.gather(*(client.post("/v1/feedback", json=body) for _ in range(4)))
    assert all(r.status_code == 204 for r in responses)
    current = await store.get(run.turn_id)
    assert current.annotations == {"beta_scope": "human", "theme": "congé"}
    assert current.analysis_category is None and current.analysis_reason is None
    assert not await store.save_analysis(first.id, first.revision, "stale", "Stale", NOW)
    assert len(await store.for_analysis(3, 10)) == 1
    async with repository_db.transaction() as connection:
        row = await (
            await connection.execute("SELECT actor_user_id, group_slug, audit_session_hash, record FROM public.chat_feedback_audit")
        ).fetchone()
        assert row[:3] == (AUTHOR, auth.group.slug, auth.audit_session_hash)
        assert row[3]["api_actor_user_id"] == str(AUTHOR) and row[3]["ai_reason"] == "Obsolete"
        assert "c" * 64 not in str(row)  # authentication lookup hash is not an audit pseudonym
        assert await (await connection.execute("SELECT ai_analyzed_at FROM public.chat_feedbacks")).fetchone() == (None,)
    read = await client.get("/v1/feedback/chatcmpl-" + run.turn_id)
    assert read.status_code == 200 and read.headers["cache-control"] == "no-store"
    assert read.json() == {**body, "completion_id": "chatcmpl-" + run.turn_id, "helpful": False}
    assert "human" not in read.text and "Obsolete" not in read.text


async def test_api_isolation_current_rights_historical_and_reconnection(individual_http):
    client, run, historical, auth, store, _ = individual_http
    assert (await client.post("/v1/feedback", json={"completion_id": run.turn_id, "stars": 3, "comment": "Initial"})).status_code == 204
    for denied in (
        replace(auth, user_id=OTHER),
        replace(auth, group=replace(auth.group, slug="other")),
        replace(auth, group=replace(auth.group, allowed_ministries=("mi",), default_ministry="mi")),
    ):
        client._transport.app.dependency_overrides[resolve_bearer] = lambda: denied
        for target in (run.turn_id, "unknown", historical.turn_id):
            get = await client.get("/v1/feedback/" + target)
            post = await client.post("/v1/feedback", json={"completion_id": target, "stars": 3, "comment": "Denied"})
            assert get.status_code == post.status_code == 404
            assert (
                get.json()
                == post.json()
                == {"error": {"message": "Feedback not found", "type": "invalid_request_error", "code": "feedback_not_found"}}
            )
    # Same internal author after reconnecting, with a new session pseudonym.
    reconnect = replace(auth, session=replace(auth.session, token_hash="e" * 64), audit_session_hash="f" * 64)
    client._transport.app.dependency_overrides[resolve_bearer] = lambda: reconnect
    assert (await client.get("/v1/feedback/" + run.turn_id)).status_code == 200
    assert (await client.post("/v1/feedback", json={"completion_id": run.turn_id, "stars": 3, "comment": "Edited"})).status_code == 204
    assert (await store.get(run.turn_id)).value.comment == "Edited"
    assert (await client.get("/v1/feedback/" + historical.turn_id)).status_code == 404
    client._transport.app.dependency_overrides[resolve_bearer] = lambda: replace(auth, user_id=None)
    for target in (run.turn_id, historical.turn_id, "unknown"):
        assert (await client.get("/v1/feedback/" + target)).status_code == 403
        assert (await client.post("/v1/feedback", json={"completion_id": target, "stars": 3, "comment": "Group"})).status_code == 403


async def test_author_roundtrip_immutable_and_legacy_cannot_write_individual(individual_http, repository_db):
    _, run, historical, _, store, runs = individual_http
    assert await runs.get(run.turn_id) == run
    assert (await runs.get(historical.turn_id)).author_user_id is None
    assert await runs.sources(run.turn_id, run.group_slug, ministries=("matte",)) == ()
    for target in (run.turn_id, historical.turn_id):
        with pytest.raises(DatabaseConflict):
            async with repository_db.transaction() as connection:
                await connection.execute("UPDATE public.chat_runs SET author_user_id = %s WHERE turn_id = %s", (OTHER, target))
    for existing in (False, True):
        if existing:
            await store.save(FeedbackInput(run.turn_id, 1, "Owned"), run.group_slug, run.session_hash, NOW, user_id=AUTHOR, ministries=("matte",))
        assert await store.save(FeedbackInput(run.turn_id, 1, "Group"), run.group_slug, run.session_hash, NOW, ministries=("matte",)) is None
        with pytest.raises(DatabaseConflict):
            async with repository_db.transaction() as connection:
                await connection.execute("INSERT INTO public.chat_feedbacks(turn_id, stars, comment) VALUES (%s, 0, %s)", (run.turn_id, "Legacy"))
        current = await store.get(run.turn_id)
        assert current.value.comment == "Owned" if existing else current is None
    # Coexistence still allows historical group writes and has no backfill.
    await store.save(FeedbackInput(historical.turn_id, 1, "Historical"), historical.group_slug, historical.session_hash, NOW, ministries=("matte",))
    async with repository_db.transaction() as connection:
        await connection.execute(FEEDBACK_MIGRATION.read_text())
    assert (await runs.get(historical.turn_id)).author_user_id is None


async def test_individual_rollback_restores_audit_analysis_and_current(individual_http, repository_db):
    client, run, _, _, store, _ = individual_http
    body = {"completion_id": run.turn_id, "stars": 1, "comment": "Initial"}
    assert (await client.post("/v1/feedback", json=body)).status_code == 204
    initial = await store.get(run.turn_id)
    assert await store.save_analysis(initial.id, initial.revision, "retrieval", "Original", NOW)
    original = await store.get(run.turn_id)
    async with repository_db.transaction() as connection:
        await connection.execute("ALTER TABLE public.chat_feedbacks ADD CONSTRAINT d1_test_no_three CHECK(stars <> 2)")
    try:
        response = await client.post("/v1/feedback", json={**body, "stars": 3})
        assert response.status_code == 500 and "d1_test_no_three" not in response.text
        assert await store.get(run.turn_id) == original
        assert await store.for_analysis(3, 10) == ()
        async with repository_db.transaction() as connection:
            assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (0,)
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.chat_feedbacks DROP CONSTRAINT d1_test_no_three")


async def test_real_b4_auth_never_supplies_individual_identity(repository_db, caplog):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    app = create_app(auth_service=auth, feedback_service=FeedbackService(FeedbackStore(repository_db), Clock()))
    headers = {"Authorization": "Bearer " + issued.access_token, "X-User-Id": str(AUTHOR)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post("/v1/feedback", json={"completion_id": "unknown", "stars": 3, "comment": "Test"}, headers=headers)
        ).status_code == 403
        assert (await client.get("/v1/feedback/unknown", headers=headers)).status_code == 403
        assert (await client.get("/v1/feedback/unknown")).status_code == 401
        await auth.logout(issued.context)
        assert (await client.get("/v1/feedback/unknown", headers=headers)).status_code == 401

    assert issued.access_token not in caplog.text
    assert "fixture-hash" not in caplog.text


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("prefix", ["", "chatcmpl-"])
async def test_collective_history_cannot_be_read_or_claimed(individual_http, repository_db, existing, prefix):
    client, _, historical, _, store, runs = individual_http
    # Same group and permitted corpus, real canonical ID: only ownership denies access.
    assert await runs.get(historical.turn_id) == historical
    before = None
    if existing:
        before = await store.save(
            FeedbackInput(historical.turn_id, 4, "Collective"), historical.group_slug, historical.session_hash, NOW, ministries=("matte",)
        )
        assert before is not None
    target = prefix + historical.turn_id
    read = await client.get("/v1/feedback/" + target)
    write = await client.post("/v1/feedback", json={"completion_id": target, "stars": 3, "comment": "Claim"})
    assert read.status_code == write.status_code == 404
    assert await store.get(historical.turn_id) == before
    async with repository_db.transaction(read_only=True) as connection:
        assert await (await connection.execute("SELECT count(*) FROM public.chat_feedback_audit")).fetchone() == (0,)


async def test_collective_store_requires_current_corpus_rights(individual_http):
    _, _, historical, _, store, runs = individual_http
    value = FeedbackInput(historical.turn_id, 4, "Collective")
    saved = await store.save(value, historical.group_slug, historical.session_hash, NOW, ministries=("matte",))
    assert saved is not None
    assert await store.save(replace(value, comment="Revoked"), historical.group_slug, historical.session_hash, NOW, ministries=("mi",)) is None
    assert await store.get(historical.turn_id) == saved
    assert await runs.sources(historical.turn_id, historical.group_slug, ministries=("mi",)) == ()


async def test_individual_sources_require_author_group_and_current_corpus(individual_http):
    _, run, historical, _, _, runs = individual_http
    assert await runs.sources(run.turn_id, run.group_slug, user_id=AUTHOR, ministries=("matte",)) == run.sources
    for user_id, group, ministries in ((OTHER, run.group_slug, ("matte",)), (AUTHOR, "other", ("matte",)), (AUTHOR, run.group_slug, ("mi",))):
        assert await runs.sources(run.turn_id, group, user_id=user_id, ministries=ministries) == ()
    assert await runs.sources(historical.turn_id, historical.group_slug, user_id=AUTHOR, ministries=("matte",)) == ()
