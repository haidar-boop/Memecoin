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
import dataclasses
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from meme_intelligence.alerts.notification_engine import (
    AlertEvent,
    AutomationRules,
    NotificationEngine,
    events_from_security_changes,
)
from meme_intelligence.analyzers.security_monitor import (
    detect_security_changes,
    extract_facts,
    merge_facts,
)
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import AlertPriority, MarketRegime, WatchlistTier
from meme_intelligence.core.errors import AllProvidersFailedError, CollectorError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.database.storage import Storage
from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools
from meme_intelligence.workflow.pipeline import PipelineResult, ResearchPipeline
from meme_intelligence.workflow.watchlist_review import (
    TIER_FOR_CLASSIFICATION as _TIER_FOR_CLASSIFICATION,
)

# Alert types whose evidence rests on market data and therefore get
# multi-source verification before dispatch (Part 15, Section 10).
_VERIFIABLE_ALERT_TYPES = {"high_priority_opportunity", "early_opportunity", "momentum"}

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
        market_service=None,  # MarketDataService: watchlist recheck + verification
        regime: MarketRegime = MarketRegime.UNKNOWN,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._notifier = notifier
        self._gecko = gecko_client
        self._market = market_service
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
            except Exception as exc:  # noqa: BLE001
                # Contract (module docstring / Rule 7): one failing cycle must
                # NEVER kill the scanner — not a domain error, not an
                # unexpected one (sqlite lock, sink failure, a stray
                # ValueError). BaseException (KeyboardInterrupt/CancelledError)
                # still propagates so graceful shutdown works.
                self._logger.error("cycle %d failed: %s (backing off %.0fs)",
                                   cycle, exc, backoff, exc_info=True)
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

        processed_this_cycle: set[str] = set()
        # Filter already-seen tokens BEFORE taking the top-N slice, so seen
        # tokens don't consume analysis slots and starve fresh candidates
        # ranked just below them.
        fresh = [
            c for c in candidates
            if (c.pair.base_token.chain, c.pair.base_token.address.lower()) not in self._seen
        ]
        for candidate in fresh[: self._settings.workflow.top_candidates]:
            token = candidate.pair.base_token
            key = (token.chain, token.address.lower())

            result = await self._pipeline.analyze_pair(candidate.pair, regime=self._regime)
            if result is None:
                # Transient miss (e.g. security data not indexed yet for a
                # brand-new token): do NOT mark it seen, so a later cycle gives
                # it another look instead of blacklisting it for the session.
                continue
            self._seen.add(key)
            stats.analyzed += 1
            processed_this_cycle.add(token.address.lower())
            await self._process_result(
                result, stats, source="continuous_scanner",
                thesis=f"continuous scan cycle {cycle} "
                       f"(discovery {candidate.discovery_score:.0f})",
            )

        # Secondary cadence (Part 15 Section 2): tracked tokens are
        # re-checked every N cycles, not every cycle.
        if (
            self._market is not None
            and cycle % self._settings.workflow.watchlist_recheck_cycles == 0
        ):
            await self._recheck_watchlist(stats, skip=processed_this_cycle)

        return stats

    async def _process_result(
        self, result: PipelineResult, stats: CycleStats, *, source: str, thesis: str | None,
    ) -> None:
        """Persist, tier, apply rules, verify important alerts, dispatch."""
        token = result.pair.base_token
        previous = self._storage.score_history(token, limit=1)
        previous_score = previous[0]["final_score"] if previous else None
        self._storage.record_snapshot(result.master, source=source)

        # Contract-change monitoring (Part 18, Section 10): diff the security
        # facts against the last known baseline, then update the baseline.
        previous_facts = self._storage.latest_security_facts(token)
        changes = detect_security_changes(previous_facts, result.security_profile)
        current_facts = extract_facts(result.security_profile)
        self._storage.record_security_facts(token, merge_facts(previous_facts, current_facts))

        tier = _TIER_FOR_CLASSIFICATION.get(result.master.classification)
        if tier is not None:
            self._storage.update_watchlist(
                token, tier,
                score=result.master.final_score,
                classification=result.master.classification,
                thesis=thesis,
            )
        elif previous_score is not None:
            # Was tracked (or at least scored) before and now fails: archive.
            existing = {e.token.address.lower() for e in self._storage.get_watchlist()}
            if token.address.lower() in existing:
                self._storage.archive(
                    token, f"re-assessment fell to Avoid (score {result.master.final_score:.0f})",
                )

        events = self._rules.evaluate(result, previous_score=previous_score)
        events = await self._verify_events(events, result)
        # Security-change events rest on contract facts, not market data, so
        # they bypass market cross-verification and are appended directly.
        events.extend(events_from_security_changes(token, changes))
        delivered = await self._notifier.dispatch(events)
        stats.alerts.extend(delivered)
        for event in delivered:
            self._storage.add_journal(
                token, "alert", f"{event.priority.value}/{event.alert_type}: {event.title}",
            )

    async def _recheck_watchlist(self, stats: CycleStats, *, skip: set[str] = frozenset()) -> None:
        """Re-analyze tracked tokens on the slower cadence (Part 15, Section 2)."""
        entries = self._storage.get_watchlist()
        limit = self._settings.workflow.watchlist_review_limit
        rechecked = 0
        for entry in entries:
            if rechecked >= limit:
                break
            if entry.token.address.lower() in skip:
                continue  # analyzed moments ago this cycle; nothing new to learn
            if entry.tier is WatchlistTier.TIER_3_RESEARCH_ONLY:
                continue  # research-only entries wait for the daily routine
            try:
                pairs = await self._market.get_token_pairs(
                    entry.token.address, chain=entry.token.chain,
                )
            except (CollectorError, AllProvidersFailedError) as exc:
                # A transient provider outage is NOT a dead market: skip and
                # retry next recheck instead of archiving a healthy token.
                self._logger.info("recheck skipped for %s: market data unavailable (%s)",
                                  entry.token.address, exc)
                continue
            if not pairs:
                self._storage.archive(entry.token, "no active trading pairs remain")
                continue
            pair = max(pairs, key=lambda p: p.liquidity_usd or 0.0)
            result = await self._pipeline.analyze_pair(pair, regime=self._regime)
            if result is None:
                continue
            rechecked += 1
            stats.analyzed += 1
            await self._process_result(result, stats, source="watchlist_recheck", thesis=None)
        if rechecked:
            self._logger.info("watchlist recheck: %d tracked token(s) re-analyzed", rechecked)

    async def _verify_events(
        self, events: list[AlertEvent], result: PipelineResult,
    ) -> list[AlertEvent]:
        """Confirm market-data-based alerts against a second source (Part 15, Section 10).

        Disagreement downgrades the alert and says why; an unavailable
        second source annotates the alert as unverified — it never
        silently passes as confirmed (Rule 8).
        """
        if self._market is None:
            return events
        needs_verification = [e for e in events if e.alert_type in _VERIFIABLE_ALERT_TYPES]
        if not needs_verification:
            return events

        verdict, note = await self._market.cross_check_liquidity(result.pair)
        verified: list[AlertEvent] = []
        for event in events:
            if event.alert_type not in _VERIFIABLE_ALERT_TYPES:
                verified.append(event)
            elif verdict is True:
                verified.append(dataclasses.replace(event, reasons=event.reasons + (note,)))
            elif verdict is False:
                verified.append(dataclasses.replace(
                    event,
                    priority=AlertPriority.MEDIUM if event.priority is AlertPriority.HIGH
                    else AlertPriority.LOW,
                    reasons=event.reasons + (f"DOWNGRADED: {note}",),
                ))
                self._logger.warning("alert downgraded, sources disagree: %s (%s)",
                                     event.alert_type, note)
            else:
                verified.append(dataclasses.replace(
                    event, reasons=event.reasons + (f"unverified: {note}",),
                ))
        return verified
