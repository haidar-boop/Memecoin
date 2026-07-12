"""Trade planning engine (Spec Part 8, with Part 9 risk discipline).

Builds a :class:`TradePlan` from the analysis engines' outputs: a trade
score (Part 8 Section 13), setup classification, conviction level with
position-size *guidance*, the pre-entry checklist (Section 3), required
confirmations, invalidation conditions, and the FOMO-prevention questions
(Section 11).

Discipline rules encoded here:

* Destructive security risk => ``NO_TRADE``, whatever else looks good.
* Low evidence coverage caps conviction at SPECULATIVE — discovery is not
  confirmation (Part 31, Section 6).
* A bear regime downgrades conviction one level (Part 8, Section 10).
* Unknown checklist items stay UNKNOWN — unverified is not a pass (Rule 8).

Never enter a trade without knowing when you are wrong: every plan carries
explicit invalidation conditions.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from meme_intelligence.analyzers.community_analyzer import CommunityAssessment
from meme_intelligence.analyzers.onchain_analyzer import OnChainAssessment
from meme_intelligence.analyzers.security_analyzer import SecurityAssessment
from meme_intelligence.analyzers.token_analyzer import TokenAssessment
from meme_intelligence.config.settings import TradeScoreWeights, TradingSettings
from meme_intelligence.core.enums import (
    CheckStatus,
    ConvictionLevel,
    MarketCapStage,
    MarketPhase,
    MarketRegime,
    RiskTier,
    SetupType,
)
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair
from meme_intelligence.scanners.discovery import TokenCandidate

# FOMO prevention questions asked before every entry (Part 8, Section 11).
FOMO_QUESTIONS = (
    "Would I buy this if the price was not moving?",
    "Am I entering because of data or excitement?",
    "Has the opportunity already become crowded?",
    "What is my exit plan?",
    "What would prove my thesis wrong?",
)

# Profit-taking discipline reminders (Part 8, Section 7).
PROFIT_DISCIPLINE = (
    "Scale out in portions instead of selling everything at once.",
    "Take partial profits at predefined levels to lock gains and reduce emotional pressure.",
    "Reduce exposure if community growth stops, smart money exits, or volume collapses.",
    "Never move risk limits because of emotion (Part 8, Section 9).",
)

# Regime downgrade order for bear markets (Part 8, Section 10).
_DOWNGRADE = {
    ConvictionLevel.HIGH: ConvictionLevel.MEDIUM,
    ConvictionLevel.MEDIUM: ConvictionLevel.SPECULATIVE,
    ConvictionLevel.SPECULATIVE: ConvictionLevel.SPECULATIVE,
    ConvictionLevel.NO_TRADE: ConvictionLevel.NO_TRADE,
}

_REGIME_SCORES = {
    MarketRegime.BULL: 80.0,
    MarketRegime.NEUTRAL: 60.0,
    MarketRegime.BEAR: 30.0,
    MarketRegime.UNKNOWN: None,
}

# Only genuine per-token evidence lifts the SPECULATIVE cap. The global
# market-regime signal is not about THIS token, and the risk/reward heuristic
# is always derivable (never absent), so neither counts as confirmation that
# the token itself has been vetted (Part 31, Section 6).
_PER_TOKEN_EVIDENCE = ("setup_quality", "security", "community", "onchain")

# Human-readable confirmation tasks for the most decision-relevant unknowns.
_CONFIRMATION_TASKS = {
    "is_honeypot": "Confirm the token is actually sellable (honeypot simulation)",
    "cannot_sell_all": "Confirm holders can sell their full balance",
    "lp_locked_percent": "Confirm LP lock/burn status and duration",
    "top10_holder_percent": "Confirm holder distribution (top-10 concentration)",
    "top_holder_percent": "Confirm the largest holder's share of supply",
    "creator_percent": "Confirm the creator wallet's holdings",
    "holder_count": "Confirm the real holder count",
    "ownership_renounced": "Confirm contract ownership status",
    "tax_percent": "Confirm buy/sell taxes",
    "is_open_source": "Confirm the contract source is verified",
}
_MAX_CONFIRMATIONS = 6


@dataclass(frozen=True)
class ChecklistItem:
    """One pre-entry checklist line (Part 8, Section 3)."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class TradeJournalEntry:
    """Trade journal schema (Part 8, Section 12). Persistence arrives with
    the database phase; the schema is defined now so plans and journals
    stay structurally aligned."""

    token_symbol: str
    token_address: str
    chain: str
    entered_at: datetime | None = None
    entry_price: float | None = None
    entry_market_cap: float | None = None
    entry_reason: str | None = None
    expected_catalysts: str | None = None
    known_risks: str | None = None
    exited_at: datetime | None = None
    exit_price: float | None = None
    profit_loss_percent: float | None = None
    what_went_right: str | None = None
    what_went_wrong: str | None = None
    lessons: str | None = None


@dataclass(frozen=True)
class TradePlan:
    """A generated research plan for one potential entry. NOT an order."""

    token_symbol: str
    token_address: str
    chain: str
    created_at: datetime
    setup_type: SetupType
    conviction: ConvictionLevel
    trade_score: float
    score_components: dict[str, float | None]
    score_coverage: float
    max_position_percent: float | None  # guidance ceiling; None for NO_TRADE
    regime: MarketRegime
    entry_reason: str
    checklist: tuple[ChecklistItem, ...]
    confirmations_required: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    fomo_questions: tuple[str, ...] = FOMO_QUESTIONS
    profit_discipline: tuple[str, ...] = PROFIT_DISCIPLINE

    def render(self) -> str:
        lines = [
            f"TRADE PLAN — {self.token_symbol} ({self.chain})   [research only, not advice]",
            f"  Setup: {self.setup_type.value}   Conviction: {self.conviction.value}   "
            f"Regime: {self.regime.value}",
            f"  Trade score: {self.trade_score:.0f}/100 "
            f"(evidence coverage {self.score_coverage:.0%})",
        ]
        if self.max_position_percent is not None:
            lines.append(f"  Max position guidance: {self.max_position_percent}% of portfolio")
        elif self.conviction is ConvictionLevel.NO_TRADE:
            lines.append("  Max position guidance: DO NOT ENTER")
        else:
            lines.append("  Max position guidance: WATCH ONLY — no entry under current conditions")
        lines.append(f"  Entry reason: {self.entry_reason}")

        lines.append("  Checklist:")
        for item in self.checklist:
            mark = {"pass": "+", "fail": "x", "unknown": "?"}[item.status.value]
            lines.append(f"    [{mark}] {item.name}: {item.detail}")

        if self.confirmations_required:
            lines.append("  Confirm before sizing up:")
            for task in self.confirmations_required:
                lines.append(f"    - {task}")

        lines.append("  Invalidation conditions (exit if any occurs):")
        for condition in self.invalidation_conditions:
            lines.append(f"    - {condition}")

        lines.append("  Before entering, answer honestly:")
        for question in self.fomo_questions:
            lines.append(f"    ? {question}")
        return "\n".join(lines)


class TradePlanner:
    """Builds disciplined trade plans from analysis outputs (Part 8)."""

    def __init__(
        self,
        settings: TradingSettings,
        weights: TradeScoreWeights,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._s = settings
        self._w = weights
        self._now = now_func
        self._logger = get_logger("trading.planner")

    def build_plan(
        self,
        pair: DexPair,
        security: SecurityAssessment,
        *,
        discovery: TokenCandidate | None = None,
        onchain: OnChainAssessment | None = None,
        community: CommunityAssessment | None = None,
        token: TokenAssessment | None = None,
        regime: MarketRegime = MarketRegime.UNKNOWN,
    ) -> TradePlan:
        components = self._score_components(pair, security, discovery, onchain,
                                            community, token, regime)
        trade_score, coverage = self._combine(components)
        confirmation_coverage = self._confirmation_coverage(components)
        setup = self._setup_type(pair, security, onchain)
        conviction = self._conviction(trade_score, confirmation_coverage, security, regime)
        checklist = self._checklist(security, onchain, community)
        confirmations = self._confirmations(security, onchain, community)
        invalidations = self._invalidations(security, onchain)

        # A watch-only setup means "monitor, do not enter now" — it must not
        # carry position sizing, whatever the evidence grade says.
        guidance = self._position_guidance(conviction)
        if setup is SetupType.WATCH_ONLY:
            guidance = None

        symbol = pair.base_token.symbol or pair.base_token.address[:8]
        self._logger.info(
            "trade plan %s/%s: score=%.0f setup=%s conviction=%s",
            pair.chain, pair.base_token.address, trade_score, setup.value, conviction.value,
        )

        return TradePlan(
            token_symbol=symbol,
            token_address=pair.base_token.address,
            chain=pair.chain,
            created_at=self._now(),
            setup_type=setup,
            conviction=conviction,
            trade_score=trade_score,
            score_components=components,
            score_coverage=coverage,
            max_position_percent=guidance,
            regime=regime,
            entry_reason=self._entry_reason(setup, security, onchain, conviction),
            checklist=tuple(checklist),
            confirmations_required=tuple(confirmations),
            invalidation_conditions=tuple(invalidations),
        )

    # ---- Trade score (Part 8, Section 13) ----

    def _score_components(
        self, pair, security, discovery, onchain, community, token, regime,
    ) -> dict[str, float | None]:
        return {
            "setup_quality": discovery.discovery_score if discovery else None,
            "security": security.overall_score,
            "community": community.overall_score if community else None,
            "onchain": onchain.overall_score if onchain else None,
            "market_conditions": _REGIME_SCORES[regime],
            "risk_reward": self._risk_reward(security, onchain, token),
        }

    def _combine(self, components: dict[str, float | None]) -> tuple[float, float]:
        weight_map = dataclasses.asdict(self._w)
        weighted_sum = 0.0
        available = 0.0
        for name, value in components.items():
            if value is not None:
                weighted_sum += value * weight_map[name]
                available += weight_map[name]
        if available == 0.0:
            return 0.0, 0.0
        return weighted_sum / available, available

    def _confirmation_coverage(self, components: dict[str, float | None]) -> float:
        """Weight of genuine per-token evidence backing the score. Excludes the
        global regime signal and the always-present risk/reward heuristic, so
        discovery/security alone cannot lift the SPECULATIVE cap."""
        weight_map = dataclasses.asdict(self._w)
        return sum(
            weight_map[name]
            for name in _PER_TOKEN_EVIDENCE
            if components.get(name) is not None
        )

    def _risk_reward(self, security, onchain, token) -> float:
        """Heuristic risk/reward grade (Part 8 Section 13, Part 25 doctrine):
        upside room and healthy behavior add; serious warnings subtract;
        normal early-stage uncertainty is not punished."""
        score = 50.0
        if token is not None:
            if token.stage is MarketCapStage.EARLY:
                score += 15.0
            elif token.stage is MarketCapStage.GROWTH:
                score += 5.0
        if security.tier is RiskTier.ACCEPTABLE_UNCERTAINTY:
            score += 15.0
        serious = sum(1 for f in security.findings if f.severity is RiskTier.SERIOUS_WARNING)
        score -= min(30.0, serious * 10.0)
        if onchain is not None and onchain.phase in (MarketPhase.ACCUMULATION, MarketPhase.EXPANSION):
            score += 10.0
        return max(0.0, min(100.0, score))

    # ---- Setup classification (Part 8, Section 2) ----

    def _setup_type(self, pair, security, onchain) -> SetupType:
        if security.is_destructive:
            return SetupType.WATCH_ONLY
        age_hours = None
        if pair.pair_created_at is not None:
            age_hours = (self._now() - pair.pair_created_at).total_seconds() / 3600.0
        phase = onchain.phase if onchain else MarketPhase.UNCLEAR

        if age_hours is not None and age_hours <= 24.0:
            return SetupType.EARLY_DISCOVERY
        if phase is MarketPhase.EXPANSION:
            return SetupType.TREND_CONTINUATION if (age_hours or 0) > 7 * 24 else SetupType.CONFIRMATION
        if phase is MarketPhase.ACCUMULATION:
            return SetupType.CONFIRMATION
        return SetupType.WATCH_ONLY

    # ---- Conviction & sizing guidance (Part 8, Sections 5-6; Part 9) ----

    def _conviction(self, trade_score, confirmation_coverage, security, regime) -> ConvictionLevel:
        if security.is_destructive:
            return ConvictionLevel.NO_TRADE

        if (
            trade_score >= self._s.high_conviction_min_score
            and security.overall_score >= self._s.high_conviction_min_security
        ):
            conviction = ConvictionLevel.HIGH
        elif trade_score >= self._s.medium_conviction_min_score:
            conviction = ConvictionLevel.MEDIUM
        else:
            conviction = ConvictionLevel.SPECULATIVE

        # Discovery is not confirmation: thin per-token evidence caps conviction.
        if confirmation_coverage < self._s.min_confirmation_coverage:
            conviction = ConvictionLevel.SPECULATIVE if conviction is not ConvictionLevel.NO_TRADE \
                else conviction

        if regime is MarketRegime.BEAR:
            conviction = _DOWNGRADE[conviction]
        return conviction

    def _position_guidance(self, conviction: ConvictionLevel) -> float | None:
        return {
            ConvictionLevel.HIGH: self._s.high_conviction_max_position_percent,
            ConvictionLevel.MEDIUM: self._s.medium_conviction_max_position_percent,
            ConvictionLevel.SPECULATIVE: self._s.speculative_max_position_percent,
            ConvictionLevel.NO_TRADE: None,
        }[conviction]

    # ---- Entry checklist (Part 8, Section 3) ----

    def _checklist(self, security, onchain, community) -> list[ChecklistItem]:
        items: list[ChecklistItem] = []

        def add(name: str, value: float | None, threshold: float, detail_ok: str, detail_bad: str):
            if value is None:
                items.append(ChecklistItem(name, CheckStatus.UNKNOWN, "no data yet"))
            elif value >= threshold:
                items.append(ChecklistItem(name, CheckStatus.PASS, detail_ok))
            else:
                items.append(ChecklistItem(name, CheckStatus.FAIL, detail_bad))

        if security.is_destructive:
            items.append(ChecklistItem("No destructive risk", CheckStatus.FAIL,
                                       security.destructive_findings[0].message))
        else:
            items.append(ChecklistItem("No destructive risk", CheckStatus.PASS,
                                       "no honeypot/scam indicators confirmed"))

        add("Contract security", security.sub_scores.get("contract"), 60,
            "contract checks passed", "contract permissions carry risk")
        add("Liquidity safety", security.sub_scores.get("liquidity"), 60,
            "liquidity acceptable", "liquidity is thin or unlockable")
        add("Holder distribution", security.sub_scores.get("distribution"), 50,
            "no dangerous concentration", "supply is concentrated")

        add("Community organic", community.overall_score if community else None, 50,
            f"rating: {community.rating.value}" if community else "",
            f"rating: {community.rating.value}" if community else "")
        add("On-chain health", onchain.overall_score if onchain else None, 50,
            "wallet behavior acceptable", "on-chain behavior weak")
        add("Volume quality", onchain.sub_scores.get("volume_quality") if onchain else None, 50,
            "volume looks organic", "volume quality is suspect")
        return items

    # ---- Required confirmations from unknowns (Part 31, Section 6) ----

    def _confirmations(self, security, onchain, community) -> list[str]:
        tasks: list[str] = []
        # A whole missing analysis category outranks individual unknown fields.
        if community is None:
            tasks.append("Assess community authenticity before sizing up")
        unknown_fields: list[str] = list(security.unknown_fields)
        if onchain is not None:
            unknown_fields.extend(onchain.unknown_fields)
        for field_name in unknown_fields:
            task = _CONFIRMATION_TASKS.get(field_name)
            if task and task not in tasks:
                tasks.append(task)
        return tasks[:_MAX_CONFIRMATIONS]

    # ---- Invalidation conditions (Part 8, Sections 8-9) ----

    def _invalidations(self, security, onchain) -> list[str]:
        conditions = [
            "Any destructive security finding appears (honeypot, liquidity pull, contract change)",
            "LP unlock, removal, or a large developer/insider sell",
            "Holder count declines while volume stays high (distribution into exits)",
            "Volume-quality flags wash trading",
            "Community activity collapses or turns artificial",
        ]
        for finding in security.findings:
            if finding.severity is RiskTier.SERIOUS_WARNING:
                conditions.append(f"Worsening of: {finding.message}")
        if onchain is not None and onchain.phase is MarketPhase.DISTRIBUTION:
            conditions.append("Confirmed distribution phase continues (sellers dominating)")
        return conditions[:10]

    # ---- Entry reason ----

    @staticmethod
    def _entry_reason(setup, security, onchain, conviction) -> str:
        if conviction is ConvictionLevel.NO_TRADE:
            return "Do not enter: destructive risk present. " + (
                security.destructive_findings[0].message if security.destructive_findings else ""
            )
        phase = onchain.phase.value if onchain else "unknown phase"
        if setup is SetupType.WATCH_ONLY:
            return (
                f"Monitor only: current phase ({phase}) and structure do not support entry. "
                "Re-evaluate if accumulation resumes, holder growth returns, or a new catalyst appears."
            )
        return (
            f"{setup.value.replace('_', ' ').title()} setup in {phase}; "
            f"security {security.overall_score:.0f}/100 ({security.band}). "
            "Enter only after required confirmations; scale in, never all at once."
        )
