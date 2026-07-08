"""Risk management engine (Spec Part 9, informed by Part 25's risk doctrine).

Produces the token-level :class:`RiskAssessment` — a 0-100 risk score where
**higher means riskier** (the opposite direction of the quality scores),
weighted per Part 9 Section 6: security 25%, market 20%, community 15%,
token 20%, execution 20%.

Risk-taxonomy discipline (Part 31 Consistency Lock):

* Normal early-stage uncertainty contributes *moderate* risk, not a veto.
* Destructive findings pin the relevant component at maximum.
* A token whose risk profile is mostly *unverifiable* can never be
  classified better than HIGH risk — unknown is not safe (Rule 8), even
  though unknown components are excluded from the weighted number itself.

Portfolio-level exposure checks and drawdown posture (Part 9 Sections
2/5/8-9) live in :class:`PortfolioRiskManager`. Emergency exit conditions
(Section 12) are detected by :func:`emergency_flags`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.common import scale, scale_inverted
from meme_intelligence.analyzers.onchain_analyzer import OnChainAssessment
from meme_intelligence.analyzers.security_analyzer import SecurityAssessment
from meme_intelligence.analyzers.token_analyzer import TokenAssessment
from meme_intelligence.analyzers.community_analyzer import CommunityAssessment
from meme_intelligence.config.settings import RiskSettings, RiskSubWeights
from meme_intelligence.core.enums import (
    MarketRegime,
    RiskCategory,
    RiskPosture,
    RiskTier,
)
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair, TokenIdentity

# Risk category bands over the 0-100 risk score (Part 9, Section 7).
_CATEGORY_BANDS = (
    (75.0, RiskCategory.EXTREME),
    (50.0, RiskCategory.HIGH),
    (25.0, RiskCategory.MODERATE),
    (0.0, RiskCategory.LOW_RELATIVE),
)

# Below this coverage the profile is mostly unverifiable => category floor HIGH.
_MIN_COVERAGE_FOR_LOW_RISK = 0.5

# Market-risk anchors: 24h swings and liquidity depth as volatility proxies.
_VOLATILITY_CALM_PERCENT = 10.0
_VOLATILITY_WILD_PERCENT = 80.0
_EXIT_EASY_LIQUIDITY_USD = 100_000.0
_EXIT_HARD_LIQUIDITY_USD = 5_000.0
_REGIME_RISK_BONUS = {MarketRegime.BEAR: 20.0, MarketRegime.UNKNOWN: 10.0}


@dataclass(frozen=True)
class RiskAssessment:
    """Token-level risk verdict (report format per Part 9, Section 14)."""

    token: TokenIdentity
    components: dict[str, float | None]  # each 0-100, higher = riskier
    risk_score: float
    category: RiskCategory
    coverage: float
    main_risks: tuple[str, ...]
    mitigations: tuple[str, ...]

    def summary(self) -> str:
        lines = [
            f"Risk assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Risk score: {self.risk_score:.0f}/100 (higher = riskier)  "
            f"category={self.category.value}  coverage={self.coverage:.0%}",
        ]
        for name, value in self.components.items():
            rendered = f"{value:.0f}/100" if value is not None else "unverifiable"
            lines.append(f"  {name:>10} risk: {rendered}")
        if self.main_risks:
            lines.append("  Main risks:")
            for risk in self.main_risks:
                lines.append(f"    - {risk}")
        if self.mitigations:
            lines.append("  Mitigations:")
            for m in self.mitigations:
                lines.append(f"    - {m}")
        return "\n".join(lines)


class RiskAnalyzer:
    """Combines the analysis engines' outputs into one risk profile (Part 9)."""

    def __init__(self, weights: RiskSubWeights):
        self._w = weights
        self._logger = get_logger("analyzers.risk")

    def assess(
        self,
        security: SecurityAssessment,
        *,
        pair: DexPair | None = None,
        community: CommunityAssessment | None = None,
        token: TokenAssessment | None = None,
        onchain: OnChainAssessment | None = None,
        regime: MarketRegime = MarketRegime.UNKNOWN,
    ) -> RiskAssessment:
        components: dict[str, float | None] = {
            "security": self._security_risk(security),
            "market": self._market_risk(pair, regime),
            "community": self._community_risk(community),
            "token": self._token_risk(token),
            "execution": self._execution_risk(pair, onchain),
        }

        weight_map = dataclasses.asdict(self._w)
        weighted_sum = 0.0
        available = 0.0
        for name, value in components.items():
            if value is not None:
                weighted_sum += value * weight_map[name]
                available += weight_map[name]
        if available == 0.0:
            raise InsufficientDataError(
                f"no risk data available for {security.token.address}"
            )

        risk_score = weighted_sum / available
        category = self._category(risk_score)

        # Unknown is not safe: a mostly-unverifiable profile cannot be
        # certified low-risk, whatever its known components say.
        if available < _MIN_COVERAGE_FOR_LOW_RISK and category in (
            RiskCategory.LOW_RELATIVE, RiskCategory.MODERATE,
        ):
            category = RiskCategory.HIGH

        main_risks = self._main_risks(security, community, components)
        self._logger.info(
            "risk assessment %s/%s: score=%.0f category=%s coverage=%.0f%%",
            security.token.chain, security.token.address,
            risk_score, category.value, available * 100,
        )

        return RiskAssessment(
            token=security.token,
            components=components,
            risk_score=risk_score,
            category=category,
            coverage=available,
            main_risks=tuple(main_risks),
            mitigations=self._mitigations(components, category),
        )

    # ---- Components (0-100, higher = riskier) ----

    @staticmethod
    def _security_risk(security: SecurityAssessment) -> float:
        if security.is_destructive:
            return 100.0
        return 100.0 - security.overall_score

    @staticmethod
    def _market_risk(pair: DexPair | None, regime: MarketRegime) -> float | None:
        if pair is None:
            return None
        signals: list[float] = []
        if pair.price_change_24h is not None:
            signals.append(scale(abs(pair.price_change_24h),
                                 _VOLATILITY_CALM_PERCENT, _VOLATILITY_WILD_PERCENT))
        if pair.liquidity_usd is not None:
            signals.append(scale_inverted(pair.liquidity_usd,
                                          _EXIT_HARD_LIQUIDITY_USD, _EXIT_EASY_LIQUIDITY_USD))
        if not signals:
            return None
        risk = sum(signals) / len(signals) + _REGIME_RISK_BONUS.get(regime, 0.0)
        return min(100.0, risk)

    @staticmethod
    def _community_risk(community: CommunityAssessment | None) -> float | None:
        if community is None:
            return None
        if community.is_artificial:
            return 100.0
        return 100.0 - community.overall_score

    @staticmethod
    def _token_risk(token: TokenAssessment | None) -> float | None:
        if token is None:
            return None
        return 100.0 - token.overall_score

    @staticmethod
    def _execution_risk(pair: DexPair | None, onchain: OnChainAssessment | None) -> float | None:
        """How hard would it be to exit this position cleanly?"""
        signals: list[float] = []
        if pair is not None and pair.liquidity_usd is not None:
            signals.append(scale_inverted(pair.liquidity_usd,
                                          _EXIT_HARD_LIQUIDITY_USD, _EXIT_EASY_LIQUIDITY_USD))
        if onchain is not None:
            volume_quality = onchain.sub_scores.get("volume_quality")
            if volume_quality is not None:
                signals.append(100.0 - volume_quality)  # fake volume = fake exit liquidity
        if not signals:
            return None
        return sum(signals) / len(signals)

    # ---- Verdict helpers ----

    @staticmethod
    def _category(risk_score: float) -> RiskCategory:
        for minimum, category in _CATEGORY_BANDS:
            if risk_score >= minimum:
                return category
        return RiskCategory.LOW_RELATIVE

    @staticmethod
    def _main_risks(security, community, components) -> list[str]:
        risks: list[str] = []
        for finding in security.findings:
            if finding.severity in (RiskTier.DESTRUCTIVE, RiskTier.SERIOUS_WARNING):
                risks.append(finding.message)
        if community is not None and community.is_artificial:
            risks.append("community engagement is artificial")
        for name, value in components.items():
            if value is None:
                risks.append(f"{name} risk is unverifiable with current data")
        return risks[:8]

    @staticmethod
    def _mitigations(components, category) -> tuple[str, ...]:
        tips = [
            "Size the position to survive a total loss (Part 9 Rule 2).",
            "Define the maximum acceptable loss before entry and never move it.",
        ]
        if category in (RiskCategory.HIGH, RiskCategory.EXTREME):
            tips.append("Treat as speculative-allocation only; do not average down.")
        if any(v is None for v in components.values()):
            tips.append("Close the data gaps above before increasing exposure.")
        return tuple(tips)


def emergency_flags(
    security: SecurityAssessment,
    onchain: OnChainAssessment | None = None,
) -> tuple[list[str], list[str]]:
    """Detect emergency exit conditions (Part 9, Section 12).

    Returns ``(critical, high)`` — critical demands immediate reassessment
    (liquidity pulled, honeypot, developer dumping); high demands prompt
    review (whale distribution, serious deterioration).
    """
    critical = [f"CRITICAL: {f.message}" for f in security.destructive_findings]
    high: list[str] = []
    for finding in security.findings:
        if finding.severity is RiskTier.SERIOUS_WARNING and finding.category == "liquidity":
            high.append(f"HIGH: {finding.message}")
    if onchain is not None:
        for finding in onchain.findings:
            if finding.severity is RiskTier.SERIOUS_WARNING:
                high.append(f"HIGH: {finding.message}")
    return critical, high


@dataclass(frozen=True)
class Position:
    """One open position for portfolio exposure checks (Part 9, Section 5)."""

    token_symbol: str
    chain: str
    portfolio_percent: float
    narrative: str | None = None


class PortfolioRiskManager:
    """Portfolio-level exposure limits and drawdown posture (Part 9)."""

    def __init__(self, settings: RiskSettings):
        self._s = settings
        self._logger = get_logger("trading.portfolio_risk")

    def exposure_warnings(self, positions: list[Position]) -> list[str]:
        """Check open positions against the concentration limits (Section 5)."""
        warnings: list[str] = []
        if len(positions) > self._s.max_open_positions:
            warnings.append(
                f"{len(positions)} open positions exceeds the "
                f"{self._s.max_open_positions}-position limit: conviction is being diluted"
            )

        total = sum(p.portfolio_percent for p in positions)
        if total > self._s.max_total_exposure_percent:
            warnings.append(
                f"total exposure {total:.1f}% exceeds {self._s.max_total_exposure_percent:.0f}%: "
                "cash reserve is below plan (Part 9, Section 2)"
            )

        for position in positions:
            if position.portfolio_percent > self._s.max_single_position_percent:
                warnings.append(
                    f"{position.token_symbol} is {position.portfolio_percent:.1f}% of portfolio "
                    f"(limit {self._s.max_single_position_percent:.0f}%)"
                )

        by_chain: dict[str, float] = {}
        by_narrative: dict[str, float] = {}
        for position in positions:
            by_chain[position.chain] = by_chain.get(position.chain, 0.0) + position.portfolio_percent
            if position.narrative:
                by_narrative[position.narrative] = (
                    by_narrative.get(position.narrative, 0.0) + position.portfolio_percent
                )
        for chain, pct in by_chain.items():
            if pct > self._s.max_chain_concentration_percent:
                warnings.append(f"chain '{chain}' holds {pct:.1f}% of portfolio "
                                f"(limit {self._s.max_chain_concentration_percent:.0f}%)")
        for narrative, pct in by_narrative.items():
            if pct > self._s.max_narrative_concentration_percent:
                warnings.append(f"narrative '{narrative}' holds {pct:.1f}% of portfolio "
                                f"(limit {self._s.max_narrative_concentration_percent:.0f}%)")

        for warning in warnings:
            self._logger.warning("portfolio exposure: %s", warning)
        return warnings

    def drawdown_posture(
        self,
        daily_loss_percent: float = 0.0,
        weekly_loss_percent: float = 0.0,
    ) -> tuple[RiskPosture, str]:
        """Map recent losses to an operating posture (Sections 8-9).

        Loss arguments are positive numbers (5.0 = down 5%).
        """
        if (daily_loss_percent >= self._s.defensive_daily_loss_percent
                or weekly_loss_percent >= self._s.defensive_weekly_loss_percent):
            return (RiskPosture.DEFENSIVE,
                    "Capital preservation mode: no new positions; review whether rules "
                    "were followed before trading again (never revenge trade).")
        if (daily_loss_percent >= self._s.reduced_daily_loss_percent
                or weekly_loss_percent >= self._s.reduced_weekly_loss_percent):
            return (RiskPosture.REDUCED,
                    "Reduce position sizes and trade count; require stronger "
                    "confirmation before every entry.")
        return RiskPosture.NORMAL, "Normal operations; standard sizing rules apply."
