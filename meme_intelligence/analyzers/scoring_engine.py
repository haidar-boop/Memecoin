"""Master scoring & decision engine (Spec Part 10, weights per Part 31 lock).

Combines every analysis engine's output into the Final Intelligence Score
and classification. Three mechanisms, applied in order:

1. **Red-flag overrides** (Part 10, Section 5): certain confirmed
   conditions force AVOID regardless of the weighted score — destructive
   security risk, fake community, extreme risk profile.
2. **Decision tree** (Part 10, Section 7): six ordered questions that can
   reject outright or cap the classification. Unknown answers neither
   pass nor fail — they are recorded and reduce confidence.
3. **Weighted score** (Part 31, Section 4): Foundation 15%, Security 15%,
   Community 15%, Blockchain 15%, Momentum 15%, Narrative 15%, Timing 10%,
   renormalized over the categories that have data, with coverage reported.

Category input mapping (documented decisions):

* ``foundation`` combines the foundation assessment (qualitative) and the
  token-structure assessment — Part 31 defines Foundation as "project
  structure, developer behavior, token design, long-term potential",
  which spans both engines. When both exist they average.
* ``momentum`` and ``narrative`` accept scores from their engines (built
  in later phases) or the AI layer; until then they report as missing.
* ``timing`` can be derived heuristically via :func:`derive_timing_score`
  (discovery freshness x market-cap stage x market phase).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from meme_intelligence.analyzers.common import confidence_from_facts
from meme_intelligence.analyzers.community_analyzer import CommunityAssessment
from meme_intelligence.analyzers.foundation_analyzer import FoundationAssessment
from meme_intelligence.analyzers.onchain_analyzer import OnChainAssessment
from meme_intelligence.analyzers.risk_analyzer import RiskAssessment
from meme_intelligence.analyzers.security_analyzer import SecurityAssessment
from meme_intelligence.analyzers.token_analyzer import TokenAssessment
from meme_intelligence.config.settings import ClassificationBands, ScoringWeights
from meme_intelligence.core.enums import (
    Classification,
    ConfidenceLevel,
    MarketCapStage,
    MarketPhase,
    RiskCategory,
)
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import CategoryScores, DexPair, TokenIdentity, classify

# Classification ordering for applying downgrades/caps (worst -> best).
_CLASS_ORDER = [
    Classification.AVOID,
    Classification.SPECULATIVE,
    Classification.WATCHLIST,
    Classification.STRONG_CANDIDATE,
    Classification.ELITE_OPPORTUNITY,
]

# Decision-tree thresholds (Part 10, Section 7).
_CONTRACT_REJECT_BELOW = 30.0
_LIQUIDITY_CAP_BELOW = 40.0
_ONCHAIN_CAP_BELOW = 40.0
_NARRATIVE_WEAK_BELOW = 40.0

# Timing heuristic anchors (Part 31 — discovery stage, market cycle, attention phase).
_AGE_TIMING = ((24.0, 90.0), (7 * 24.0, 70.0), (30 * 24.0, 50.0))
_AGE_TIMING_FLOOR = 30.0
_STAGE_TIMING = {
    MarketCapStage.EARLY: 85.0,
    MarketCapStage.GROWTH: 60.0,
    MarketCapStage.MATURE: 30.0,
}
_PHASE_TIMING = {
    MarketPhase.ACCUMULATION: 80.0,
    MarketPhase.EXPANSION: 70.0,
    MarketPhase.UNCLEAR: 50.0,
    MarketPhase.DISTRIBUTION: 20.0,
}


@dataclass(frozen=True)
class DecisionStep:
    """One decision-tree question with its verdict (Part 10, Section 7)."""

    question: str
    answer: str  # "yes" / "no" / "unknown"
    action: str  # what the engine did about it


@dataclass(frozen=True)
class MasterAssessment:
    """The combined verdict every downstream consumer builds on (Part 10, Section 8)."""

    token: TokenIdentity
    generated_at: datetime
    category_scores: CategoryScores
    final_score: float
    coverage: float
    classification: Classification
    overrides: tuple[str, ...]        # red-flag reasons that forced AVOID
    decision_trace: tuple[DecisionStep, ...]
    confidence: ConfidenceLevel

    def summary(self) -> str:
        lines = [
            f"MASTER ASSESSMENT — {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Final score: {self.final_score:.0f}/100  "
            f"classification={self.classification.value}  "
            f"confidence={self.confidence.value}  evidence coverage={self.coverage:.0%}",
        ]
        if self.overrides:
            lines.append("  RED-FLAG OVERRIDES (forced Avoid):")
            for reason in self.overrides:
                lines.append(f"    ! {reason}")
        lines.append("  Category scores:")
        for name, value in dataclasses.asdict(self.category_scores).items():
            rendered = f"{value:.0f}/100" if value is not None else "no data"
            lines.append(f"    {name:>10}: {rendered}")
        lines.append("  Decision trace:")
        for step in self.decision_trace:
            lines.append(f"    [{step.answer:>7}] {step.question} -> {step.action}")
        return "\n".join(lines)


def derive_timing_score(
    pair: DexPair | None,
    token: TokenAssessment | None,
    onchain: OnChainAssessment | None,
    *,
    now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> float | None:
    """Heuristic opportunity-timing score (Part 31, Section 4 — timing 10%).

    Averages whichever of these are observable: pair freshness, market-cap
    stage, and accumulation/distribution phase. Returns ``None`` when none
    are known — timing is never invented (Rule 8).
    """
    signals: list[float] = []

    if pair is not None and pair.pair_created_at is not None:
        age_hours = max(0.0, (now_func() - pair.pair_created_at).total_seconds() / 3600.0)
        for limit, points in _AGE_TIMING:
            if age_hours <= limit:
                signals.append(points)
                break
        else:
            signals.append(_AGE_TIMING_FLOOR)

    if token is not None and token.stage in _STAGE_TIMING:
        signals.append(_STAGE_TIMING[token.stage])

    if onchain is not None:
        signals.append(_PHASE_TIMING[onchain.phase])

    if not signals:
        return None
    return sum(signals) / len(signals)


class ScoringEngine:
    """Applies overrides, the decision tree, and the locked weighting (Part 10)."""

    def __init__(
        self,
        weights: ScoringWeights,
        bands: ClassificationBands,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._weights = weights
        self._bands = bands
        self._now = now_func
        self._logger = get_logger("analyzers.scoring")

    def evaluate(
        self,
        security: SecurityAssessment,
        *,
        community: CommunityAssessment | None = None,
        onchain: OnChainAssessment | None = None,
        foundation: FoundationAssessment | None = None,
        token_structure: TokenAssessment | None = None,
        risk: RiskAssessment | None = None,
        momentum_score: float | None = None,
        narrative_score: float | None = None,
        timing_score: float | None = None,
    ) -> MasterAssessment:
        for name, value in (("momentum_score", momentum_score),
                            ("narrative_score", narrative_score),
                            ("timing_score", timing_score)):
            if value is not None and not (0.0 <= value <= 100.0):
                raise ValueError(f"{name} must be within 0-100, got {value}")

        scores = CategoryScores(
            foundation=self._foundation_input(foundation, token_structure),
            security=security.overall_score,
            community=community.overall_score if community else None,
            blockchain=onchain.overall_score if onchain else None,
            momentum=momentum_score,
            narrative=narrative_score,
            timing=timing_score,
        )

        overrides = self._red_flag_overrides(security, community, risk)
        trace, caps, rejected = self._decision_tree(security, community, onchain,
                                                    narrative_score, risk)

        final_score, coverage = self._weighted(scores)

        if overrides or rejected:
            classification = Classification.AVOID
        else:
            classification = classify(final_score, self._bands)
            for cap in caps:
                if _CLASS_ORDER.index(classification) > _CLASS_ORDER.index(cap):
                    classification = cap

        known = sum(1 for v in dataclasses.asdict(scores).values() if v is not None)
        unknown = sum(1 for v in dataclasses.asdict(scores).values() if v is None)
        confidence = confidence_from_facts(known, unknown)

        self._logger.info(
            "master assessment %s/%s: score=%.0f class=%s coverage=%.0f%% overrides=%d",
            security.token.chain, security.token.address,
            final_score, classification.value, coverage * 100, len(overrides),
        )

        return MasterAssessment(
            token=security.token,
            generated_at=self._now(),
            category_scores=scores,
            final_score=final_score,
            coverage=coverage,
            classification=classification,
            overrides=tuple(overrides),
            decision_trace=tuple(trace),
            confidence=confidence,
        )

    # ---- Category input mapping ----

    @staticmethod
    def _foundation_input(
        foundation: FoundationAssessment | None,
        token_structure: TokenAssessment | None,
    ) -> float | None:
        values = [a.overall_score for a in (foundation, token_structure) if a is not None]
        if not values:
            return None
        return sum(values) / len(values)

    # ---- Red-flag overrides (Part 10, Section 5) ----

    @staticmethod
    def _red_flag_overrides(security, community, risk) -> list[str]:
        overrides: list[str] = []
        for finding in security.destructive_findings:
            overrides.append(f"destructive security risk: {finding.message}")
        if community is not None and community.is_artificial:
            overrides.append("fake community detected")
        if risk is not None and risk.category is RiskCategory.EXTREME:
            overrides.append("extreme overall risk profile")
        return overrides

    # ---- Decision tree (Part 10, Section 7) ----

    def _decision_tree(
        self, security, community, onchain, narrative_score, risk,
    ) -> tuple[list[DecisionStep], list[Classification], bool]:
        trace: list[DecisionStep] = []
        caps: list[Classification] = []
        rejected = False

        # Q1 — Is the contract safe?
        contract = security.sub_scores.get("contract")
        if security.is_destructive or (contract is not None and contract < _CONTRACT_REJECT_BELOW):
            trace.append(DecisionStep("Is the contract safe?", "no", "reject"))
            rejected = True
        elif contract is None:
            trace.append(DecisionStep("Is the contract safe?", "unknown",
                                      "continue with reduced confidence"))
        else:
            trace.append(DecisionStep("Is the contract safe?", "yes", "continue"))

        # Q2 — Is liquidity healthy?
        liquidity = security.sub_scores.get("liquidity")
        if liquidity is None:
            trace.append(DecisionStep("Is liquidity healthy?", "unknown",
                                      "continue with reduced confidence"))
        elif liquidity < _LIQUIDITY_CAP_BELOW:
            trace.append(DecisionStep("Is liquidity healthy?", "no",
                                      "cap classification at Speculative"))
            caps.append(Classification.SPECULATIVE)
        else:
            trace.append(DecisionStep("Is liquidity healthy?", "yes", "continue"))

        # Q3 — Is the community real?
        if community is None:
            trace.append(DecisionStep("Is the community real?", "unknown",
                                      "continue with reduced confidence"))
        elif community.is_artificial:
            trace.append(DecisionStep("Is the community real?", "no", "reject"))
            rejected = True
        else:
            trace.append(DecisionStep("Is the community real?", "yes", "continue"))

        # Q4 — Is on-chain activity healthy?
        if onchain is None:
            trace.append(DecisionStep("Is on-chain activity healthy?", "unknown",
                                      "continue with reduced confidence"))
        elif onchain.overall_score < _ONCHAIN_CAP_BELOW:
            trace.append(DecisionStep("Is on-chain activity healthy?", "no",
                                      "cap classification at Speculative"))
            caps.append(Classification.SPECULATIVE)
        else:
            trace.append(DecisionStep("Is on-chain activity healthy?", "yes", "continue"))

        # Q5 — Does the narrative have growth potential?
        if narrative_score is None:
            trace.append(DecisionStep("Does the narrative have growth potential?", "unknown",
                                      "continue with reduced confidence"))
        elif narrative_score < _NARRATIVE_WEAK_BELOW:
            trace.append(DecisionStep("Does the narrative have growth potential?", "no",
                                      "weak narrative lowers the weighted score"))
        else:
            trace.append(DecisionStep("Does the narrative have growth potential?", "yes",
                                      "continue"))

        # Q6 — Is risk/reward attractive?
        if risk is None:
            trace.append(DecisionStep("Is risk/reward attractive?", "unknown",
                                      "continue with reduced confidence"))
        elif risk.category is RiskCategory.EXTREME:
            trace.append(DecisionStep("Is risk/reward attractive?", "no", "reject"))
            rejected = True
        elif risk.category is RiskCategory.HIGH:
            trace.append(DecisionStep("Is risk/reward attractive?", "no",
                                      "cap classification at Speculative"))
            caps.append(Classification.SPECULATIVE)
        else:
            trace.append(DecisionStep("Is risk/reward attractive?", "yes", "continue"))

        return trace, caps, rejected

    # ---- Weighted score (Part 31, Section 4) ----

    def _weighted(self, scores: CategoryScores) -> tuple[float, float]:
        score_map = dataclasses.asdict(scores)
        weight_map = dataclasses.asdict(self._weights)
        weighted_sum = 0.0
        available = 0.0
        for name, value in score_map.items():
            if value is not None:
                weighted_sum += value * weight_map[name]
                available += weight_map[name]
        if available == 0.0:
            raise InsufficientDataError("no category scores available for master assessment")
        # Clamp: float renormalization can overshoot to 100.00000000000001 on
        # boundary-valid all-100 inputs, and classify() then raised ValueError,
        # aborting the whole cycle/routine (bug-hunt finding, reproduced).
        return min(100.0, max(0.0, weighted_sum / available)), available
