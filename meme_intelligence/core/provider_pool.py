"""Multi-provider failover pool (Spec Part 2 Section 5, Part 21 Section 5, Part 32.5).

Never rely on one provider: the pool tries providers in priority order,
tracks consecutive failures, and puts unhealthy providers on a cooldown so
the system keeps operating on the remaining sources (Rule 9 — if one
provider becomes unavailable, continue with the rest).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from meme_intelligence.core.errors import AllProvidersFailedError, CollectorError


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    total_failures: int = 0
    total_successes: int = 0


@dataclass
class ProviderHealth:
    """Snapshot of one provider's health for dashboards and logs."""

    name: str
    healthy: bool
    consecutive_failures: int
    total_failures: int
    total_successes: int
    cooldown_remaining: float = field(default=0.0)


class ProviderPool:
    """Ordered pool of interchangeable providers with automatic failover.

    Providers are any objects exposing the same async methods (e.g. several
    market-data clients each implementing ``search_pairs``). A provider that
    fails ``failure_threshold`` times in a row is skipped for
    ``cooldown_seconds``, then given another chance.
    """

    def __init__(
        self,
        providers: Sequence[Any],
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        time_func: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        if not providers:
            raise ValueError("ProviderPool requires at least one provider")
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        self._providers = list(providers)
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._time = time_func
        self._logger = logger or logging.getLogger("meme_intelligence.provider_pool")
        self._states: dict[str, _ProviderState] = {
            self._name_of(p): _ProviderState() for p in self._providers
        }

    @staticmethod
    def _name_of(provider: Any) -> str:
        return getattr(provider, "name", type(provider).__name__)

    async def call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        """Invoke ``method`` on the first healthy provider that succeeds.

        Raises :class:`AllProvidersFailedError` (with per-provider causes)
        when every provider fails or is cooling down — callers should treat
        that as "data unavailable", not as a zero value (Rule 8).
        """
        result, _name = await self.call_with_provider(method, *args, **kwargs)
        return result

    async def call_with_provider(self, method: str, /, *args: Any, **kwargs: Any) -> tuple[Any, str]:
        """Like :meth:`call`, but also returns which provider answered.

        Needed by callers that must later verify the result against a
        genuinely *different* source (Part 15 Section 10) — without
        knowing who actually served ``result``, a caller that guesses
        "not the first provider" can end up asking the same provider that
        already answered to confirm its own data.
        """
        now = self._time()
        causes: dict[str, Exception] = {}

        for provider in self._providers:
            name = self._name_of(provider)
            state = self._states[name]
            if state.cooldown_until > now:
                continue

            try:
                result = await getattr(provider, method)(*args, **kwargs)
            except CollectorError as exc:
                causes[name] = exc
                self._record_failure(name, state, method, exc)
                continue

            if state.consecutive_failures:
                self._logger.info("provider '%s' recovered", name)
            state.consecutive_failures = 0
            state.total_successes += 1
            return result, name

        raise AllProvidersFailedError(method, causes)

    def _record_failure(self, name: str, state: _ProviderState, method: str, exc: Exception) -> None:
        state.consecutive_failures += 1
        state.total_failures += 1
        self._logger.warning("provider '%s' failed on '%s': %s", name, method, exc)
        if state.consecutive_failures >= self._failure_threshold:
            state.cooldown_until = self._time() + self._cooldown_seconds
            self._logger.warning(
                "provider '%s' reached %d consecutive failures; cooling down for %.0fs",
                name, state.consecutive_failures, self._cooldown_seconds,
            )

    def health(self) -> list[ProviderHealth]:
        """Health snapshot for every provider (for the dashboard and logs)."""
        now = self._time()
        return [
            ProviderHealth(
                name=name,
                healthy=state.cooldown_until <= now,
                consecutive_failures=state.consecutive_failures,
                total_failures=state.total_failures,
                total_successes=state.total_successes,
                cooldown_remaining=max(0.0, state.cooldown_until - now),
            )
            for name, state in self._states.items()
        ]
