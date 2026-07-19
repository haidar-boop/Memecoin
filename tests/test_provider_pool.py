"""Tests for multi-provider failover (Rule 9 — never rely on one source)."""

import asyncio

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


class RaisingProvider:
    """A provider whose implementation raises something other than a
    CollectorError — simulating a bug in the provider, not routine
    provider trouble (e.g. an AttributeError, TypeError, or a bare
    ValueError from unvalidated data)."""

    def __init__(self, name: str, exc: BaseException):
        self.name = name
        self.exc = exc
        self.calls = 0

    async def fetch(self, value: str) -> str:
        self.calls += 1
        raise self.exc


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


async def test_all_on_cooldown_raises_with_informative_causes():
    """When every provider is cooling down, causes must still be populated
    (bug-hunt fix) — the for-loop body never runs for a cooling-down
    provider, so without an explicit fix causes stays {} and the error
    message degrades to the uninformative 'no providers available'."""
    clock = FakeClock()
    primary, backup = FakeProvider("primary", fail=True), FakeProvider("backup", fail=True)
    pool = ProviderPool([primary, backup], failure_threshold=1, cooldown_seconds=60, time_func=clock)

    with pytest.raises(AllProvidersFailedError):
        await pool.call("fetch", "a")  # both fail once -> both on cooldown

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await pool.call("fetch", "b")  # every provider skipped for cooldown this time

    assert set(excinfo.value.causes) == {"primary", "backup"}
    message = str(excinfo.value)
    assert "no providers available" not in message
    assert "cooldown" in message.lower()
    # The second call must not have actually invoked the cooling-down providers.
    assert primary.calls == 1
    assert backup.calls == 1


async def test_unexpected_non_collector_exception_does_not_stop_failover():
    """A provider bug (AttributeError/TypeError/bare ValueError, none of
    which derive from CollectorError) must not kill the whole pool — the
    next provider in priority order is still tried and can still succeed."""
    primary = RaisingProvider("primary", AttributeError("boom, this is a provider bug"))
    backup = FakeProvider("backup")
    pool = ProviderPool([primary, backup])

    result, name = await pool.call_with_provider("fetch", "x")

    assert result == "backup:x"
    assert name == "backup"
    assert primary.calls == 1


async def test_unexpected_exception_still_counts_toward_cooldown():
    """A provider that keeps raising unexpected exceptions should still
    eventually cool down, exactly like TransientCollectorError does,
    rather than being retried forever."""
    clock = FakeClock()
    primary = RaisingProvider("primary", TypeError("provider bug"))
    backup = FakeProvider("backup")
    pool = ProviderPool([primary, backup], failure_threshold=2, cooldown_seconds=60, time_func=clock)

    await pool.call("fetch", "a")
    await pool.call("fetch", "b")
    assert primary.calls == 2  # reached failure_threshold -> cooldown starts

    await pool.call("fetch", "c")
    assert primary.calls == 2  # skipped this time, on cooldown


async def test_cancelled_error_propagates_and_is_never_swallowed():
    """asyncio.CancelledError derives from BaseException, not Exception, so
    the new `except Exception` clause must never catch it — a
    graceful-shutdown cancellation must never be absorbed as a mere
    'provider failure'."""
    primary = RaisingProvider("primary", asyncio.CancelledError())
    backup = FakeProvider("backup")
    pool = ProviderPool([primary, backup])

    with pytest.raises(asyncio.CancelledError):
        await pool.call("fetch", "x")

    # The pool must not have failed over past the cancellation to try backup.
    assert backup.calls == 0
