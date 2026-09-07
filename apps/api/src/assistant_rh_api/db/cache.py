"""Bounded TTL cache, scoped to one adapter instance and one async event loop."""

from asyncio import Lock
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from math import isfinite


class RevisionCache[K: Hashable, V]:
    """Cache immutable snapshots, including their content revision.

    All loads/invalidation are serialized, so invalidation cannot be undone by
    an in-flight load. Keys must include tenant/scope/name where applicable.
    A cache belongs to one DB target. Loader errors never return stale data.
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int, monotonic: Callable[[], float]) -> None:
        if not isfinite(ttl_seconds) or ttl_seconds < 0 or max_entries < 1:
            raise ValueError("Cache requires a finite non-negative TTL and a positive capacity")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = Lock()

    async def get(self, key: K, load: Callable[[], Awaitable[V]]) -> V:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._monotonic() < entry[0]:
                self._entries.move_to_end(key)
                return entry[1]
            self._entries.pop(key, None)
            value = await load()
            self._entries[key] = (self._monotonic() + self._ttl, value)
            if len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return value

    async def invalidate(self, key: K) -> None:
        """Call after a successful write commit, never before it."""
        async with self._lock:
            self._entries.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
