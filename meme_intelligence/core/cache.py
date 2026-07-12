"""Async-safe TTL cache (Spec Part 21 Rule 3, Part 32.5 — anti-throttling).

Caching recently collected data is the first line of defense against API
abuse: identical information is never requested twice within its TTL
(Rules 10 and 11).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

_MISSING = object()


class TTLCache:
    """In-memory cache with per-entry TTL and LRU eviction.

    ``time_func`` is injectable for deterministic tests. A Redis-backed
    implementation can replace this behind the same interface once
    multi-process scanning is introduced (Part 21, Section 9).
    """

    def __init__(
        self,
        max_entries: int = 2048,
        default_ttl: float = 30.0,
        *,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        if default_ttl <= 0:
            raise ValueError(f"default_ttl must be positive, got {default_ttl}")
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._time = time_func
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()
        # Single-flight guard: concurrent misses on the same key share one
        # per-key lock so ``factory`` runs once (Rules 10/11 — never request
        # identical information twice within its TTL). Each entry is removed by
        # the call that installed it once the value is stored, so this dict
        # stays bounded rather than accumulating a lock per key ever seen.
        self._inflight_locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value, or ``default`` if absent or expired."""
        async with self._lock:
            entry = self._entries.get(key, _MISSING)
            if entry is _MISSING:
                return default
            expires_at, value = entry
            if self._time() >= expires_at:
                del self._entries[key]
                return default
            self._entries.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store a value, evicting the least-recently-used entry when full."""
        ttl = self._default_ttl if ttl is None else ttl
        if ttl <= 0:
            raise ValueError(f"ttl must be positive, got {ttl}")
        async with self._lock:
            self._entries[key] = (self._time() + ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Any:
        """Return the cached value, computing and storing it via ``factory`` on a miss.

        Concurrent callers that miss on the same key are collapsed onto a single
        ``factory`` invocation via a per-key in-flight lock; callers that arrive
        while the value is being computed re-check the cache after acquiring the
        lock and reuse the freshly stored value instead of calling ``factory``.
        """
        cached = await self.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        lock = self._inflight_locks.get(key)
        if lock is None:
            lock = self._inflight_locks[key] = asyncio.Lock()
        try:
            async with lock:
                # Re-check under the per-key lock: an earlier caller may have
                # populated the cache while we waited to acquire it.
                cached = await self.get(key, _MISSING)
                if cached is not _MISSING:
                    return cached
                value = await factory()
                await self.set(key, value, ttl)
                return value
        finally:
            # The caller that installed this lock removes it once done; followers
            # that reused it find the key already gone (or remapped to a newer
            # lock) and skip, so the guard dict never accumulates stale keys.
            if self._inflight_locks.get(key) is lock:
                del self._inflight_locks[key]

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
