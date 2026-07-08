"""Token-bucket rate limiter (Spec Part 2 Section 5, Rule 11 — avoid API abuse).

Every collector acquires a token before each outbound request, keeping the
system safely below provider limits. The objective is stable operation,
not maximum request frequency.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class RateLimiter:
    """Classic token bucket: sustained ``rate_per_second`` with bursts up to ``burst``.

    ``time_func`` / ``sleep_func`` are injectable for deterministic tests.
    """

    def __init__(
        self,
        rate_per_second: float,
        burst: int = 1,
        *,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError(f"rate_per_second must be positive, got {rate_per_second}")
        if burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._time = time_func
        self._sleep = sleep_func
        self._updated = time_func()
        self._lock = asyncio.Lock()

    @classmethod
    def per_minute(cls, requests_per_minute: float, burst: int = 5, **kwargs) -> "RateLimiter":
        """Convenience constructor matching how providers document their limits."""
        return cls(requests_per_minute / 60.0, burst, **kwargs)

    async def acquire(self) -> None:
        """Wait until a request token is available, then consume it."""
        while True:
            async with self._lock:
                now = self._time()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            await self._sleep(wait)

    @property
    def available_tokens(self) -> float:
        """Current token count without refreshing the bucket (diagnostic use)."""
        return self._tokens
