"""Momentum & market-timing engine (Spec Part 14, with Part 26's doctrine).

Scores momentum across the four Part 14 Section 5 lenses — price, volume,
social, on-chain — 25% each, and classifies the entry-timing zone
(Section 6) and preferred action (Section 14).

Doctrine encoded here:

* Price movement alone is not momentum (Part 26 Section 1): the price lens
  rewards *consistent* multi-window trends and penalizes vertical
  unsupported 1h spikes.
* Acceleration beats level (Part 26 Section 3): the volume and on-chain
  lenses compare the last hour's rate against the 24h baseline.
* Fake momentum is hunted (Part 26 Section 7): momentum built on suspect
  volume quality is deducted, and one-sided data stays unknown.
* Technical analysis is a supporting tool (Part 14 final rule): this score
  fills the master framework's momentum category (15%) — it never
  overrides security or foundation evidence.

Social momentum needs the social collectors (API keys); until then that
lens reports "no data" and coverage shrinks honestly.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from meme_intelligence.analyzers.common import (
    Finding,
    SubScore,
    confidence_from_facts,
    scale,
)
from meme_intelligence.analyzers.onchain_analyzer import OnChainAssessment
from meme_intelligence.config.settings import MomentumSubWeights, MomentumThresholds
from meme_intelligence.core.enums import (
    ConfidenceLevel,
    EntryZone,
    PreferredAction,
    RiskTier,
)
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair, TokenIdentity

# Price-trend base signal: 40 at flat, +1 point per +1% 24h change, clamped.
_TREND_BASE_SIGNAL = 40.0

# Multi-window consistency signals.
_CONSISTENT_TREND_SIGNAL = 90.0   # 1h, 6h, and 24h all positive
_FADING_TREND_SIGNAL = 35.0       # 24h up but the last hour is red
_MIXED_TREND_SIGNAL = 60.0

# Buy-pressure shift signals (1h buy ratio vs 24h baseline).
_BUYERS_INCREASING_SIGNAL = 85.0
_BUYERS_STEADY_SIGNAL = 60.0
_BUYERS_FADING_SIGNAL = 35.0

_SUSPECT_VOLUME_QUALITY_BELOW = 40.0

_ACTION_CONSIDER_MIN = 70.0
_ACTION_WAIT_MIN = 45.0
_ACTION_AVOID_BELOW = 30.0


@dataclass(frozen=True)
class MomentumAssessment:
    """Momentum verdict for one token (report format per Part 14, Section 14)."""

    token: TokenIdentity
    sub_scores: dict[str, float | None]
    overall_score: float
    entry_zone: EntryZone
    preferred_action: PreferredAction
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float

    def summary(self) -> str:
        lines = [
            f"Momentum assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100  zone={self.entry_zone.value}  "
            f"action={self.preferred_action.value}  confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(f"  NOTE: only {self.coverage:.0%} of momentum lenses have data")
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>8}: {rendered}")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        return "\n".join(lines)


class MomentumAnalyzer:
    """Scores momentum per the Part 14 framework."""

    def __init__(
        self,
        thresholds: MomentumThresholds,
        weights: MomentumSubWeights,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._t = thresholds
        self._w = weights
        self._now = now_func
        self._logger = get_logger("analyzers.momentum")

    def assess(
        self,
        pair: DexPair,
        *,
        onchain: OnChainAssessment | None = None,
        social_growth_7d_percent: float | None = None,
        holder_growth_24h_percent: float | None = None,
    ) -> MomentumAssessment:
        parts = [
            self._assess_price(pair),
            self._assess_volume(pair),
            self._assess_social(social_growth_7d_percent),
            self._assess_onchain(pair, onchain, holder_growth_24h_percent),
        ]
        weight_map = dataclasses.asdict(self._w)

        sub_scores: dict[str, float | None] = {}
        findings: list[Finding] = []
        unknowns: list[str] = []
        weighted_sum = 0.0
        available = 0.0
        for part in parts:
            score = part.score()
            sub_scores[part.category] = score
            findings.extend(part.findings)
            unknowns.extend(part.unknowns)
            if score is not None:
                weighted_sum += score * weight_map[part.category]
                available += weight_map[part.category]

        if available == 0.0:
            raise InsufficientDataError(
                f"no momentum data available for {pair.base_token.address}"
            )

        overall = weighted_sum / available
        zone = self._entry_zone(pair, overall)
        action = self._preferred_action(overall, zone)
        confidence = confidence_from_facts(sum(p.known_count for p in parts), len(unknowns))

        self._logger.info(
            "momentum assessment %s/%s: score=%.0f zone=%s action=%s",
            pair.chain, pair.base_token.address, overall, zone.value, action.value,
        )

        return MomentumAssessment(
            token=pair.base_token,
            sub_scores=sub_scores,
            overall_score=overall,
            entry_zone=zone,
            preferred_action=action,
            confidence=confidence,
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=available,
        )

    # ---- Price lens (Part 14 Sections 1-2, Part 26 Category 1) ----

    def _assess_price(self, pair: DexPair) -> SubScore:
        s = SubScore("price")

        if s.observe("price_change_24h", pair.price_change_24h):
            s.signal(max(0.0, min(100.0, _TREND_BASE_SIGNAL + pair.price_change_24h)))

        consistency = self._trend_consistency(pair)
        if s.observe("trend_consistency", consistency):
            s.signal(consistency)

        if s.observe("price_change_1h", pair.price_change_1h):
            if pair.price_change_1h >= self._t.spike_1h_percent:
                s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"+{pair.price_change_1h:.0f}% in one hour: vertical moves often retrace")

        return s

    @staticmethod
    def _trend_consistency(pair: DexPair) -> float | None:
        changes = (pair.price_change_1h, pair.price_change_6h, pair.price_change_24h)
        if any(c is None for c in changes):
            return None
        h1, h6, h24 = changes
        if h1 >= 0 and h6 >= 0 and h24 >= 0:
            return _CONSISTENT_TREND_SIGNAL
        if h24 > 0 and h1 < 0:
            return _FADING_TREND_SIGNAL
        return _MIXED_TREND_SIGNAL

    # ---- Volume lens (Part 14 Section 3, Part 26 Category 2) ----

    def _assess_volume(self, pair: DexPair) -> SubScore:
        s = SubScore("volume")

        acceleration = None
        if pair.volume_1h is not None and pair.volume_24h:
            acceleration = (pair.volume_1h * 24.0) / pair.volume_24h
        if s.observe("volume_acceleration", acceleration):
            if acceleration >= self._t.volume_acceleration_ratio:
                s.signal(90.0)
            elif acceleration <= self._t.volume_fade_ratio:
                s.signal(30.0)
                s.deduct(0, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         "volume is fading versus the 24h baseline")
            else:
                s.signal(scale(acceleration, self._t.volume_fade_ratio,
                               self._t.volume_acceleration_ratio) * 0.6 + 30.0)

        return s

    # ---- Social lens (Part 14 Section 5; needs social collectors) ----

    def _assess_social(self, social_growth_7d_percent: float | None) -> SubScore:
        s = SubScore("social")
        if s.observe("social_growth_7d_percent", social_growth_7d_percent):
            s.signal(scale(social_growth_7d_percent, 0.0,
                           self._t.target_social_growth_7d_percent))
        return s

    # ---- On-chain lens (Part 14 Section 5, Part 26 Categories 3-4) ----

    def _assess_onchain(
        self,
        pair: DexPair,
        onchain: OnChainAssessment | None,
        holder_growth_24h_percent: float | None,
    ) -> SubScore:
        s = SubScore("onchain")

        shift = self._buy_ratio_shift(pair)
        if s.observe("buy_ratio_shift", shift):
            if shift >= self._t.buy_ratio_shift:
                s.signal(_BUYERS_INCREASING_SIGNAL)
            elif shift <= -self._t.buy_ratio_shift:
                s.signal(_BUYERS_FADING_SIGNAL)
            else:
                s.signal(_BUYERS_STEADY_SIGNAL)

        if s.observe("holder_growth_24h_percent", holder_growth_24h_percent):
            s.signal(scale(holder_growth_24h_percent, 0.0, 20.0))

        # Momentum built on suspect volume is fake momentum (Part 26, Section 7).
        volume_quality = onchain.sub_scores.get("volume_quality") if onchain else None
        if s.observe("volume_quality", volume_quality):
            if volume_quality < _SUSPECT_VOLUME_QUALITY_BELOW:
                s.deduct(25, RiskTier.SERIOUS_WARNING,
                         "momentum is built on suspect volume quality")
            else:
                s.signal(volume_quality)

        return s

    @staticmethod
    def _buy_ratio_shift(pair: DexPair) -> float | None:
        """1h buy ratio minus 24h buy ratio: positive = buyers accelerating."""
        def ratio(buys: int | None, sells: int | None) -> float | None:
            if buys is None and sells is None:
                return None
            total = (buys or 0) + (sells or 0)
            return (buys or 0) / total if total else None

        recent = ratio(pair.buys_1h, pair.sells_1h)
        baseline = ratio(pair.buys_24h, pair.sells_24h)
        if recent is None or baseline is None:
            return None
        return recent - baseline

    # ---- Entry zone & preferred action (Part 14 Sections 6/14) ----

    def _entry_zone(self, pair: DexPair, overall: float) -> EntryZone:
        change = pair.price_change_24h
        if change is not None and change >= self._t.late_extension_24h_percent:
            return EntryZone.LATE
        age_hours = None
        if pair.pair_created_at is not None:
            age_hours = (self._now() - pair.pair_created_at).total_seconds() / 3600.0
        if age_hours is not None and age_hours <= 24.0:
            return EntryZone.EARLY
        if overall >= _ACTION_WAIT_MIN:
            return EntryZone.CONFIRMATION
        return EntryZone.UNCLEAR

    @staticmethod
    def _preferred_action(overall: float, zone: EntryZone) -> PreferredAction:
        if zone is EntryZone.LATE:
            return PreferredAction.MONITOR  # risk of buying into distribution
        if overall < _ACTION_AVOID_BELOW:
            return PreferredAction.AVOID
        if overall >= _ACTION_CONSIDER_MIN and zone in (EntryZone.EARLY, EntryZone.CONFIRMATION):
            return PreferredAction.CONSIDER_RESEARCH_ENTRY
        if overall >= _ACTION_WAIT_MIN:
            return PreferredAction.WAIT_FOR_CONFIRMATION
        return PreferredAction.MONITOR
