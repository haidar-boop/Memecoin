"""Tests for multi-provider failover (Rule 9 — never rely on one source)."""

import pytest

from meme_intelligence.core.errors import AllProvidersFailedError, TransientCollectorError
from meme_intelligence.core.provider_pool import ProviderPool


class FakeProvider:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls = 0

    async def fetch(self, value: str) -> str:
        self.calls += 1
        if self.fail:
            raise TransientCollectorError(f"{self.name} is down")
        return f"{self.name}:{value}"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_primary_used_when_healthy():
    primary, backup = FakeProvider("primary"), FakeProvider("backup")
    pool = ProviderPool([primary, backup])
    assert await pool.call("fetch", "x") == "primary:x"
    assert backup.calls == 0


async def test_failover_to_backup():
    primary, backup = FakeProvider("primary", fail=True), FakeProvider("backup")
    pool = ProviderPool([primary, backup])
    assert await pool.call("fetch", "x") == "backup:x"
    assert primary.calls == 1


async def test_cooldown_skips_unhealthy_provider():
    clock = FakeClock()
    primary, backup = FakeProvider("primary", fail=True), FakeProvider("backup")
    pool = ProviderPool([primary, backup], failure_threshold=2, cooldown_seconds=60, time_func=clock)

    await pool.call("fetch", "a")  # failure 1 -> failover
    await pool.call("fetch", "b")  # failure 2 -> cooldown starts
    assert primary.calls == 2

    await pool.call("fetch", "c")  # primary skipped during cooldown
    assert primary.calls == 2

    clock.now = 61.0               # cooldown elapsed; primary gets another chance
    await pool.call("fetch", "d")
    assert primary.calls == 3


async def test_recovery_resets_failure_count():
    clock = FakeClock()
    primary, backup = FakeProvider("primary", fail=True), FakeProvider("backup")
    pool = ProviderPool([primary, backup], failure_threshold=3, time_func=clock)

    await pool.call("fetch", "a")
    primary.fail = False
    await pool.call("fetch", "b")  # primary succeeds, counter resets

    health = {h.name: h for h in pool.health()}
    assert health["primary"].consecutive_failures == 0
    assert health["primary"].total_failures == 1
    assert health["primary"].total_successes == 1


async def test_all_failing_raises_with_causes():
    pool = ProviderPool([FakeProvider("a", fail=True), FakeProvider("b", fail=True)])
    with pytest.raises(AllProvidersFailedError) as excinfo:
        await pool.call("fetch", "x")
    assert set(excinfo.value.causes) == {"a", "b"}


def test_empty_pool_rejected():
    with pytest.raises(ValueError):
        ProviderPool([])
