"""Token structure & market-structure analysis engine (Spec Part 7).

Evaluates whether a token's *economic structure* can support growth:
valuation stage, dilution overhang, liquidity depth relative to size,
volume sustainability, supply concentration, and competitive position.

Doctrine (Part 7 final rule): a strong meme still fails on a weak
structure — narrative creates attention, but token structure determines
whether attention can become sustainable value. And a token is never
judged by how much it has already pumped.

Competition and catalyst analysis are partly qualitative (Part 7 Sections
9/11); they accept scores from the AI reasoning layer, plus a data-driven
helper that ranks a token among supplied competitor pairs.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Sequence

from meme_intelligence.analyzers.common import (
    Finding,
    SubScore,
    confidence_from_facts,
    scale,
    scale_inverted,
)
from meme_intelligence.config.settings import TokenSubWeights, TokenThresholds
from meme_intelligence.core.enums import (
    ConfidenceLevel,
    MarketCapStage,
    RiskTier,
    ValuationClassification,
)
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity

# Token rating bands (Part 7, Section 14).
_RATING_BANDS = (
    (90.0, "Excellent"),
    (75.0, "Good"),
    (50.0, "Average"),
    (25.0, "Risky"),
    (0.0, "Avoid"),
)

# Valuation-upside signals per stage: early caps have the most structural
# room to expand (Part 7 Section 2 — higher upside, higher risk).
_STAGE_SIGNALS = {
    MarketCapStage.EARLY: 85.0,
    MarketCapStage.GROWTH: 65.0,
    MarketCapStage.MATURE: 40.0,
}

# Concentration anchors shared with the on-chain lens (Part 7 Section 7).
_TOP10_BEST, _TOP10_WORST = 20.0, 70.0


@dataclass(frozen=True)
class TokenAssessment:
    """Token structure verdict (report format per Part 7, Section 14)."""

    token: TokenIdentity
    sub_scores: dict[str, float | None]
    overall_score: float
    rating: str
    stage: MarketCapStage
    valuation: ValuationClassification
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float

    def summary(self) -> str:
        lines = [
            f"Token structure: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100 [{self.rating}]  "
            f"stage={self.stage.value}  valuation={self.valuation.value}  "
            f"confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(f"  NOTE: only {self.coverage:.0%} of token categories have data")
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>12}: {rendered}")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        return "\n".join(lines)


def competition_score(pair: DexPair, competitors: Sequence[DexPair]) -> float | None:
    """Rank a token among competitor pairs by liquidity + volume (Part 7, Section 9).

    Returns the token's percentile (0-100) among the field, or ``None`` when
    no competitors are supplied — an empty comparison is unknown, not a win.
    """
    if not competitors:
        return None

    def strength(p: DexPair) -> float:
        return (p.liquidity_usd or 0.0) + (p.volume_24h or 0.0)

    own = strength(pair)
    beaten = sum(1 for c in competitors if strength(c) < own)
    return 100.0 * beaten / len(competitors)


class TokenAnalyzer:
    """Scores token structure per the Part 7 framework."""

    def __init__(self, thresholds: TokenThresholds, weights: TokenSubWeights):
        self._t = thresholds
        self._w = weights
        self._logger = get_logger("analyzers.token")

    def assess(
        self,
        pair: DexPair,
        security: SecurityProfile | None = None,
        *,
        competition: float | None = None,
        catalysts: float | None = None,
    ) -> TokenAssessment:
        """``competition``/``catalysts`` are 0-100 judgments (AI layer or
        :func:`competition_score`); ``security`` supplies supply-concentration facts."""
        for name, value in (("competition", competition), ("catalysts", catalysts)):
            if value is not None and not (0.0 <= value <= 100.0):
                raise ValueError(f"{name} score must be within 0-100, got {value}")

        stage = self._classify_stage(pair)
        parts = [
            self._assess_valuation(pair, stage),
            self._assess_liquidity(pair),
            self._assess_supply(pair, security),
            self._assess_volume(pair),
            self._qualitative("competition", competition),
            self._qualitative("catalysts", catalysts),
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
                f"no token structure data available for {pair.base_token.address}"
            )

        overall = weighted_sum / available_weight
        valuation = self._classify_valuation(pair, stage)
        confidence = confidence_from_facts(sum(p.known_count for p in parts), len(unknowns))

        self._logger.info(
            "token assessment %s/%s: score=%.0f stage=%s valuation=%s",
            pair.chain, pair.base_token.address, overall, stage.value, valuation.value,
        )

        return TokenAssessment(
            token=pair.base_token,
            sub_scores=sub_scores,
            overall_score=overall,
            rating=self._rating(overall),
            stage=stage,
            valuation=valuation,
            confidence=confidence,
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=available_weight,
        )

    # ---- Stage & valuation (Part 7 Sections 2-3) ----

    def _effective_mcap(self, pair: DexPair) -> float | None:
        """Market cap, falling back to FDV when circulating cap is unreported.

        A reported cap of ``0`` (or negative) is treated as missing, not as a
        valid zero-cap anchor: otherwise the token stages EARLY and earns a
        full valuation signal on non-existent data (Rule 8)."""
        if pair.market_cap is not None and pair.market_cap > 0:
            return pair.market_cap
        if pair.fdv is not None and pair.fdv > 0:
            return pair.fdv
        return None

    def _classify_stage(self, pair: DexPair) -> MarketCapStage:
        mcap = self._effective_mcap(pair)
        if mcap is None:
            return MarketCapStage.UNKNOWN
        if mcap < self._t.early_stage_mcap_usd:
            return MarketCapStage.EARLY
        if mcap < self._t.mature_stage_mcap_usd:
            return MarketCapStage.GROWTH
        return MarketCapStage.MATURE

    def _fdv_ratio(self, pair: DexPair) -> float | None:
        if pair.fdv is None or pair.market_cap is None or pair.market_cap <= 0:
            return None
        return pair.fdv / pair.market_cap

    def _assess_valuation(self, pair: DexPair, stage: MarketCapStage) -> SubScore:
        s = SubScore("valuation")

        if s.observe("market_cap", self._effective_mcap(pair)):
            s.signal(_STAGE_SIGNALS[stage])

        if s.observe("fdv_ratio", self._fdv_ratio(pair)):
            ratio = self._fdv_ratio(pair)
            if ratio >= self._t.fdv_dilution_severe_ratio:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         f"FDV is {ratio:.1f}x market cap: large future supply overhang")
            elif ratio >= self._t.fdv_dilution_warn_ratio:
                s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"FDV is {ratio:.1f}x market cap: some future dilution")

        return s

    # ---- Liquidity structure (Part 7 Section 4) ----

    def _assess_liquidity(self, pair: DexPair) -> SubScore:
        s = SubScore("liquidity")
        mcap = self._effective_mcap(pair)

        ratio_percent = None
        if pair.liquidity_usd is not None and mcap is not None and mcap > 0:
            ratio_percent = 100.0 * pair.liquidity_usd / mcap
        if s.observe("liquidity_to_mcap_percent", ratio_percent):
            s.signal(scale(ratio_percent, self._t.low_liquidity_to_mcap_percent,
                           self._t.healthy_liquidity_to_mcap_percent))
            if ratio_percent < self._t.low_liquidity_to_mcap_percent:
                s.deduct(25, RiskTier.SERIOUS_WARNING,
                         f"liquidity is only {ratio_percent:.2f}% of market cap: "
                         "small trades can move price violently")

        return s

    # ---- Supply structure (Part 7 Sections 6-7) ----

    def _assess_supply(self, pair: DexPair, security: SecurityProfile | None) -> SubScore:
        s = SubScore("supply")

        circulating = None
        ratio = self._fdv_ratio(pair)
        if ratio is not None and ratio > 0:
            circulating = 1.0 / ratio  # mcap / fdv
        if s.observe("circulating_fraction", circulating):
            s.signal(scale(circulating, self._t.min_circulating_fraction,
                           self._t.healthy_circulating_fraction))

        top10 = security.top10_holder_percent if security else None
        if s.observe("top10_holder_percent", top10):
            s.signal(scale_inverted(top10, _TOP10_BEST, _TOP10_WORST))

        return s

    # ---- Volume structure (Part 7 Section 5) ----

    def _assess_volume(self, pair: DexPair) -> SubScore:
        s = SubScore("volume")
        mcap = self._effective_mcap(pair)

        ratio_percent = None
        if pair.volume_24h is not None and mcap is not None and mcap > 0:
            ratio_percent = 100.0 * pair.volume_24h / mcap
        if s.observe("volume_to_mcap_percent", ratio_percent):
            if ratio_percent >= self._t.excessive_volume_to_mcap_percent:
                s.signal(30.0)
                s.deduct(20, RiskTier.SERIOUS_WARNING,
                         f"24h volume is {ratio_percent:.0f}% of market cap: "
                         "churn this extreme is rarely organic")
            else:
                s.signal(scale(ratio_percent, self._t.min_volume_to_mcap_percent,
                               self._t.target_volume_to_mcap_percent))

        return s

    # ---- Qualitative slots (Part 7 Sections 9/11) ----

    @staticmethod
    def _qualitative(category: str, value: float | None) -> SubScore:
        s = SubScore(category)
        if s.observe(category, value):
            s.signal(value)
        return s

    # ---- Valuation classification (Part 7 Section 14) ----

    def _classify_valuation(self, pair: DexPair, stage: MarketCapStage) -> ValuationClassification:
        """Rule-based structural assessment — deliberately conservative:
        UNDERVALUED requires multiple positive structural signals together."""
        mcap = self._effective_mcap(pair)
        if mcap is None:
            return ValuationClassification.UNKNOWN

        fdv_ratio = self._fdv_ratio(pair)
        if fdv_ratio is not None and fdv_ratio >= self._t.fdv_dilution_severe_ratio:
            return ValuationClassification.OVERVALUED

        volume_ratio = None
        if pair.volume_24h is not None and mcap > 0:
            volume_ratio = 100.0 * pair.volume_24h / mcap
        liquidity_ratio = None
        if pair.liquidity_usd is not None and mcap > 0:
            liquidity_ratio = 100.0 * pair.liquidity_usd / mcap

        if stage is MarketCapStage.MATURE and volume_ratio is not None and volume_ratio < 2.0:
            return ValuationClassification.EXPENSIVE  # size without matching demand

        if (
            stage is MarketCapStage.EARLY
            and volume_ratio is not None
            and volume_ratio >= self._t.target_volume_to_mcap_percent
            and volume_ratio < self._t.excessive_volume_to_mcap_percent
            and liquidity_ratio is not None
            and liquidity_ratio >= self._t.healthy_liquidity_to_mcap_percent
        ):
            return ValuationClassification.UNDERVALUED  # early + real demand + real depth

        return ValuationClassification.FAIRLY_VALUED

    @staticmethod
    def _rating(score: float) -> str:
        for minimum, label in _RATING_BANDS:
            if score >= minimum:
                return label
        return _RATING_BANDS[-1][1]
