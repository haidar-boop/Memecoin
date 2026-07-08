"""Tests for retry with exponential backoff (Rules 7/11)."""

import pytest

from meme_intelligence.core.errors import CollectorError, TransientCollectorError
from meme_intelligence.core.retry import retry_async


async def _no_sleep(_seconds: float) -> None:
    pass


async def test_succeeds_after_transient_failures():
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientCollectorError("blip")
        return "ok"

    result = await retry_async(flaky, attempts=4, sleep_func=_no_sleep)
    assert result == "ok"
    assert attempts == 3


async def test_gives_up_after_max_attempts():
    attempts = 0

    async def always_fails():
        nonlocal attempts
        attempts += 1
        raise TransientCollectorError("down")

    with pytest.raises(TransientCollectorError):
        await retry_async(always_fails, attempts=3, sleep_func=_no_sleep)
    assert attempts == 3


async def test_permanent_errors_are_not_retried():
    attempts = 0

    async def bad_request():
        nonlocal attempts
        attempts += 1
        raise CollectorError("bad request")  # not a TransientCollectorError

    with pytest.raises(CollectorError):
        await retry_async(bad_request, attempts=4, sleep_func=_no_sleep)
    assert attempts == 1


async def test_backoff_delays_grow_exponentially():
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    async def always_fails():
        raise TransientCollectorError("down")

    with pytest.raises(TransientCollectorError):
        await retry_async(
            always_fails, attempts=4, base_delay=1.0, max_delay=100.0,
            jitter=0.0, sleep_func=record_sleep,
        )
    assert delays == [1.0, 2.0, 4.0]
