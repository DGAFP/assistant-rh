import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from assistant_rh_api.core.errors import DatabaseConflict
from assistant_rh_api.db.auth_stores import DelegationReplayStore

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


async def count(database):
    async with database.transaction(read_only=True) as connection:
        return (await (await connection.execute("SELECT count(*) FROM public.api_delegation_replays")).fetchone())[0]


async def test_concurrent_claims_of_one_assertion_admit_exactly_one(repository_db):
    store = DelegationReplayStore(repository_db)
    results = await asyncio.gather(*(store.claim("a" * 32, "kid", NOW + timedelta(seconds=60), NOW) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    assert await store.claim("b" * 32, "kid", NOW + timedelta(seconds=60), NOW)
    # Still refused at the end of its lifetime, then purged once expired.
    assert not await store.claim("a" * 32, "kid", NOW + timedelta(seconds=60), NOW + timedelta(seconds=59))
    assert await store.claim("c" * 32, "kid", NOW + timedelta(seconds=200), NOW + timedelta(seconds=61))
    assert await count(repository_db) == 1


async def test_invalid_claims_are_rejected_before_or_by_the_database(repository_db):
    store = DelegationReplayStore(repository_db)
    with pytest.raises(ValueError):
        await store.claim("d" * 32, "kid", datetime(2026, 10, 7, 12, 1), NOW)
    with pytest.raises(DatabaseConflict):
        await store.claim("not valid!", "kid", NOW + timedelta(seconds=60), NOW)
    assert await count(repository_db) == 0
