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
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from meme_intelligence.alerts.notification_engine import (
    INTEREST_ALERT_TYPES,
    AlertEvent,
    AutomationRules,
    NotificationEngine,
    events_from_security_changes,
    gate_events_by_interest,
)
from meme_intelligence.analyzers.security_monitor import (
    detect_security_changes,
    extract_facts,
    merge_facts,
)
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import AlertPriority, MarketRegime, WatchlistTier
from meme_intelligence.core.errors import (
    AllProvidersFailedError,
    CollectorError,
    MemeIntelError,
)
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.database.storage import Storage
from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools
from meme_intelligence.scanners.launch_monitor import (
    LaunchMonitor,
    collect_launch_candidates,
)
from meme_intelligence.workflow.pipeline import PipelineResult, ResearchPipeline
from meme_intelligence.workflow.watchlist_review import (
    TIER_FOR_CLASSIFICATION as _TIER_FOR_CLASSIFICATION,
)

# Alert types whose evidence rests on market data and therefore get
# multi-source verification before dispatch (Part 15, Section 10).
_VERIFIABLE_ALERT_TYPES = {"high_priority_opportunity", "strong_candidate",
                           "early_opportunity", "momentum"}

_ERROR_BACKOFF_START = 5.0
_ERROR_BACKOFF_MAX = 300.0


def _creator_outflow_usd(wallet_assessment, creator: str | None) -> float | None:
    """Observed net USD outflow of the creator wallet in the recent trade window.

    Reads the per-wallet net flows the wallet analyzer already computed
    (Part 17) — zero extra API calls. A negative net flow means the creator is
    a net seller: that magnitude is the dev-dumping evidence the rug engine
    checks. A creator present with a non-negative flow, or absent from the
    observed window entirely, is an observed zero. No wallet data or no known
    creator returns None — unknown, never fabricated (Rule 8).

    EVM (0x…) addresses compare case-insensitively; Solana base58 addresses are
    case-sensitive and compare exactly.
    """
    if wallet_assessment is None or not creator:
        return None
    fold = creator.lower().startswith("0x")
    wanted = creator.lower() if fold else creator
    for wallet, net_usd in wallet_assessment.net_flows:
        candidate = wallet.lower() if fold else wallet
        if candidate == wanted and net_usd is not None:
            return -net_usd if net_usd < 0 else 0.0
    return 0.0


class _BoundedKeySet:
    """Insertion-ordered membership set with a hard capacity (FIFO eviction).

    The scanner's dedupe/verified caches grow one entry per token forever;
    over weeks on the 1GB droplet with the pump.fun firehose that is
    hundreds of thousands of dead keys (bug-hunt finding). Capping them
    bounds memory; a token evicted after tens of thousands of newer tokens
    is, in practice, one the scanner will never revisit, and re-adding it is
    harmless (at worst one redundant re-analysis). Backed by a dict so it can
    also carry a value (used by the AI-verified cache).
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._data: "OrderedDict[tuple[str, str], object]" = OrderedDict()

    def __contains__(self, key) -> bool:
        return key in self._data

    def add(self, key, value: object = True) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)  # evict oldest

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class CycleStats:
    """What one scan cycle did (logged and aggregated)."""

    cycle: int
    pools_seen: int = 0
    candidates: int = 0
    analyzed: int = 0
    launches_tracked: int = 0  # pump.fun launches under observation (Part 32.5)
    learned: int = 0           # coins fed into the self-learning mind layer
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
        community_client=None,  # CoinGeckoClient-compatible (get_community_profile)
        pumpportal_client=None,  # PumpPortalClient-compatible launch stream (Part 32.5)
        pumpfun_client=None,     # PumpFunFrontendClient-compatible traction rechecks
        wallet_service=None,     # WalletDataService (Part 17); metered credits
        ai_service=None,         # AIJudgmentService (Part 23); costs API tokens
        learning_service=None,   # LearningService (mind layer, Section 10); off by default
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
        # Pump.fun launch funnel (Part 32.5 Section 3): needs the stream,
        # the traction-recheck client, AND an independent market source —
        # without market confirmation a launch candidate can never enter
        # analysis (Section 2), so the stage stays off rather than
        # tracking tokens it could never promote.
        self._pumpportal = pumpportal_client
        self._pumpfun = pumpfun_client
        self._launch_monitor: LaunchMonitor | None = None
        if pumpportal_client is not None and pumpfun_client is not None:
            if market_service is not None:
                self._launch_monitor = LaunchMonitor(settings.pumpfun, now_func=now_func)
            else:
                self._logger.warning(
                    "pump.fun launch discovery disabled: no market service available "
                    "for independent confirmation (Part 32.5 Section 2)")
        # A get_majors-only CoinGecko-compatible client (no community data
        # support) must degrade gracefully rather than crash the whole
        # scanner on the first token (Rule 3/18 — DailyRoutine applies this
        # same guard).
        if community_client is not None and not hasattr(community_client, "get_community_profile"):
            community_client = None
        # Metered layers run in the 24/7 loop only when explicitly opted in
        # (Rule 10/11 — expensive analysis only after filtering, and never
        # by accident). The settings flags are authoritative regardless of
        # what the caller wired in.
        if wallet_service is not None and not settings.wallet.enable_in_monitor:
            self._logger.info(
                "wallet service wired but MEMEINTEL_WALLET_ENABLE_IN_MONITOR is off; "
                "smart-money analysis stays out of the scan loop")
            wallet_service = None
        # Two INDEPENDENT AI modes (Part 23 + Part 32.5 Section 8) — neither
        # flag implies the other. enable_in_monitor judges every analyzed
        # token (expensive — the pipeline gets the service);
        # verify_opportunities judges ONLY tokens that passed all review
        # gates. Previously an `or` here meant MEMEINTEL_AI_VERIFY_
        # OPPORTUNITIES=false was ignored whenever enable_in_monitor was
        # on: a per-token judgment discarded for low confidence (or any
        # other reason) got silently re-attempted a second time on a
        # gate-passing token — a real extra paid call the user explicitly
        # disabled (bug-hunt finding).
        had_ai_service = ai_service is not None
        self._ai_verifier = (ai_service if had_ai_service and settings.ai.verify_opportunities
                             else None)
        if had_ai_service and not settings.ai.enable_in_monitor:
            ai_service = None  # pipeline judges nothing per-token
        if had_ai_service and not settings.ai.enable_in_monitor and not settings.ai.verify_opportunities:
            self._logger.info(
                "AI service wired but both MEMEINTEL_AI_ENABLE_IN_MONITOR and "
                "MEMEINTEL_AI_VERIFY_OPPORTUNITIES are off; AI stays out of the loop")
        # A token verified once stays verified for this scanner's lifetime:
        # without this, a persistent gate-passer got re-judged with a
        # fresh paid Claude call on EVERY watchlist recheck (Part 15
        # Section 2 cadence) forever, even while its alert sat
        # cooldown-suppressed and unseen (bug-hunt finding). The value
        # remembers whether that one verification was INCONCLUSIVE (judgment
        # discarded below the confidence floor / call failed) so the
        # strong-candidate veto holds on later rechecks too — otherwise a
        # token whose judgment was thrown away fired HIGH one cycle later.
        self._ai_verified = _BoundedKeySet(settings.workflow.max_tracked_keys)
        # Self-learning mind layer (Section 10): additive and off by default.
        # Like the metered layers, the settings flag is authoritative — a
        # wired service with the flag off stays out of the loop (Rule 10/11).
        if learning_service is not None and not settings.learning.enable_in_monitor:
            self._logger.info(
                "learning service wired but MEMEINTEL_LEARNING_ENABLE_IN_MONITOR is off; "
                "the mind layer stays out of the scan loop")
            learning_service = None
        self._learning = learning_service
        self._pipeline = ResearchPipeline(settings, goplus_client,
                                          community_client=community_client,
                                          wallet_service=wallet_service,
                                          ai_service=ai_service,
                                          now_func=now_func)
        self._rules = AutomationRules(settings.alerts, settings.alert_engine)
        self._seen = _BoundedKeySet(settings.workflow.max_tracked_keys)
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
        if self._launch_monitor is not None:
            await self._pumpportal.start()  # idempotent background listener
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
                    "cycle %d: %d pools, %d candidates, %d analyzed, "
                    "%d launches tracked, %d learned, %d alerts",
                    cycle, stats.pools_seen, stats.candidates, stats.analyzed,
                    stats.launches_tracked, stats.learned, len(stats.alerts),
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

        # Discovery is ONE source; a GeckoTerminal outage must not stall the
        # launch funnel, watchlist rechecks, security-change detection, or the
        # learning retrain for hours (bug-hunt finding: an uncaught
        # CollectorError here aborted the whole cycle into the outer backoff
        # even though every other stage uses independent providers — Rule 9).
        try:
            candidates, rejected = await scan_new_pools(
                self._gecko, self._discovery, self._settings.workflow.network_list,
            )
        except CollectorError as exc:
            self._logger.warning("pool discovery unavailable this cycle: %s", exc)
            candidates, rejected = [], []
        stats.pools_seen = len(candidates) + len(rejected)
        stats.candidates = len(candidates)

        processed_this_cycle: set[str] = set()
        for candidate in candidates[: self._settings.workflow.top_candidates]:
            token = candidate.pair.base_token
            key = (token.chain, token.address.lower())
            if key in self._seen:
                continue

            result = await self._pipeline.analyze_pair(candidate.pair, regime=self._regime)
            if result is None:
                # Security data not indexed yet — do NOT mark as seen: a
                # never-actually-analyzed token must stay a live candidate
                # for the pump.fun launch path (or a later cycle) to pick
                # up, not get silently dropped when it's confirmed there
                # (bug-hunt finding — _seen previously meant "attempted",
                # not "analyzed").
                continue
            self._seen.add(key)
            stats.analyzed += 1
            processed_this_cycle.add(token.address.lower())
            await self._process_result(
                result, stats, source="continuous_scanner",
                thesis=f"continuous scan cycle {cycle} "
                       f"(discovery {candidate.discovery_score:.0f})",
            )

        # Pump.fun launch funnel (Part 32.5 Sections 3/7): stream events ->
        # basic filtering -> traction rechecks -> market confirmation ->
        # the same analysis pipeline as every other candidate.
        if self._launch_monitor is not None:
            await self._process_launches(cycle, stats, processed_this_cycle)

        # Secondary cadence (Part 15 Section 2): tracked tokens are
        # re-checked every N cycles, not every cycle.
        if (
            self._market is not None
            and cycle % self._settings.workflow.watchlist_recheck_cycles == 0
        ):
            await self._recheck_watchlist(stats, skip=processed_this_cycle)

        # Periodic learning (Section 4/7): the classifier warm-starts once
        # enough coins have resolved. Cheap when not due; error-isolated so a
        # training failure never breaks the loop (Rule 7).
        if self._learning is not None:
            try:
                if self._learning.retrain_if_due():
                    self._logger.info("mind layer retrained (cycle %d)", cycle)
            except Exception as exc:  # noqa: BLE001 — additive; must not kill the cycle
                self._logger.warning("mind layer retrain failed: %s", exc)

        return stats

    async def _process_launches(
        self, cycle: int, stats: CycleStats, processed: set[str],
    ) -> None:
        """Run one launch-monitor pass and analyze market-confirmed candidates.

        A failing launch stage never fails the whole cycle: launchpad
        sources are additive discovery, and regular pool discovery keeps
        working without them (Rule 9).
        """
        try:
            candidates = await collect_launch_candidates(
                self._pumpportal, self._pumpfun, self._launch_monitor)
        except CollectorError as exc:
            self._logger.warning("launch monitor pass failed: %s", exc)
            stats.launches_tracked = self._launch_monitor.tracked_count
            return

        for candidate in candidates:
            token = candidate.launch.token
            key = (token.chain, token.address.lower())
            if key in self._seen:
                self._launch_monitor.confirm(token)
                continue

            # Section 2: discovery is never confirmation — an independent
            # market provider must see the token before deep analysis.
            pair = await self._market.get_best_pair(token.address, chain=token.chain)
            if pair is None:
                self._launch_monitor.defer(token)  # not indexed yet; retry later
                continue

            stats.candidates += 1
            result = await self._pipeline.analyze_pair(pair, regime=self._regime)
            if result is None:
                # Security data not indexed yet — retry rather than losing
                # the candidate (fresh launches lag the security providers).
                self._launch_monitor.defer(token)
                continue

            self._seen.add(key)
            self._launch_monitor.confirm(token)
            stats.analyzed += 1
            processed.add(token.address.lower())
            await self._process_result(
                result, stats, source="pumpfun_launch",
                thesis=f"pump.fun launch (cycle {cycle}): " + "; ".join(candidate.reasons),
                creator=candidate.launch.creator,  # launch tx signer -> deployer reputation
            )

        stats.launches_tracked = self._launch_monitor.tracked_count

    async def _process_result(
        self, result: PipelineResult, stats: CycleStats, *, source: str, thesis: str | None,
        creator: str | None = None,
    ) -> None:
        """Persist, tier, apply rules, verify important alerts, dispatch."""
        token = result.pair.base_token
        previous = self._storage.score_history(token, limit=1)
        previous_score = previous[0]["final_score"] if previous else None

        # AI verification of gate-passing opportunities (Part 32.5 Section 8:
        # deep analysis only after initial requirements). One judgment,
        # re-scored through the locked weighting, then the SAME gates run
        # again on the enriched result — if the judgment holds the score up,
        # the alert fires annotated; if it knocks the score below a gate,
        # the high-priority alert simply never fires (Rule 13 logs why).
        verify_key = (token.chain, token.address.lower())
        ai_inconclusive = self._ai_verified.get(verify_key, False)
        deterministic_veto: str | None = None
        if (self._ai_verifier is not None and not result.security.is_destructive
                and verify_key not in self._ai_verified):
            provisional = self._rules.evaluate(result, previous_score=previous_score)
            if any(e.alert_type in ("high_priority_opportunity", "strong_candidate")
                   for e in provisional):
                # Credit conservation: a paid call is the LAST check, never
                # the first. Every free deterministic signal must be clean
                # before the API is asked for an opinion (Rule 10/11). A
                # vetoed token is NOT cached as verified — if its risk clears
                # on a later recheck, verification can still run then. The
                # veto downgrades BOTH HIGH tiers via deterministic_risk_veto
                # (bug-hunt finding: it used to ride the inconclusive flag,
                # which the fully-verified tier ignored).
                veto = self._ai_spend_veto(result, provisional, creator)
                if veto is not None:
                    self._logger.info(
                        "AI verification skipped for %s: %s — credits saved; "
                        "alert downgraded", token.address, veto)
                    deterministic_veto = veto
                else:
                    self._logger.info("gate-passing candidate %s: running AI verification",
                                      token.address)
                    enriched = await self._pipeline.enrich_with_ai(
                        result, service=self._ai_verifier)
                    # No judgment attached means the verification produced
                    # nothing usable (discarded below the confidence floor, or
                    # the call failed) — recorded so the alert rules treat it
                    # as unconfirmed rather than as if AI never looked (Rule 8).
                    ai_inconclusive = enriched.ai_judgment is None
                    self._ai_verified.add(verify_key, ai_inconclusive)
                    if (enriched is not result
                            and enriched.master.final_score < result.master.final_score):
                        self._logger.info(
                            "AI verification moved %s score %.0f -> %.0f",
                            token.address, result.master.final_score,
                            enriched.master.final_score)
                    result = enriched

        self._storage.record_snapshot(
            result.master, source=source,
            pair=result.pair, regime=self._regime.value,
            opportunity_rank=result.opportunity.score if result.opportunity else None)

        # Contract-change monitoring (Part 18, Section 10): diff the security
        # facts against the last known baseline, then update the baseline.
        previous_facts = self._storage.latest_security_facts(token)
        changes = detect_security_changes(previous_facts, result.security_profile)
        current_facts = extract_facts(result.security_profile)
        self._storage.record_security_facts(token, merge_facts(previous_facts, current_facts))

        # A dead token never (re-)enters the watchlist regardless of its
        # score — the master number still reflects pump-window data, but a
        # collapsed pool is a completed failure, and re-tiering it would
        # re-warn on the corpse every recheck (Part 29 Section 1).
        liquidity = result.pair.liquidity_usd
        is_dead = (liquidity is not None
                   and liquidity < self._settings.alert_engine.dead_liquidity_usd)
        tier = None if is_dead else _TIER_FOR_CLASSIFICATION.get(result.master.classification)
        if tier is not None:
            self._storage.update_watchlist(
                token, tier,
                score=result.master.final_score,
                classification=result.master.classification,
                thesis=thesis,
            )
        elif is_dead or previous_score is not None:
            # Was tracked (or at least scored) before and now fails: archive.
            existing = {e.token.address.lower() for e in self._storage.get_watchlist()}
            if token.address.lower() in existing:
                reason = (
                    f"liquidity collapsed to ${liquidity:,.0f}: token appears dead"
                    if is_dead else
                    f"re-assessment fell to Avoid (score {result.master.final_score:.0f})"
                )
                self._storage.archive(token, reason)

        interest = self._operator_interest(token)
        events = self._rules.evaluate(result, previous_score=previous_score,
                                      ai_verification_inconclusive=ai_inconclusive,
                                      deterministic_risk_veto=deterministic_veto,
                                      operator_interest=interest)
        if result.ai_judgment is not None:
            events = [self._annotate_with_ai(event, result.ai_judgment)
                      for event in events]
        events = await self._verify_events(events, result)
        # Security-change events rest on contract facts, not market data, so
        # they bypass market cross-verification and are appended directly.
        # They pass the same interest gate as the rule-generated events —
        # a HIGH opportunity firing in THIS batch counts as interest, so
        # contradictory signals on a just-recommended token both arrive.
        batch_interest = interest or any(
            e.alert_type in INTEREST_ALERT_TYPES
            and e.priority is AlertPriority.HIGH for e in events)
        events.extend(gate_events_by_interest(
            events_from_security_changes(
                token, changes, master_score=result.master.final_score),
            operator_interest=batch_interest,
            enabled=self._settings.alert_engine.risk_alerts_require_interest))
        delivered = await self._notifier.dispatch(events)
        stats.alerts.extend(delivered)
        for event in delivered:
            # Structured history for Section 12 / Part 24 performance
            # measurement, plus the human-readable journal line.
            self._storage.record_alert(event, source=source)
            self._storage.add_journal(
                token, "alert", f"{event.priority.value}/{event.alert_type}: {event.title}",
            )

        self._feed_learning(result, stats, creator=creator)

    def _operator_interest(self, token) -> bool:
        """Was the operator ever POINTED at this token? (interest gate)

        True when a HIGH opportunity alert (high_priority_opportunity /
        strong_candidate) was previously delivered for it — the only way the
        operator learns about a token, hence the only way a protective alert
        can be guarding a real decision. Fails OPEN: if history cannot be
        read, alerts keep their full priority rather than being silently
        demoted (Rule 6 — an error must not suppress a warning).
        """
        try:
            history = self._storage.alert_history(token, limit=100)
        except Exception as exc:  # noqa: BLE001 — advisory lookup, fail open
            self._logger.warning("interest lookup failed for %s (alerts keep "
                                 "full priority): %s", token.address, exc)
            return True
        return any(row["alert_type"] in INTEREST_ALERT_TYPES for row in history)

    # Alert types that mean "this token is already flagged risky" — a paid AI
    # opinion on it is wasted money, not information.
    _RISK_ALERT_TYPES = frozenset({"emergency_review", "risk_warning"})

    def _ai_spend_veto(self, result: PipelineResult, provisional: list,
                       creator: str | None) -> str | None:
        """Free deterministic checks that gate every paid AI verification.

        Returns the reason to skip (credits saved), or None when everything
        checks out and the AI's opinion is genuinely the missing piece.
        Checks: risk alerts already firing on this token, and the mind
        layer's rug engine (contract facts + deployer blacklist + observed
        dev outflow) — all zero-API-cost.
        """
        risky = sorted({e.alert_type for e in provisional
                        if e.alert_type in self._RISK_ALERT_TYPES})
        if risky:
            return f"risk alerts already firing ({', '.join(risky)})"

        from meme_intelligence.learning.models import CoinSnapshot
        from meme_intelligence.learning.rug_engine import RugEngine

        profile = result.security_profile
        if creator is None and profile is not None:
            creator = profile.creator_address
        deployer_rugs = 0
        if self._learning is not None and creator:
            try:
                deployer_rugs = self._learning.store.deployer_rug_count(
                    creator, result.pair.base_token.chain)
            except Exception:  # noqa: BLE001 — advisory lookup, never blocks
                deployer_rugs = 0
        snapshot = CoinSnapshot.from_dict({
            "age_seconds": 0.0,
            "volume_1h_usd": result.pair.volume_1h,
            "holder_count": profile.holder_count if profile is not None else None,
            "dev_outflow_usd": _creator_outflow_usd(result.wallet, creator),
        })
        rug = RugEngine(self._settings.rug_signal_weights,
                        self._settings.rug_thresholds).assess(
            security=profile, snapshots=(snapshot,),
            deployer_rug_count=deployer_rugs)
        if rug.score >= self._settings.ai.verify_skip_rug_score:
            return (f"rug engine score {rug.score:.0f} >= "
                    f"{self._settings.ai.verify_skip_rug_score:.0f} "
                    f"(signals: {', '.join(rug.fired_names)})")
        return None

    def _feed_learning(self, result: PipelineResult, stats: CycleStats,
                       *, creator: str | None = None) -> None:
        """Feed one analyzed coin into the mind layer (Section 10).

        Additive and fully error-isolated: it records the coin and appends a
        trajectory snapshot (built from data already collected — zero extra API
        calls) so the mind layer accumulates memory as the scanner runs. Any
        failure is logged and swallowed — the learning hook must never break a
        scan cycle (Rule 7/9). The outer loop only catches ``MemeIntelError``,
        so this catches broadly (the LearningService uses numpy/faiss/lightgbm).
        """
        if self._learning is None:
            return
        try:
            pair = result.pair
            token = pair.base_token
            profile = result.security_profile
            # Launch-event signer wins (chain truth); GoPlus's creator_address
            # covers tokens discovered without a launch event.
            if creator is None and profile is not None:
                creator = profile.creator_address
            age_seconds = 0.0
            if pair.pair_created_at is not None:
                age_seconds = max(0.0, (self._now() - pair.pair_created_at).total_seconds())
            snapshot = {
                "age_seconds": age_seconds,
                "price_usd": pair.price_usd,
                "liquidity_usd": pair.liquidity_usd,
                "market_cap_usd": pair.market_cap,
                "volume_1h_usd": pair.volume_1h,
                "holder_count": profile.holder_count if profile is not None else None,
                "buys": pair.buys_1h,
                "sells": pair.sells_1h,
                "top10_holder_percent":
                    profile.top10_holder_percent if profile is not None else None,
                # Dev-dumping evidence from the wallet analyzer's net flows
                # (Part 17) — None when wallet data / creator are unknown.
                "dev_outflow_usd": _creator_outflow_usd(result.wallet, creator),
            }
            self._learning.record_detection(
                token.address, token.chain, detection_price_usd=pair.price_usd,
                symbol=token.symbol, name=token.name, creator=creator)
            self._learning.capture_snapshot(token.address, token.chain, snapshot)
            # Also record a verdict (insert-once, first sighting wins) so the
            # coin can be GRADED when it resolves — without a stored
            # prediction, the adaptive ensemble weights and the Section 8
            # report card never update in monitor-only operation. All local
            # models; zero API cost.
            self._learning.evaluate_coin(token.address, token.chain, [snapshot],
                                         security=profile, creator=creator)
            stats.learned += 1
        except Exception as exc:  # noqa: BLE001 — additive; must not kill the cycle
            self._logger.warning("learning hook failed for %s: %s",
                                 result.pair.base_token.address, exc)

    async def _recheck_watchlist(self, stats: CycleStats, *, skip: set[str] = frozenset()) -> None:
        """Re-analyze tracked tokens on the slower cadence (Part 15, Section 2).

        Entries are visited least-recently-updated FIRST (bug-hunt finding:
        the default tier/score ordering plus the per-pass limit meant entries
        ranked below the top N were never re-assessed, never archived, and
        kept stale scores forever — every review bumps updated_at, so this
        ordering rotates the limit through the whole list).
        """
        entries = sorted(self._storage.get_watchlist(), key=lambda e: e.updated_at)
        limit = self._settings.workflow.watchlist_review_limit
        rechecked = 0
        for entry in entries:
            if rechecked >= limit:
                break
            if entry.token.address.lower() in skip:
                continue  # analyzed moments ago this cycle; nothing new to learn
            if entry.tier is WatchlistTier.TIER_3_RESEARCH_ONLY:
                continue  # research-only entries wait for the daily routine
            # Fetch via get_token_pairs, which RAISES on a provider outage,
            # rather than get_best_pair, which collapses "all providers down"
            # into the same None as "token has no pairs" (bug-hunt finding:
            # a 2-failure blip passed the old health() heuristic — providers
            # only report unhealthy after 3 consecutive failures — and
            # permanently archived healthy tokens). Archive ONLY when a
            # SUCCESSFUL call says the market is empty (Rule 8), exactly as
            # watchlist_review.review_entries does.
            try:
                pairs = await self._market.get_token_pairs(
                    entry.token.address, chain=entry.token.chain)
            except (CollectorError, AllProvidersFailedError) as exc:
                self._logger.warning(
                    "watchlist recheck for %s skipped: market data unavailable (%s)",
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

    @staticmethod
    def _annotate_with_ai(event: AlertEvent, judgment) -> AlertEvent:
        """Carry the AI verification into opportunity alerts (Part 32.5 S8).

        Only opportunity alerts are annotated — they are what the judgment
        was run to verify. The strongest bear-case point rides along so the
        alert never reads as unconditional endorsement (Part 23 doctrine:
        always surface possible losses).
        """
        if event.alert_type not in ("high_priority_opportunity", "strong_candidate",
                                    "early_opportunity"):
            return event
        extra = [f"AI verification: judgment confidence "
                 f"{judgment.confidence:.0f}/100 ({judgment.model})"]
        if judgment.bear_case:
            extra.append(f"AI caution: {judgment.bear_case[0]}")
        return dataclasses.replace(event, reasons=event.reasons + tuple(extra))

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
