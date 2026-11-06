import asyncio
from datetime import datetime, timedelta

import psycopg
import pytest
from assistant_rh_api.core.errors import DatabaseConflict, InvalidCredentials
from assistant_rh_api.db.auth_stores import DelegationReplayStore

from apps.api.tests.auth_fakes import Signer, delegated_service

pytestmark = pytest.mark.anyio


async def database_now(database):
    async with database.transaction(read_only=True) as connection:
        return (await (await connection.execute("SELECT clock_timestamp()")).fetchone())[0].replace(microsecond=0)


async def test_fast_replica_cannot_purge_an_assertion_still_valid_on_a_slow_replica(repository_db):
    now = await database_now(repository_db)
    signer = Signer()
    auth = delegated_service(signer)
    auth.replays = DelegationReplayStore(repository_db)
    issued = int(now.timestamp())
    token = signer.sign(signer.claims(iat=issued, exp=issued + 60))
    fresh = signer.sign(signer.claims(iat=issued, exp=issued + 120))
    await auth.resolve_delegation(token, now)
    # Fast replica is at exp + 1, slow replica at exp - 1. The DB has not expired the token.
    await auth.resolve_delegation(fresh, now + timedelta(seconds=61))
    with pytest.raises(InvalidCredentials):
        await auth.resolve_delegation(token, now + timedelta(seconds=59))


async def test_slow_replica_cannot_reclaim_an_expired_assertion_after_purge(repository_db):
    now = await database_now(repository_db)
    signer = Signer()
    auth = delegated_service(signer)
    auth.replays = DelegationReplayStore(repository_db)
    issued = int(now.timestamp())
    claims = signer.claims(iat=issued - 60, exp=issued - 1)
    async with repository_db.transaction() as connection:
        await connection.execute(
            "INSERT INTO public.api_delegation_replays VALUES (%s, %s, %s)",
            (claims["jti"], signer.kid, now - timedelta(seconds=1)),
        )
    await auth.resolve_delegation(signer.sign(signer.claims(iat=issued, exp=issued + 60)), now)
    assert await count(repository_db) == 1  # The old tombstone was purged.
    with pytest.raises(InvalidCredentials):
        await auth.resolve_delegation(signer.sign(claims), now - timedelta(seconds=2))
    assert await count(repository_db) == 1


async def count(database):
    async with database.transaction(read_only=True) as connection:
        return (await (await connection.execute("SELECT count(*) FROM public.api_delegation_replays")).fetchone())[0]


async def test_claim_expiring_while_waiting_for_a_deleted_tombstone_is_refused(repository_db, repository_dsn):
    store = DelegationReplayStore(repository_db)
    async with repository_db.transaction() as connection:
        expires = (await (await connection.execute("SELECT clock_timestamp() + interval '2 seconds'")).fetchone())[0]
    assert await store.claim("a" * 32, "kid", expires)
    async with asyncio.TaskGroup() as tasks:
        async with repository_db.transaction() as deleting:
            pid = (await (await deleting.execute("SELECT pg_backend_pid()")).fetchone())[0]
            await deleting.execute("DELETE FROM public.api_delegation_replays WHERE jti = %s", ("a" * 32,))
            claimed = tasks.create_task(store.claim("a" * 32, "kid", expires))
            # Ensure INSERT is waiting for the concurrent purge before crossing expiry.
            # Autocommit refreshes pg_stat_activity on every poll; one long transaction
            # can cache the session list before the pool opens the blocked connection.
            async with asyncio.timeout(1), await psycopg.AsyncConnection.connect(repository_dsn, autocommit=True) as observing:
                while True:
                    cursor = await observing.execute("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid)))", (pid,))
                    blocked = await cursor.fetchone()
                    if blocked[0]:
                        break
                    await asyncio.sleep(0.01)
            await deleting.execute("SELECT pg_sleep_until(%s)", (expires + timedelta(milliseconds=10),))
    assert not claimed.result()


async def test_concurrent_claims_of_one_assertion_admit_exactly_one(repository_db):
    store = DelegationReplayStore(repository_db)
    expires = await database_now(repository_db) + timedelta(seconds=60)
    results = await asyncio.gather(*(store.claim("a" * 32, "kid", expires) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    assert await store.claim("b" * 32, "kid", expires)
    assert not await store.claim("a" * 32, "kid", expires)
    # Purge is bounded to 100 expired rows and never removes live assertions.
    async with repository_db.transaction() as connection:
        await connection.execute("""
            INSERT INTO public.api_delegation_replays
            SELECT 'expired-assertion-' || n, 'kid', clock_timestamp() - interval '1 second'
            FROM generate_series(1, 101) n
        """)
    assert await store.claim("c" * 32, "kid", expires)
    assert await count(repository_db) == 4  # Three live assertions and one expired row.


async def test_invalid_claims_are_rejected_before_or_by_the_database(repository_db):
    store = DelegationReplayStore(repository_db)
    with pytest.raises(ValueError):
        await store.claim("d" * 32, "kid", datetime(2026, 10, 7, 12, 1))
    with pytest.raises(DatabaseConflict):
        await store.claim("not valid!", "kid", await database_now(repository_db) + timedelta(seconds=60))
    assert await count(repository_db) == 0
