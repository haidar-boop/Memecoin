"""Exception hierarchy for the meme coin intelligence system.

Collectors distinguish transient failures (worth retrying / failing over)
from permanent ones so the retry and provider-pool layers can react
appropriately (Spec Part 2 / Part 21 / Part 32.5 — anti-throttling and
failure handling).
"""

from __future__ import annotations


class MemeIntelError(Exception):
    """Base class for all errors raised by this project."""


class ConfigurationError(MemeIntelError):
    """Invalid or inconsistent configuration (bad env value, weights not summing, ...)."""


class CollectorError(MemeIntelError):
    """A data collector failed in a way that is NOT worth retrying (4xx, bad payload)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransientCollectorError(CollectorError):
    """A data collector failed in a way that IS worth retrying (network, timeout, 5xx)."""


class RateLimitedError(TransientCollectorError):
    """The provider throttled us (HTTP 429). Retry with backoff or fail over.

    ``retry_after_seconds`` carries the provider's Retry-After hint when one
    was sent; the retry loop sleeps at least that long instead of hammering a
    provider that just said "back off" (Rule 11 — bug-hunt finding: 4 hits in
    ~4s inside the throttle window).
    """

    def __init__(self, message: str, status_code: int | None = 429,
                 retry_after_seconds: float | None = None) -> None:
        super().__init__(message, status_code)
        self.retry_after_seconds = retry_after_seconds


class AllProvidersFailedError(MemeIntelError):
    """Every provider in a pool failed or is cooling down.

    Carries the per-provider causes so callers can log and reduce
    confidence instead of fabricating data (Rule 8 — data before assumptions).
    """

    def __init__(self, method: str, causes: dict[str, Exception]):
        self.method = method
        self.causes = causes
        summary = "; ".join(f"{name}: {exc}" for name, exc in causes.items()) or "no providers available"
        super().__init__(f"all providers failed for '{method}': {summary}")


class InsufficientDataError(MemeIntelError):
    """Not enough data exists to compute a meaningful result.

    Raised instead of returning a fabricated value (Rule 8).
    """
