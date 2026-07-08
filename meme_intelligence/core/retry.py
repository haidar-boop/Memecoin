"""Retry with exponential backoff and jitter (Rule 11, Spec Part 21 Section 8).

Only *transient* failures are retried; permanent errors (bad request,
malformed payload) propagate immediately so callers can react instead of
hammering a provider that will never succeed.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

from meme_intelligence.core.errors import TransientCollectorError

T = TypeVar("T")


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.25,
    retry_on: tuple[type[Exception], ...] = (TransientCollectorError,),
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    logger: logging.Logger | None = None,
) -> T:
    """Call ``func`` until it succeeds or ``attempts`` is exhausted.

    Delays double each attempt (capped at ``max_delay``) with +/- ``jitter``
    randomization to avoid synchronized retry storms across tasks.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 1.0 + random.uniform(-jitter, jitter)
            delay = max(0.0, delay)
            if logger is not None:
                logger.warning(
                    "transient failure (attempt %d/%d), retrying in %.2fs: %s",
                    attempt, attempts, delay, exc,
                )
            await sleep_func(delay)

    raise AssertionError("unreachable")  # loop always returns or raises
