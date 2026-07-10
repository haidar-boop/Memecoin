"""Daily operating routine (Spec Part 11).

Runs the research desk's day in order:

1. **Morning market check** (Section 2): BTC/ETH/SOL 24h trend from
   CoinGecko -> risk-on / neutral / risk-off regime. Provider failure
   degrades to UNKNOWN regime with a note — the routine keeps running on
   remaining data (Rule 9).
2. **Discovery scan** (Section 3): new pools -> discovery engine.
3. **First-level filtering + deep analysis** (Section 4): security screen,
   on-chain, token structure, risk, master assessment for the top
   candidates; every assessment snapshot is persisted for the learning
   system (Part 24).
4. **Watchlist management** (Section 5): classifications map to tiers —
   Strong/Elite -> Tier 1, Watchlist -> Tier 2, Speculative -> Tier 3,
   Avoid -> archived (or never added).
5. **Watchlist review** (Sections 6-7): existing entries are re-scored
   (bounded per run) and re-tiered; deteriorated entries are archived
   with the reason journaled.
6. **Daily final report** (Section 14): market summary, ranked
   opportunities, biggest risks, watchlist changes.

The objective is not maximum activity — it is maximum quality (Part 11
final rule): most scanned tokens should end the day rejected or ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from meme_intelligence.analyzers.scoring_engine import MasterAssessment
from meme_intelligence.analyzers.security_monitor import (
    detect_security_changes,
    extract_facts,
    merge_facts,
)
from meme_intelligence.collectors.market_data import MajorsSnapshot
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import Classification, MarketRegime, RiskTier, WatchlistTier
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair
from meme_intelligence.database.storage import Storage, WatchlistChange
from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools
from meme_intelligence.workflow.pipeline import PipelineResult, ResearchPipeline
from meme_intelligence.workflow.watchlist_review import (
    TIER_FOR_CLASSIFICATION,
    review_entries,
)


@dataclass(frozen=True)
class MarketEnvironment:
    """Morning market-environment verdict (Part 11, Section 2)."""

    regime: MarketRegime
    btc_price_usd: float | None
    btc_change_24h_percent: float | None
    eth_change_24h_percent: float | None
    sol_change_24h_percent: float | None
    note: str

    def summary(self) -> str:
        def pct(value: float | None) -> str:
            return f"{value:+.1f}%" if value is not None else "unknown"

        price = f"${self.btc_price_usd:,.0f}" if self.btc_price_usd is not None else "unknown"
        return (
            f"Market regime: {self.regime.value} — BTC {price} ({pct(self.btc_change_24h_percent)} 24h), "
            f"ETH {pct(self.eth_change_24h_percent)}, SOL {pct(self.sol_change_24h_percent)}. {self.note}"
        )


@dataclass(frozen=True)
class OpportunityLine:
    """One ranked opportunity in the daily report."""

    symbol: str
    chain: str
    address: str
    final_score: float
    classification: Classification
    discovery_score: float | None


@dataclass
class DailyReport:
    """End-of-day summary (Part 11, Section 14)."""

    date: datetime
    environment: MarketEnvironment
    pools_scanned: int = 0
    candidates_analyzed: int = 0
    rejected_at_discovery: int = 0
    opportunities: list[OpportunityLine] = field(default_factory=list)
    biggest_risks: list[str] = field(default_factory=list)
    watchlist_changes: list[WatchlistChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"DAILY INTELLIGENCE REPORT — {self.date.strftime('%Y-%m-%d %H:%M UTC')}",
            f"  {self.environment.summary()}",
            f"  Scanned {self.pools_scanned} new pools: {self.rejected_at_discovery} rejected "
            f"at discovery, {self.candidates_analyzed} deep-analyzed.",
        ]
        if self.opportunities:
            lines.append("  Best opportunities:")
            for i, opp in enumerate(self.opportunities[:5], 1):
                lines.append(
                    f"    {i}. {opp.symbol} ({opp.chain}) — score {opp.final_score:.0f}, "
                    f"{opp.classification.value}"
                )
        else:
            lines.append("  Best opportunities: none met the bar today (that is a valid outcome).")
        if self.biggest_risks:
            lines.append("  Biggest risks observed:")
            for risk in self.biggest_risks[:5]:
                lines.append(f"    - {risk}")
        if self.watchlist_changes:
            lines.append("  Watchlist changes:")
            for change in self.watchlist_changes:
                symbol = change.token.symbol or change.token.address[:8]
                lines.append(f"    - {symbol}: {change.change} ({change.detail})")
        else:
            lines.append("  Watchlist changes: none")
        for note in self.notes:
            lines.append(f"  Note: {note}")
        return "\n".join(lines)


def assess_market_environment(
    majors: MajorsSnapshot | None,
    *,
    risk_on_change: float,
    risk_off_drop: float,
    note: str = "",
) -> MarketEnvironment:
    """Map the majors' 24h trend onto a market regime (Part 11, Section 2)."""
    if majors is None or majors.btc_change_24h_percent is None:
        return MarketEnvironment(
            regime=MarketRegime.UNKNOWN,
            btc_price_usd=majors.btc_price_usd if majors else None,
            btc_change_24h_percent=None,
            eth_change_24h_percent=majors.eth_change_24h_percent if majors else None,
            sol_change_24h_percent=majors.sol_change_24h_percent if majors else None,
            note=note or "BTC trend unavailable; regime unknown — require stronger confirmation.",
        )

    change = majors.btc_change_24h_percent
    if change >= risk_on_change:
        regime, verdict = MarketRegime.BULL, "risk-on conditions"
    elif change <= -risk_off_drop:
        regime, verdict = MarketRegime.BEAR, "risk-off conditions; be highly selective"
    else:
        regime, verdict = MarketRegime.NEUTRAL, "mixed conditions; require stronger confirmation"

    return MarketEnvironment(
        regime=regime,
        btc_price_usd=majors.btc_price_usd,
        btc_change_24h_percent=majors.btc_change_24h_percent,
        eth_change_24h_percent=majors.eth_change_24h_percent,
        sol_change_24h_percent=majors.sol_change_24h_percent,
        note=note or verdict,
    )


class DailyRoutine:
    """Orchestrates one full research-desk day (Part 11)."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        *,
        gecko_client,       # GeckoTerminalClient-compatible (get_new_pools)
        goplus_client,      # GoPlusClient-compatible (get_token_security)
        jupiter_client=None,  # JupiterClient-compatible (check_round_trip_liquidity)
        coingecko_client=None,  # CoinGeckoClient-compatible (get_majors), optional
        dexscreener_client=None,  # for watchlist review refresh, optional
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._gecko = gecko_client
        self._goplus = goplus_client
        self._coingecko = coingecko_client
        self._dexscreener = dexscreener_client
        self._now = now_func
        self._logger = get_logger("workflow.daily")

        self._discovery = DiscoveryEngine(settings.discovery, now_func=now_func)
        self._pipeline = ResearchPipeline(settings, goplus_client, jupiter_client=jupiter_client,
                                          now_func=now_func)

    async def run(self) -> DailyReport:
        report = DailyReport(date=self._now(), environment=await self._market_check())
        self._logger.info("daily routine started: %s", report.environment.summary())

        await self._discover_and_analyze(report)
        await self._review_watchlist(report)

        self._storage.add_journal(None, "daily_report", report.render())
        self._logger.info(
            "daily routine finished: %d analyzed, %d opportunities, %d watchlist changes",
            report.candidates_analyzed, len(report.opportunities), len(report.watchlist_changes),
        )
        return report

    # ---- Step 1: market environment ----

    async def _market_check(self) -> MarketEnvironment:
        majors = None
        note = ""
        if self._coingecko is not None:
            try:
                majors = await self._coingecko.get_majors()
            except CollectorError as exc:
                note = f"market data provider unavailable ({exc}); continuing without regime."
                self._logger.warning(note)
        else:
            note = "no market-environment provider configured; regime unknown."
        return assess_market_environment(
            majors,
            risk_on_change=self._settings.workflow.risk_on_btc_change_percent,
            risk_off_drop=self._settings.workflow.risk_off_btc_drop_percent,
            note=note,
        )

    # ---- Steps 2-4: discovery, analysis, watchlist intake ----

    async def _discover_and_analyze(self, report: DailyReport) -> None:
        try:
            candidates, rejected = await scan_new_pools(
                self._gecko, self._discovery, self._settings.workflow.network_list,
            )
        except CollectorError as exc:
            report.notes.append(f"discovery unavailable: {exc}")
            self._logger.warning("discovery scan failed: %s", exc)
            return

        report.pools_scanned = len(candidates) + len(rejected)
        report.rejected_at_discovery = len(rejected)

        for candidate in candidates[: self._settings.workflow.top_candidates]:
            master = await self._analyze(candidate.pair, report)
            if master is None:
                continue
            report.candidates_analyzed += 1
            self._intake(candidate.pair, master, candidate.discovery_score, report)

    async def _analyze(self, pair: DexPair, report: DailyReport) -> MasterAssessment | None:
        """Layers 2-3 for one pair via the shared pipeline; None when unanalyzable."""
        result = await self._pipeline.analyze_pair(pair, regime=report.environment.regime)
        if result is None:
            return None
        token = pair.base_token
        symbol = token.symbol or token.address[:8]
        self._storage.record_snapshot(result.master, source="daily_routine")

        # Contract-change monitoring (Part 18 Section 10): worsened facts are
        # front-page risks in the daily report and journaled for the record.
        previous_facts = self._storage.latest_security_facts(token)
        changes = detect_security_changes(previous_facts, result.security_profile)
        self._storage.record_security_facts(
            token, merge_facts(previous_facts, extract_facts(result.security_profile)))
        for change in changes:
            report.biggest_risks.insert(0, f"{symbol}: SECURITY CHANGE — {change.message}")
            self._storage.add_journal(token, "security_change", change.message)

        for finding in result.security.findings:
            if finding.severity in (RiskTier.DESTRUCTIVE, RiskTier.SERIOUS_WARNING):
                report.biggest_risks.append(f"{symbol}: {finding.message}")
        return result.master

    def _intake(self, pair: DexPair, master: MasterAssessment,
                discovery_score: float | None, report: DailyReport) -> None:
        symbol = pair.base_token.symbol or pair.base_token.address[:8]
        tier = TIER_FOR_CLASSIFICATION.get(master.classification)
        if tier is None:  # AVOID: never added; journal the rejection for learning
            self._storage.add_journal(
                pair.base_token, "decision",
                f"rejected at intake: score {master.final_score:.0f}, "
                f"overrides={list(master.overrides)}",
            )
            return

        change = self._storage.update_watchlist(
            pair.base_token, tier,
            score=master.final_score, classification=master.classification,
            thesis=f"discovered via daily scan (discovery {discovery_score or 0:.0f}, "
                   f"master {master.final_score:.0f})",
        )
        report.watchlist_changes.append(change)
        report.opportunities.append(OpportunityLine(
            symbol=symbol, chain=pair.chain, address=pair.base_token.address,
            final_score=master.final_score, classification=master.classification,
            discovery_score=discovery_score,
        ))
        report.opportunities.sort(key=lambda o: o.final_score, reverse=True)

    # ---- Step 5: existing watchlist review (Part 11, Sections 6-7) ----

    async def _review_watchlist(self, report: DailyReport) -> None:
        if self._dexscreener is None:
            return
        # Skip tokens just added this run — nothing new to learn yet.
        skip = {c.token.address.lower() for c in report.watchlist_changes}

        async def collect_risks(result: PipelineResult) -> None:
            for finding in result.security.findings:
                if finding.severity in (RiskTier.DESTRUCTIVE, RiskTier.SERIOUS_WARNING):
                    symbol = result.pair.base_token.symbol or result.pair.base_token.address[:8]
                    report.biggest_risks.append(f"{symbol}: {finding.message}")

        changes = await review_entries(
            self._storage, self._dexscreener, self._pipeline,
            regime=report.environment.regime,
            limit=self._settings.workflow.watchlist_review_limit,
            skip=skip,
            snapshot_source="daily_routine",
            on_result=collect_risks,
        )
        # Only surface meaningful movements; silent refreshes stay silent.
        report.watchlist_changes.extend(c for c in changes if c.change != "updated")
