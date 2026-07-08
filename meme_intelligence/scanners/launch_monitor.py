"""Pump.fun launch filtering and promotion (Spec Part 32.5 Sections 3, 5, 7, 8).

The launch monitor sits between the raw launch stream and the analysis
pipeline, implementing the Section 7 front of the candidate funnel::

    New Token Detected  ->  Basic Filtering  ->  Early Activity Review

"The system must not alert on every new launch. Most launches should be
filtered out." (Section 3.) Basic filtering discards launches that fail
structural checks (unaccepted launchpad, anonymous token, insider-sized
dev buy); survivors are *tracked*, rechecked against the Pump.fun
frontend API on a bounded budget (Section 5 — request prioritization and
refresh control, Rule 11), and promoted only after meeting the Section 8
deep-analysis threshold: evidence of real buying, community interest,
increasing attention, and live trading.

Promotion still is not analysis: the scanner takes promoted candidates
through independent market confirmation before the pipeline runs
(Section 2 — discovery is never confirmation). The monitor keeps a
promoted candidate in ``READY`` state until the scanner confirms it, so
a token the market providers have not indexed yet is retried rather
than lost (Rule 7).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from meme_intelligence.config.settings import PumpFunSettings
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import PumpFunCoinState, PumpFunLaunch, TokenIdentity

# Consecutive frontend-API 404s tolerated before a tracked launch is
# dropped as unindexed/delisted. Fresh mints can lag the API briefly, so
# one miss is normal; repeated misses mean the coin is gone or hidden.
_MAX_FRONTEND_MISSES = 3


class LaunchStatus(enum.Enum):
    PENDING = "pending"  # tracked, awaiting Section 8 traction evidence
    READY = "ready"      # passed promotion gates, awaiting market confirmation


@dataclass(frozen=True)
class LaunchRejection:
    """One launch discarded by basic filtering, with the reason (Rule 13)."""

    launch: PumpFunLaunch
    reason: str


@dataclass(frozen=True)
class LaunchCandidate:
    """A tracked launch that passed the Section 8 deep-analysis threshold."""

    launch: PumpFunLaunch
    state: PumpFunCoinState | None   # latest traction snapshot (None on migration fast-path)
    reasons: tuple[str, ...]         # evidence supporting the promotion
    promoted_at: datetime


@dataclass
class _TrackedLaunch:
    """Internal mutable tracking record for one launch."""

    launch: PumpFunLaunch
    first_seen: datetime
    next_check_at: datetime
    status: LaunchStatus = LaunchStatus.PENDING
    last_state: PumpFunCoinState | None = None
    graduated: bool = False          # migration event seen (fast-path promotion)
    frontend_misses: int = 0
    promotion_reasons: tuple[str, ...] = field(default_factory=tuple)


class LaunchMonitor:
    """Tracks launch-stream tokens from basic filtering to promotion."""

    def __init__(
        self,
        settings: PumpFunSettings,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._s = settings
        self._now = now_func
        self._tracked: dict[str, _TrackedLaunch] = {}   # key: lowercased mint
        self._logger = get_logger("scanners.launch_monitor")

    # ---- Section 7: Basic Filtering ----

    def ingest(self, launches: Iterable[PumpFunLaunch]) -> tuple[int, list[LaunchRejection]]:
        """Apply basic filtering to new launch events; track the survivors.

        Returns ``(accepted_count, rejections)`` — rejections carry
        reasons so filtering stays observable, mirroring
        :class:`~meme_intelligence.scanners.discovery.RejectedPool`.
        """
        now = self._now()
        accepted = 0
        rejections: list[LaunchRejection] = []
        for launch in launches:
            key = launch.token.address.lower()
            if key in self._tracked:
                continue  # duplicate event
            reason = self._basic_filter(launch)
            if reason is not None:
                rejections.append(LaunchRejection(launch, reason))
                continue
            if len(self._tracked) >= self._s.max_pending:
                rejections.append(LaunchRejection(
                    launch, f"tracking capacity reached ({self._s.max_pending} pending)"))
                continue
            self._tracked[key] = _TrackedLaunch(
                launch=launch,
                first_seen=now,
                # First traction check after one full interval: a launch
                # needs time to show anything beyond its creation state.
                next_check_at=now + timedelta(seconds=self._s.recheck_interval_seconds),
            )
            accepted += 1
        if accepted or rejections:
            self._logger.info(
                "launch batch: %d accepted for tracking, %d rejected, %d tracked total",
                accepted, len(rejections), len(self._tracked))
        return accepted, rejections

    def _basic_filter(self, launch: PumpFunLaunch) -> str | None:
        """Structural gates on the launch event itself; returns a rejection reason or None."""
        if launch.launchpad is not None and launch.launchpad not in self._s.launchpad_list:
            return f"launchpad '{launch.launchpad}' not in accepted list"
        if launch.token.name is None and launch.token.symbol is None:
            return "anonymous launch: no name or symbol"
        if (launch.initial_buy_percent is not None
                and launch.initial_buy_percent > self._s.max_creator_buy_percent):
            return (f"creator bought {launch.initial_buy_percent:.1f}% of supply at launch "
                    f"(max {self._s.max_creator_buy_percent:.0f}%): insider-grab pattern")
        return None

    # ---- Migration events (graduation fast-path) ----

    def note_migration(self, token: TokenIdentity) -> None:
        """Record a bonding-curve graduation for a tracked launch.

        Graduation is the strongest traction signal a launchpad emits, so
        the launch skips further traction rechecks and moves straight to
        READY. Untracked mints are ignored — without launch context they
        are ordinary new pools, and regular pool discovery covers them.
        """
        entry = self._tracked.get(token.address.lower())
        if entry is None:
            return
        entry.graduated = True
        if entry.status is LaunchStatus.PENDING:
            entry.status = LaunchStatus.READY
            entry.promotion_reasons = ("bonding curve completed: token graduated to a DEX",)
            entry.next_check_at = self._now()  # confirmable immediately
            self._logger.info("launch graduated: %s (%s)",
                              token.address, entry.launch.token.symbol or "?")

    # ---- Section 5/8: traction rechecks and promotion ----

    async def recheck_due(self, frontend_client) -> None:
        """Recheck due PENDING launches against the frontend API, within budget.

        ``frontend_client`` provides ``get_coin_state(token)``. Provider
        failures are logged and rescheduled — a broken frontend API slows
        promotion but never stops the monitor (Rules 6/9).
        """
        now = self._now()
        self._expire_stale(now)
        due = sorted(
            (e for e in self._tracked.values()
             if e.status is LaunchStatus.PENDING and e.next_check_at <= now),
            key=lambda e: e.next_check_at,
        )[:self._s.max_rechecks_per_cycle]
        for entry in due:
            entry.next_check_at = now + timedelta(seconds=self._s.recheck_interval_seconds)
            try:
                state = await frontend_client.get_coin_state(entry.launch.token)
            except CollectorError as exc:
                self._logger.warning("traction recheck failed for %s: %s",
                                     entry.launch.token.address, exc)
                continue
            self._apply_state(entry, state, now)

    def _apply_state(self, entry: _TrackedLaunch, state: PumpFunCoinState | None,
                     now: datetime) -> None:
        key = entry.launch.token.address.lower()
        if state is None:
            entry.frontend_misses += 1
            if entry.frontend_misses >= _MAX_FRONTEND_MISSES:
                self._logger.info("dropping %s: not indexed by the frontend API after %d checks",
                                  key, entry.frontend_misses)
                del self._tracked[key]
            return
        entry.frontend_misses = 0
        entry.last_state = state
        if state.is_banned or state.nsfw:
            self._logger.info("dropping %s: banned/nsfw on pump.fun", key)
            del self._tracked[key]
            return
        passed, reasons = self._promotion_gates(entry.launch, state, now)
        if passed:
            entry.status = LaunchStatus.READY
            entry.promotion_reasons = tuple(reasons)
            entry.next_check_at = now  # confirmable immediately
            self._logger.info("launch promoted: %s (%s) — %s",
                              key, entry.launch.token.symbol or "?", "; ".join(reasons))

    def _promotion_gates(self, launch: PumpFunLaunch, state: PumpFunCoinState,
                         now: datetime) -> tuple[bool, list[str]]:
        """Section 8 deep-analysis threshold. Every gate needs DATA to pass —
        a missing metric fails its gate rather than being assumed (Rule 8)."""
        reasons: list[str] = []
        if state.complete:
            return True, ["bonding curve completed: token graduated to a DEX"]

        # Minimum activity level: real buying pushed the cap well past launch size.
        if state.usd_market_cap is None or state.usd_market_cap < self._s.min_usd_market_cap:
            return False, []
        reasons.append(f"market cap ${state.usd_market_cap:,.0f} "
                       f"(gate ${self._s.min_usd_market_cap:,.0f})")

        # Increasing attention: SOL-denominated growth vs the launch snapshot
        # (SOL-to-SOL comparison avoids SOL price drift).
        if launch.market_cap_sol is None or launch.market_cap_sol <= 0 or state.market_cap_sol is None:
            return False, []
        growth = state.market_cap_sol / launch.market_cap_sol
        if growth < self._s.min_market_cap_growth_ratio:
            return False, []
        reasons.append(f"market cap grew {growth:.1f}x since launch "
                       f"(gate {self._s.min_market_cap_growth_ratio:.1f}x)")

        # Evidence of organic interest: people are talking about it.
        if state.reply_count is None or state.reply_count < self._s.min_reply_count:
            return False, []
        reasons.append(f"{state.reply_count} community replies "
                       f"(gate {self._s.min_reply_count})")

        # Still alive: traded recently, not an abandoned spike.
        if state.last_trade_at is None:
            return False, []
        age_minutes = (now - state.last_trade_at).total_seconds() / 60.0
        if age_minutes > self._s.max_last_trade_age_minutes:
            return False, []
        reasons.append(f"last trade {age_minutes:.0f}m ago "
                       f"(gate {self._s.max_last_trade_age_minutes:.0f}m)")

        if state.curve_progress_percent is not None:  # context, not a gate
            reasons.append(f"bonding curve {state.curve_progress_percent:.0f}% complete")
        return True, reasons

    def _expire_stale(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=self._s.pending_ttl_hours)
        stale = [key for key, e in self._tracked.items() if e.first_seen < cutoff]
        for key in stale:
            entry = self._tracked.pop(key)
            self._logger.info("expiring tracked launch %s (%s): no promotion within %.0fh",
                              key, entry.launch.token.symbol or "?", self._s.pending_ttl_hours)

    # ---- Scanner-facing candidate handoff ----

    def ready_candidates(self) -> list[LaunchCandidate]:
        """READY launches due for a market-confirmation attempt this cycle."""
        now = self._now()
        self._expire_stale(now)
        return [
            LaunchCandidate(
                launch=e.launch,
                state=e.last_state,
                reasons=e.promotion_reasons,
                promoted_at=now,
            )
            for e in self._tracked.values()
            if e.status is LaunchStatus.READY and e.next_check_at <= now
        ]

    def confirm(self, token: TokenIdentity) -> None:
        """The scanner confirmed and analyzed this candidate; stop tracking it."""
        self._tracked.pop(token.address.lower(), None)

    def defer(self, token: TokenIdentity) -> None:
        """Market confirmation unavailable this cycle; retry after one interval."""
        entry = self._tracked.get(token.address.lower())
        if entry is not None:
            entry.next_check_at = self._now() + timedelta(
                seconds=self._s.recheck_interval_seconds)

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)


async def collect_launch_candidates(
    stream_client,     # PumpPortalClient-compatible: drain_launches()/drain_migrations()
    frontend_client,   # PumpFunFrontendClient-compatible: get_coin_state(token)
    monitor: LaunchMonitor,
) -> list[LaunchCandidate]:
    """One monitor pass: drain the stream, recheck traction, return READY candidates.

    Mirrors :func:`~meme_intelligence.scanners.discovery.scan_new_pools` —
    the I/O composition lives here so :class:`LaunchMonitor` stays a pure
    state machine (Rule 4).
    """
    monitor.ingest(stream_client.drain_launches())
    for token in stream_client.drain_migrations():
        monitor.note_migration(token)
    await monitor.recheck_due(frontend_client)
    return monitor.ready_candidates()
