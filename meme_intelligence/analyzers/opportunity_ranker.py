"""Watchlist opportunity ranking (Spec Part 28, Sections 5-6).

Part 28's mission: after discovery, keep ranking tracked tokens so the
strongest current opportunities get attention and weakening ones lose
priority. This is a SECOND, upside-tilted ranking axis, deliberately
separate from the Part 31-locked master score:

* The **master score** (``scoring_engine.py``) is a balanced,
  security-first *quality/classification* judgment (Elite / Strong /
  Watchlist / Avoid). It is frozen by the Part 31 Framework Consistency
  Lock and this module never touches it.
* The **opportunity rank** answers a different question — "of everything
  we're tracking, which deserves attention *right now*?" — with the
  Section 5 upside-tilted weights (Growth 30 / Momentum 25 / Foundation
  20 / Risk 15 / Timing 10).

Both are computed from the same already-produced analyzer outputs, so
the rank adds no new data collection (Rule 10) and cannot fabricate — a
factor with no underlying data is excluded and the remaining weights are
renormalized, exactly like the master engine's coverage handling (Rule
8). The rank is advisory prioritization only; it never implies holding
or trading (the never-execute rule).
"""

from __future__ import annotations

from dataclasses import dataclass

from meme_intelligence.config.settings import OpportunityWeights
from meme_intelligence.core.models import CategoryScores


@dataclass(frozen=True)
class OpportunityRank:
    """Where one tracked token sits on the Part 28 Section 5 ranking."""

    score: float                       # 0-100, higher = stronger current opportunity
    components: dict[str, float | None]  # per-factor contributions (None = no data)
    coverage: float                    # fraction of ranking weight backed by data

    def summary(self) -> str:
        parts = ", ".join(
            f"{name}={value:.0f}" if value is not None else f"{name}=?"
            for name, value in self.components.items()
        )
        return f"opportunity {self.score:.0f}/100 ({parts}; coverage {self.coverage:.0%})"


class OpportunityRanker:
    """Ranks tracked tokens by current opportunity (Part 28 Section 5)."""

    def __init__(self, weights: OpportunityWeights) -> None:
        self._w = weights

    def rank(
        self,
        category_scores: CategoryScores,
        risk_score: float | None = None,
    ) -> OpportunityRank:
        """Compute the opportunity rank from a master assessment's category
        scores plus the risk score (``higher = riskier``; opportunity rises
        as risk falls). Any factor whose source score is ``None`` is dropped
        and the remaining weights renormalize (Rule 8)."""
        # Section 5 factor -> existing analyzer output:
        #   Growth Potential  <- narrative strength (the memecoin growth driver)
        #   Current Momentum  <- momentum score
        #   Foundation Quality<- foundation category (Community+Security+Token)
        #   Risk Level        <- 100 - risk_score (low risk = more opportunity)
        #   Timing            <- timing category
        risk_component = None if risk_score is None else max(0.0, min(100.0, 100.0 - risk_score))
        factors: dict[str, tuple[float | None, float]] = {
            "growth_potential": (category_scores.narrative, self._w.growth_potential),
            "momentum": (category_scores.momentum, self._w.momentum),
            "foundation": (category_scores.foundation, self._w.foundation),
            "risk": (risk_component, self._w.risk),
            "timing": (category_scores.timing, self._w.timing),
        }

        weighted_sum = 0.0
        available = 0.0
        components: dict[str, float | None] = {}
        for name, (value, weight) in factors.items():
            components[name] = value
            if value is not None:
                weighted_sum += value * weight
                available += weight

        score = weighted_sum / available if available > 0 else 0.0
        # coverage is the share of total ranking weight that had data
        total_weight = sum(weight for _, weight in factors.values())
        coverage = available / total_weight if total_weight > 0 else 0.0
        return OpportunityRank(
            score=round(score, 1),
            components=components,
            coverage=coverage,
        )
