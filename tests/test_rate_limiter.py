"""Tests for the token-bucket rate limiter (Rule 11 — avoid API abuse)."""

import pytest

from meme_intelligence.core.rate_limiter import RateLimiter


class FakeAsyncClock:
    """Deterministic clock where sleeping advances time instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_burst_is_immediate():
    clock = FakeAsyncClock()
    limiter = RateLimiter(1.0, burst=3, time_func=clock.time, sleep_func=clock.sleep)
    for _ in range(3):
        await limiter.acquire()
    assert clock.sleeps == []  # burst capacity used, no waiting


async def test_waits_when_bucket_empty():
    clock = FakeAsyncClock()
    limiter = RateLimiter(2.0, burst=1, time_func=clock.time, sleep_func=clock.sleep)
    await limiter.acquire()          # consumes the only token
    await limiter.acquire()          # must wait ~0.5s at 2 req/s
    assert len(clock.sleeps) >= 1
    assert sum(clock.sleeps) == pytest.approx(0.5, abs=0.01)


async def test_tokens_refill_over_time():
    clock = FakeAsyncClock()
    limiter = RateLimiter(1.0, burst=2, time_func=clock.time, sleep_func=clock.sleep)
    await limiter.acquire()
    await limiter.acquire()
    clock.now += 2.0                 # bucket refills while idle
    await limiter.acquire()
    await limiter.acquire()
    assert clock.sleeps == []


def test_per_minute_constructor():
    limiter = RateLimiter.per_minute(300.0)
    assert limiter._rate == pytest.approx(5.0)  # 300/min == 5/s


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        RateLimiter(0)
    with pytest.raises(ValueError):
        RateLimiter(1.0, burst=0)
