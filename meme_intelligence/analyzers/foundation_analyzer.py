"""Foundation scoring (Spec Part 5, Sections 1-5 and 12).

Meme strength, narrative, brand identity, and long-term potential are
qualitative judgments — per the agent architecture (Parts 13/23) they are
produced by the AI reasoning layer (or a human analyst), not computed from
market numbers. This module supplies the *structured* half:

* :class:`FoundationInputs` — the validated 0-100 judgment slots;
* :class:`FoundationAnalyzer` — combines them with the community quality
  score (from :class:`CommunityAnalyzer`) using the Part 5 Section 12
  weights, with the same coverage/confidence honesty as every other engine.

When the AI integration phase lands, the report generator fills
``FoundationInputs`` from structured LLM output; until then callers may
supply analyst judgments directly or leave slots ``None``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.common import confidence_from_facts
from meme_intelligence.config.settings import FoundationSubWeights
from meme_intelligence.core.enums import ConfidenceLevel
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import TokenIdentity


@dataclass(frozen=True)
class FoundationInputs:
    """Qualitative foundation judgments, each 0-100 or None if not yet assessed.

    Guidance per slot (Part 5):

    * ``meme_strength``     -- simplicity, recognition, adaptability, emotion (Section 2)
    * ``narrative``         -- relevance, uniqueness, shareability, longevity (Section 3)
    * ``brand``             -- visual identity, consistency, memorability (Section 4)
    * ``dev_communication`` -- transparency, cadence, community relationship (Section 5)
    * ``long_term``         -- staying power beyond the current hype cycle (Section 1)
    """

    meme_strength: float | None = None
    narrative: float | None = None
    brand: float | None = None
    dev_communication: float | None = None
    long_term: float | None = None

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if value is not None and not (0.0 <= value <= 100.0):
                raise ValueError(f"foundation input '{name}' must be within 0-100, got {value}")


@dataclass(frozen=True)
class FoundationAssessment:
    """Combined foundation verdict (Part 5, Section 12 weighting)."""

    token: TokenIdentity
    sub_scores: dict[str, float | None]
    overall_score: float
    confidence: ConfidenceLevel
    missing: tuple[str, ...]
    coverage: float

    def summary(self) -> str:
        lines = [
            f"Foundation assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100  confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(f"  NOTE: only {self.coverage:.0%} of foundation categories assessed")
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "not assessed"
            lines.append(f"  {name:>18}: {rendered}")
        return "\n".join(lines)


class FoundationAnalyzer:
    """Combines qualitative judgments + community quality into the foundation score."""

    def __init__(self, weights: FoundationSubWeights):
        self._w = weights

    def assess(
        self,
        token: TokenIdentity,
        inputs: FoundationInputs,
        community_quality: float | None = None,
    ) -> FoundationAssessment:
        """``community_quality`` is the overall score from :class:`CommunityAnalyzer`."""
        if community_quality is not None and not (0.0 <= community_quality <= 100.0):
            raise ValueError(f"community_quality must be within 0-100, got {community_quality}")

        scores: dict[str, float | None] = {
            "meme_strength": inputs.meme_strength,
            "narrative": inputs.narrative,
            "brand": inputs.brand,
            "community_quality": community_quality,
            "dev_communication": inputs.dev_communication,
            "long_term": inputs.long_term,
        }
        weight_map = dataclasses.asdict(self._w)

        weighted_sum = 0.0
        available_weight = 0.0
        missing: list[str] = []
        for name, value in scores.items():
            if value is None:
                missing.append(name)
            else:
                weighted_sum += value * weight_map[name]
                available_weight += weight_map[name]

        if available_weight == 0.0:
            raise InsufficientDataError(f"no foundation inputs assessed for {token.address}")

        known = len(scores) - len(missing)
        return FoundationAssessment(
            token=token,
            sub_scores=scores,
            overall_score=weighted_sum / available_weight,
            confidence=confidence_from_facts(known, len(missing)),
            missing=tuple(missing),
            coverage=available_weight,
        )
