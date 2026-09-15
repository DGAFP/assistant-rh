import asyncio

import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.models.configuration import Snapshot
from assistant_rh_api.db.cache import RevisionCache
from assistant_rh_api.db.revisions import content_revision, freeze_json


def test_revisions_are_deterministic_and_snapshots_are_detached() -> None:
    raw = {"b": ["é", {"flag": True}], "a": 1}
    frozen = freeze_json(raw)
    revision = content_revision(frozen)
    assert revision == content_revision(freeze_json({"a": 1, "b": ["é", {"flag": True}]}))
    raw["b"].append("changed")
    assert revision != content_revision(freeze_json(raw))
    assert len(frozen["b"]) == 2
    with pytest.raises(TypeError):
        frozen["b"][1]["flag"] = False
    assert content_revision(freeze_json([1, 2])) != content_revision(freeze_json([2, 1]))
    with pytest.raises(ValueError):
        freeze_json(float("nan"))


@pytest.mark.anyio
async def test_cache_ttl_invalidation_scope_capacity_and_no_stale_fallback() -> None:
    now = 0.0
    cache = RevisionCache(ttl_seconds=15, max_entries=2, monotonic=lambda: now)
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        value = freeze_json({"version": calls})
        return Snapshot(value, content_revision(value), "database")

    first = await cache.get(("ministry-a", "config"), load)
    assert await cache.get(("ministry-a", "config"), load) is first
    assert calls == 1
    assert await cache.get(("ministry-b", "config"), load) is not first
    now = 15.0
    second = await cache.get(("ministry-a", "config"), load)
    assert first.revision != second.revision
    await cache.invalidate(("ministry-a", "config"))
    assert await cache.get(("ministry-a", "config"), load) is not second
    await cache.clear()
    await cache.get("a", load)
    await cache.get("b", load)
    await cache.get("c", load)
    before = calls
    await cache.get("a", load)
    assert calls == before + 1

    async def unavailable():
        raise DatabaseUnavailable()

    now += 15
    with pytest.raises(DatabaseUnavailable):
        await cache.get("a", unavailable)


@pytest.mark.anyio
async def test_concurrent_loads_share_a_snapshot_and_invalidation_wins() -> None:
    cache = RevisionCache(ttl_seconds=15, max_entries=2, monotonic=lambda: 0.0)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return calls

    first = asyncio.create_task(cache.get("a", load))
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(cache.get("a", load))
    invalidate = asyncio.create_task(cache.invalidate("a"))
    release.set()
    assert await first == await second == 1
    await invalidate
    assert await cache.get("a", load) == 2


@pytest.mark.anyio
async def test_cache_cancelled_load_does_not_poison_key() -> None:
    cache = RevisionCache(ttl_seconds=15, max_entries=2, monotonic=lambda: 0.0)
    started = asyncio.Event()

    async def load():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cache.get("a", load))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def recovered():
        return "recovered"

    assert await cache.get("a", recovered) == "recovered"
