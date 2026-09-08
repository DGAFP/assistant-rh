from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.core.runtime import Session
from assistant_rh_api.db.auth_stores import GroupStore, SessionStore

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


async def test_groups_roles_absence_and_tie_order(repository_db):
    store = GroupStore(repository_db)
    assert await store.get("absent") is None
    async with repository_db.transaction() as connection:
        await connection.execute("INSERT INTO public.user_groups(slug, label, is_admin, visible) VALUES ('admin', 'Admin', TRUE, FALSE)")
    groups = await store.list_groups()
    assert [g.slug for g in groups] == ["admin", "synthetic"]
    assert groups[0].roles == ("admin",)
    assert groups[1].roles == ("user",)
    assert groups[1].allowed_ministries == ("matte",)
    assert "fixture-hash" not in repr(groups[1])


async def test_sessions_conflict_expiry_revocation_and_legacy_password_reset(repository_db):
    store = SessionStore(repository_db)
    session = Session("a" * 64, "synthetic", NOW, NOW + timedelta(hours=8), "fixture-hash")
    assert await store.get_active(session.token_hash, NOW) is None
    await store.create(session)
    assert await store.get_active(session.token_hash, NOW) == session
    assert await store.get_active(session.token_hash, NOW - timedelta(seconds=1)) is None
    assert await store.get_active(session.token_hash, session.expires_at) is None
    with pytest.raises(DatabaseConflict):
        await store.create(session)
    assert await store.get_active(session.token_hash, NOW) == session
    with pytest.raises(DatabaseConflict):
        await store.create(replace(session, token_hash="b" * 64, credential_hash="wrong"))
    assert await store.get_active("b" * 64, NOW) is None
    async with repository_db.transaction() as connection:
        await connection.execute("UPDATE public.user_groups SET password_hash = 'reset-hash' WHERE slug = 'synthetic'")
    assert await store.get_active(session.token_hash, NOW) is None
    renewed = replace(session, token_hash="c" * 64, credential_hash="reset-hash", credential_revision=1)
    await store.create(renewed)
    await store.revoke(renewed.token_hash, NOW)
    await store.revoke(renewed.token_hash, NOW)
    assert await store.get_active(renewed.token_hash, NOW) is None
    await store.revoke("missing", NOW)
