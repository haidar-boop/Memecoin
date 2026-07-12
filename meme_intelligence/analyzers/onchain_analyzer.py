"""On-chain intelligence engine (Spec Part 6).

Turns a normalized :class:`OnChainProfile` into a scored
:class:`OnChainAssessment` across the six Part 6 Section 15 categories:
holder health, smart money, whale behavior, developer activity, volume
quality, and token flow — plus the accumulation/distribution phase
classification (Section 14).

Data reality (Rule 8): holder structure, developer wallet, and trading
behavior are already derivable from the market + security collectors via
:func:`derive_onchain_profile`. Smart-money, whale-movement, and
exchange-flow analysis require the wallet-intelligence collectors
(Helius/Birdeye — Part 17); until those land their sub-scores report
"no data" and shrink coverage rather than pretending to a full picture.

Volume-quality doctrine (Part 6 Sections 11-12): high volume is NOT
automatically demand. Volume is checked against unique traders (repeated
wallets churning = wash-trading signature) and against the holder base
(huge volume atop a tiny holder base = artificial activity).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.common import (
    Finding,
    SubScore,
    confidence_from_facts,
    scale,
    scale_inverted,
)
from meme_intelligence.config.settings import OnChainSubWeights, OnChainThresholds
from meme_intelligence.core.enums import ConfidenceLevel, MarketPhase, RiskTier
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import (
    DexPair,
    OnChainProfile,
    SecurityProfile,
    TokenIdentity,
)

# Holder-concentration anchors: top holder <=3% is ideal, >=20% scores zero;
# top-10 <=20% ideal, >=70% zero (Part 6 Section 2). The hard risk gates for
# concentration live in the security analyzer; these grade quality.
_TOP_HOLDER_BEST, _TOP_HOLDER_WORST = 3.0, 20.0
_TOP10_BEST, _TOP10_WORST = 20.0, 70.0
_CREATOR_BEST, _CREATOR_WORST = 1.0, 10.0

# Phase-classification anchors (Part 6 Section 14).
_PHASE_FLAT_BAND_PERCENT = 10.0   # |24h change| below this = consolidation
_PHASE_EXPANSION_BUY_RATIO = 0.55


@dataclass(frozen=True)
class OnChainAssessment:
    """On-chain verdict for one token (report format per Part 6, Section 16)."""

    token: TokenIdentity
    source: str
    sub_scores: dict[str, float | None]
    overall_score: float
    phase: MarketPhase
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float

    def summary(self) -> str:
        lines = [
            f"On-chain assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100  phase={self.phase.value}  "
            f"confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(
                f"  NOTE: only {self.coverage:.0%} of on-chain categories have data; "
                "unmeasured categories are excluded, not assumed"
            )
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>18}: {rendered}")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        return "\n".join(lines)


def derive_onchain_profile(
    pair: DexPair,
    security: SecurityProfile | None = None,
    *,
    holder_count_24h_ago: int | None = None,
) -> OnChainProfile:
    """Build a partial on-chain profile from already-collected data (Rule 9).

    Market pairs contribute trading behavior; the security profile
    contributes holder structure and developer wallet facts. Fields no
    current collector reports remain ``None``.
    """
    return OnChainProfile(
        token=pair.base_token,
        source="derived:market+security" if security else "derived:market",
        holder_count=security.holder_count if security else None,
        holder_count_24h_ago=holder_count_24h_ago,
        top_holder_percent=security.top_holder_percent if security else None,
        top10_holder_percent=security.top10_holder_percent if security else None,
        creator_percent=security.creator_percent if security else None,
        owner_percent=security.owner_percent if security else None,
        buys_24h=pair.buys_24h,
        sells_24h=pair.sells_24h,
        unique_buyers_24h=pair.buyers_24h,
        unique_sellers_24h=pair.sellers_24h,
        volume_24h_usd=pair.volume_24h,
        liquidity_usd=pair.liquidity_usd,
        price_change_24h_percent=pair.price_change_24h,
    )


class OnChainAnalyzer:
    """Scores on-chain behavior per the Part 6 framework."""

    def __init__(self, thresholds: OnChainThresholds, weights: OnChainSubWeights):
        self._t = thresholds
        self._w = weights
        self._logger = get_logger("analyzers.onchain")

    def assess(self, profile: OnChainProfile) -> OnChainAssessment:
        parts = [
            self._assess_holder_health(profile),
            self._assess_smart_money(profile),
            self._assess_whale_behavior(profile),
            self._assess_developer_activity(profile),
            self._assess_volume_quality(profile),
            self._assess_token_flow(profile),
        ]
        weight_map = dataclasses.asdict(self._w)

        sub_scores: dict[str, float | None] = {}
        findings: list[Finding] = []
        unknowns: list[str] = []
        weighted_sum = 0.0
        available_weight = 0.0
        for part in parts:
            score = part.score()
            sub_scores[part.category] = score
            findings.extend(part.findings)
            unknowns.extend(part.unknowns)
            if score is not None:
                weighted_sum += score * weight_map[part.category]
                available_weight += weight_map[part.category]

        if available_weight == 0.0:
            raise InsufficientDataError(
                f"no on-chain data available for {profile.token.address} on {profile.token.chain}"
            )

        overall = weighted_sum / available_weight
        phase = self._classify_phase(profile)
        confidence = confidence_from_facts(sum(p.known_count for p in parts), len(unknowns))

        self._logger.info(
            "on-chain assessment %s/%s: score=%.0f phase=%s findings=%d unknown=%d",
            profile.token.chain, profile.token.address, overall,
            phase.value, len(findings), len(unknowns),
        )

        return OnChainAssessment(
            token=profile.token,
            source=profile.source,
            sub_scores=sub_scores,
            overall_score=overall,
            phase=phase,
            confidence=confidence,
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=available_weight,
        )

    # ---- Holder health: distribution + growth (Part 6 Sections 2-3) ----

    def _assess_holder_health(self, p: OnChainProfile) -> SubScore:
        s = SubScore("holder_health")

        if s.observe("holder_count", p.holder_count):
            s.signal(scale(p.holder_count, self._t.min_holder_count, self._t.target_holder_count))
            if p.holder_count_24h_ago is not None and p.holder_count_24h_ago > 0:
                growth = 100.0 * (p.holder_count - p.holder_count_24h_ago) / p.holder_count_24h_ago
                if growth < 0:
                    s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                             f"holder count shrank {abs(growth):.1f}% in 24h")
                else:
                    # Flat growth is neutral, not a demerit; only growth ABOVE
                    # flat earns a reward. Anchoring the scale symmetrically at
                    # +/- the target maps 0% growth to a neutral 50, so a token
                    # that merely held its holder base cannot score below one
                    # that actually LOST holders (penalized in the branch above).
                    s.signal(scale(growth, -self._t.holder_growth_target_percent_24h,
                                   self._t.holder_growth_target_percent_24h))

        if s.observe("top_holder_percent", p.top_holder_percent):
            s.signal(scale_inverted(p.top_holder_percent, _TOP_HOLDER_BEST, _TOP_HOLDER_WORST))
        if s.observe("top10_holder_percent", p.top10_holder_percent):
            s.signal(scale_inverted(p.top10_holder_percent, _TOP10_BEST, _TOP10_WORST))

        return s

    # ---- Smart money: requires wallet intelligence (Part 6 Sections 6-7) ----

    def _assess_smart_money(self, p: OnChainProfile) -> SubScore:
        s = SubScore("smart_money")
        if s.observe("smart_wallet_count", p.smart_wallet_count):
            # One wallet is not a signal (Part 6 Section 7) — scale to several.
            s.signal(scale(p.smart_wallet_count, 0.0, 5.0))
        if s.observe("smart_wallet_net_flow_usd", p.smart_wallet_net_flow_usd):
            if p.smart_wallet_net_flow_usd < 0:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         "smart-money wallets are net sellers")
            else:
                s.signal(100.0)
        return s

    # ---- Whale behavior: requires wallet intelligence (Part 6 Section 8) ----

    def _assess_whale_behavior(self, p: OnChainProfile) -> SubScore:
        s = SubScore("whale_behavior")
        if s.observe("whale_net_flow_usd", p.whale_net_flow_usd):
            if p.whale_net_flow_usd < 0:
                s.deduct(30, RiskTier.SERIOUS_WARNING, "whales are net distributing")
            else:
                s.signal(100.0)
        return s

    # ---- Developer wallet (Part 6 Section 5) ----

    def _assess_developer_activity(self, p: OnChainProfile) -> SubScore:
        s = SubScore("developer_activity")
        if s.observe("creator_percent", p.creator_percent):
            s.signal(scale_inverted(p.creator_percent, _CREATOR_BEST, _CREATOR_WORST))
        if s.observe("owner_percent", p.owner_percent):
            s.signal(scale_inverted(p.owner_percent, _CREATOR_BEST, _CREATOR_WORST))
        return s

    # ---- Volume quality (Part 6 Sections 11-12) ----

    def _assess_volume_quality(self, p: OnChainProfile) -> SubScore:
        s = SubScore("volume_quality")

        txns = None
        if p.buys_24h is not None or p.sells_24h is not None:
            txns = (p.buys_24h or 0) + (p.sells_24h or 0)

        traders = None
        if p.unique_buyers_24h is not None or p.unique_sellers_24h is not None:
            # Buyers and sellers overlap (one wallet can do both), so their sum
            # over-counts unique traders and deflates trades-per-trader, hiding
            # wash trading. The true union is unknown; max() is a conservative
            # lower bound on unique traders that never under-detects churn.
            traders = max(p.unique_buyers_24h or 0, p.unique_sellers_24h or 0)

        # Wash-trading signature: many trades from few wallets (Section 12).
        if s.observe("trades_per_trader", None if txns is None or not traders else txns / traders):
            ratio = txns / traders
            s.signal(scale_inverted(ratio, self._t.healthy_trades_per_trader,
                                    self._t.wash_trades_per_trader))
            if ratio >= self._t.wash_trades_per_trader:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         f"{ratio:.1f} trades per unique wallet: possible wash trading")

        # Volume disproportionate to the holder base = artificial activity.
        if s.observe(
            "volume_per_holder",
            None if p.volume_24h_usd is None or not p.holder_count
            else p.volume_24h_usd / p.holder_count,
        ):
            per_holder = p.volume_24h_usd / p.holder_count
            s.signal(scale_inverted(per_holder, self._t.volume_per_holder_healthy_usd,
                                    self._t.volume_per_holder_suspicious_usd))
            if per_holder >= self._t.volume_per_holder_suspicious_usd:
                s.deduct(25, RiskTier.SERIOUS_WARNING,
                         f"${per_holder:,.0f} 24h volume per holder: "
                         "volume without matching holder base")

        # Buy/sell balance (Section 11).
        if s.observe("buy_ratio", self._buy_ratio(p)):
            ratio = self._buy_ratio(p)
            if ratio < self._t.buy_ratio_weak:
                s.signal(30.0)
                s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"heavy selling pressure ({ratio:.0%} of trades are buys)")
            elif ratio > 0.85:
                s.signal(60.0)  # one-sided buying can mean bundled/coordinated entries
            elif ratio >= self._t.buy_ratio_strong:
                s.signal(100.0)
            else:
                s.signal(scale(ratio, self._t.buy_ratio_weak, self._t.buy_ratio_strong))

        return s

    # ---- Token flow: requires exchange-flow data (Part 6 Sections 10, 13) ----

    def _assess_token_flow(self, p: OnChainProfile) -> SubScore:
        s = SubScore("token_flow")
        known_in = s.observe("exchange_inflow_usd", p.exchange_inflow_usd)
        known_out = s.observe("exchange_outflow_usd", p.exchange_outflow_usd)
        if known_in and known_out:
            net = p.exchange_outflow_usd - p.exchange_inflow_usd
            if net >= 0:
                s.signal(100.0)  # net withdrawal to private wallets = holding intent
            else:
                s.signal(40.0)
                s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         "net token flow toward exchanges suggests selling intent")
        return s

    # ---- Phase classification (Part 6 Section 14) ----

    def _classify_phase(self, p: OnChainProfile) -> MarketPhase:
        change = p.price_change_24h_percent
        buy_ratio = self._buy_ratio(p)
        if change is None or buy_ratio is None:
            return MarketPhase.UNCLEAR

        if change > _PHASE_FLAT_BAND_PERCENT and buy_ratio >= _PHASE_EXPANSION_BUY_RATIO:
            return MarketPhase.EXPANSION
        if abs(change) <= _PHASE_FLAT_BAND_PERCENT and buy_ratio >= 0.5:
            return MarketPhase.ACCUMULATION
        if buy_ratio < 0.5 and change <= 0:
            return MarketPhase.DISTRIBUTION
        return MarketPhase.UNCLEAR

    @staticmethod
    def _buy_ratio(p: OnChainProfile) -> float | None:
        if p.buys_24h is None and p.sells_24h is None:
            return None
        total = (p.buys_24h or 0) + (p.sells_24h or 0)
        if total == 0:
            return None
        return (p.buys_24h or 0) / total
