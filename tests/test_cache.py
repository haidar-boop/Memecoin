"""Tests for the TTL cache (anti-throttling foundation, Rules 10/11)."""

import pytest

from meme_intelligence.core.cache import TTLCache


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


async def test_set_and_get(clock):
    cache = TTLCache(time_func=clock)
    await cache.set("k", {"a": 1})
    assert await cache.get("k") == {"a": 1}


async def test_expiry(clock):
    cache = TTLCache(default_ttl=10.0, time_func=clock)
    await cache.set("k", "v")
    clock.advance(9.9)
    assert await cache.get("k") == "v"
    clock.advance(0.2)
    assert await cache.get("k") is None


async def test_per_entry_ttl_overrides_default(clock):
    cache = TTLCache(default_ttl=100.0, time_func=clock)
    await cache.set("short", "v", ttl=5.0)
    clock.advance(6.0)
    assert await cache.get("short") is None


async def test_lru_eviction(clock):
    cache = TTLCache(max_entries=2, default_ttl=100.0, time_func=clock)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.get("a")       # touch "a" so "b" is least recently used
    await cache.set("c", 3)    # evicts "b"
    assert await cache.get("a") == 1
    assert await cache.get("b") is None
    assert await cache.get("c") == 3


async def test_get_or_set_only_computes_on_miss(clock):
    cache = TTLCache(time_func=clock)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return "value"

    assert await cache.get_or_set("k", factory) == "value"
    assert await cache.get_or_set("k", factory) == "value"
    assert calls == 1


async def test_invalid_parameters_rejected(clock):
    with pytest.raises(ValueError):
        TTLCache(max_entries=0, time_func=clock)
    cache = TTLCache(time_func=clock)
    with pytest.raises(ValueError):
        await cache.set("k", "v", ttl=0)
