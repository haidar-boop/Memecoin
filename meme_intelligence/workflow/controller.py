"""Continuous scanning controller (Spec Part 13, Part 21 Section 4, Part 22 Section 2).

Runs the discovery -> analysis -> rules -> alerts cycle on the fast-layer
cadence, indefinitely or for a bounded number of cycles.

Reliability rules (Rule 7, Part 32.5 Section 10):

* One failing cycle never kills the scanner: errors are logged and the
  next cycle starts after an exponential backoff that resets on success.
* Graceful shutdown: SIGINT/SIGTERM (where the platform supports them)
  set a stop flag; the current cycle finishes and state is flushed.
* Every assessment is snapshotted; alert decisions are journaled.

The controller optimizes for quality, not volume (Part 13 final rule):
tokens already seen this session are skipped, most candidates are
filtered before deep analysis, and alerts fire only through the
automation gates.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from meme_intelligence.alerts.notification_engine import (
    AlertEvent,
    AutomationRules,
    NotificationEngine,
)
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import Classification, MarketRegime, WatchlistTier
from meme_intelligence.core.errors import MemeIntelError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.database.storage import Storage
from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools
from meme_intelligence.workflow.pipeline import ResearchPipeline

_TIER_FOR_CLASSIFICATION = {
    Classification.ELITE_OPPORTUNITY: WatchlistTier.TIER_1_HIGH_PRIORITY,
    Classification.STRONG_CANDIDATE: WatchlistTier.TIER_1_HIGH_PRIORITY,
    Classification.WATCHLIST: WatchlistTier.TIER_2_DEVELOPING,
    Classification.SPECULATIVE: WatchlistTier.TIER_3_RESEARCH_ONLY,
}

_ERROR_BACKOFF_START = 5.0
_ERROR_BACKOFF_MAX = 300.0


@dataclass
class CycleStats:
    """What one scan cycle did (logged and aggregated)."""

    cycle: int
    pools_seen: int = 0
    candidates: int = 0
    analyzed: int = 0
    alerts: list[AlertEvent] = field(default_factory=list)


class ContinuousScanner:
    """24/7 scanning loop over the shared research pipeline (Part 13)."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        notifier: NotificationEngine,
        *,
        gecko_client,   # get_new_pools(network)
        goplus_client,  # get_token_security(chain, address)
        regime: MarketRegime = MarketRegime.UNKNOWN,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._notifier = notifier
        self._gecko = gecko_client
        self._regime = regime
        self._now = now_func
        self._sleep = sleep_func
        self._logger = get_logger("workflow.controller")

        self._discovery = DiscoveryEngine(settings.discovery, now_func=now_func)
        self._pipeline = ResearchPipeline(settings, goplus_client, now_func=now_func)
        self._rules = AutomationRules(settings.alerts, settings.alert_engine)
        self._seen: set[tuple[str, str]] = set()
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Ask the scanner to stop after the current cycle (graceful shutdown)."""
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.request_stop)
        except (NotImplementedError, RuntimeError):
            # Platform without signal support (e.g. some test loops); Ctrl-C
            # still raises KeyboardInterrupt in run().
            pass

    async def run(self, max_cycles: int | None = None) -> list[CycleStats]:
        """Run scan cycles until stopped or ``max_cycles`` is reached."""
        self._install_signal_handlers()
        self._logger.info(
            "continuous scanner started: networks=%s interval=%.0fs cycles=%s",
            self._settings.workflow.network_list,
            self._settings.workflow.monitor_interval_seconds,
            max_cycles if max_cycles is not None else "unbounded",
        )

        history: list[CycleStats] = []
        backoff = _ERROR_BACKOFF_START
        cycle = 0
        while not self._stop.is_set() and (max_cycles is None or cycle < max_cycles):
            cycle += 1
            try:
                stats = await self._run_cycle(cycle)
                history.append(stats)
                backoff = _ERROR_BACKOFF_START  # healthy cycle resets the backoff
                self._logger.info(
                    "cycle %d: %d pools, %d candidates, %d analyzed, %d alerts",
                    cycle, stats.pools_seen, stats.candidates, stats.analyzed,
                    len(stats.alerts),
                )
            except MemeIntelError as exc:
                self._logger.error("cycle %d failed: %s (backing off %.0fs)", cycle, exc, backoff)
                await self._sleep(backoff)
                backoff = min(_ERROR_BACKOFF_MAX, backoff * 2)
                continue

            if self._stop.is_set() or (max_cycles is not None and cycle >= max_cycles):
                break
            await self._sleep(self._settings.workflow.monitor_interval_seconds)

        self._logger.info("continuous scanner stopped after %d cycle(s)", cycle)
        return history

    async def _run_cycle(self, cycle: int) -> CycleStats:
        stats = CycleStats(cycle=cycle)

        candidates, rejected = await scan_new_pools(
            self._gecko, self._discovery, self._settings.workflow.network_list,
        )
        stats.pools_seen = len(candidates) + len(rejected)
        stats.candidates = len(candidates)

        for candidate in candidates[: self._settings.workflow.top_candidates]:
            token = candidate.pair.base_token
            key = (token.chain, token.address.lower())
            if key in self._seen:
                continue
            self._seen.add(key)

            previous = self._storage.score_history(token, limit=1)
            previous_score = previous[0]["final_score"] if previous else None

            result = await self._pipeline.analyze_pair(candidate.pair, regime=self._regime)
            if result is None:
                continue
            stats.analyzed += 1
            self._storage.record_snapshot(result.master, source="continuous_scanner")

            tier = _TIER_FOR_CLASSIFICATION.get(result.master.classification)
            if tier is not None:
                self._storage.update_watchlist(
                    token, tier,
                    score=result.master.final_score,
                    classification=result.master.classification,
                    thesis=f"continuous scan cycle {cycle} "
                           f"(discovery {candidate.discovery_score:.0f})",
                )

            events = self._rules.evaluate(result, previous_score=previous_score)
            delivered = await self._notifier.dispatch(events)
            stats.alerts.extend(delivered)
            for event in delivered:
                self._storage.add_journal(
                    token, "alert", f"{event.priority.value}/{event.alert_type}: {event.title}",
                )

        return stats
