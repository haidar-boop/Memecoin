"""Narrative intelligence & viral potential engine (Spec Part 19).

Turns qualitative narrative judgments plus available community evidence
into two scores and the Part 19 Section 12 report:

* **Viral score** (Section 3): memorability, shareability, emotional
  impact, cultural timing, community participation — 20% each.
* **Narrative intelligence score** (Section 11): meme strength, cultural
  timing, viral potential, community creativity, long-term narrative
  strength — 20% each. This is the number that fills the master
  framework's 15% ``narrative`` category (Part 31 lock) via
  :meth:`~meme_intelligence.analyzers.scoring_engine.ScoringEngine.evaluate`.

The two rubrics share the cultural-timing lens, and the viral score feeds
the intelligence score's ``viral_potential`` component — the only reading
under which both Section 3 and Section 11 can be satisfied by one engine.

Like :class:`~meme_intelligence.analyzers.foundation_analyzer.FoundationInputs`,
the judgment slots in :class:`NarrativeInputs` are produced by the AI
reasoning layer (Parts 13/23) or a human analyst — narrative strength is
not computable from market numbers. What *is* evidence-driven here:

* community participation/creativity cross-fill from the community
  engine's creativity sub-score (Rule 9 — multi-source; Rule 18 — reuse);
* artificial-attention detection piggybacking on the community engine's
  fake-community verdict (Section 5 — organic vs artificial growth);
* life-cycle-stage timing signals and stage risk findings (Section 7);
* sentiment classification from measured sentiment data (Section 6);
* narrative risk level derived from the three Section 10 risk factors.

Sentiment is classified and reported but never deducted from the score:
measured sentiment already feeds the community engine's loyalty category,
and double-counting one fact across two master-score categories would
skew the locked weighting (Rule 19 — documented trade-off).

Social trend monitoring (mentions, search interest, creator activity —
Section 5) needs the social collectors, which await a data-source budget
decision (see handoff/DECISIONS_LOG.md); until then those facts stay
unknown and coverage/confidence report the gap honestly (Rule 8).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.common import (
    Finding,
    SubScore,
    confidence_from_facts,
)
from meme_intelligence.analyzers.community_analyzer import CommunityAssessment
from meme_intelligence.config.settings import (
    NarrativeSubWeights,
    NarrativeThresholds,
    ViralSubWeights,
)
from meme_intelligence.core.enums import (
    CatalystLevel,
    ConfidenceLevel,
    NarrativeCategory,
    NarrativeRating,
    NarrativeRisk,
    NarrativeStage,
    RiskTier,
    SentimentLabel,
)
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity

# Rating bands over the narrative intelligence score (Part 19, Section 12),
# on the same ladder every quality rating in the framework uses.
_RATING_BANDS = (
    (85.0, NarrativeRating.EXCELLENT),
    (70.0, NarrativeRating.STRONG),
    (50.0, NarrativeRating.AVERAGE),
    (0.0, NarrativeRating.WEAK),
)

# Life-cycle stage timing signals (Part 19, Section 7): creation/expansion
# are the favorable discovery windows; mainstream is late; saturation and
# decline are where distribution risk lives.
_STAGE_TIMING_SIGNAL = {
    NarrativeStage.CREATION: 85.0,
    NarrativeStage.EXPANSION: 90.0,
    NarrativeStage.MAINSTREAM: 55.0,
    NarrativeStage.SATURATION: 30.0,
    NarrativeStage.DECLINE: 15.0,
}

# Section 10 risk deductions, applied to the long-term component: a
# narrative with "no lasting story" loses more longevity credit than one
# merely dependent on a trend or crowded with copycats.
_SHORT_TERM_HYPE_DEDUCTION = 30.0
_TREND_DEPENDENCY_DEDUCTION = 20.0
_COPYCAT_DEDUCTION = 20.0

# Sub-score levels treated as evidence for the strengths/weaknesses lists.
_STRENGTH_MIN = 70.0
_WEAKNESS_MAX = 40.0

# The NarrativeInputs fields validated as 0-100 judgments.
_JUDGMENT_SLOTS = (
    "memorability",
    "shareability",
    "emotional_impact",
    "cultural_timing",
    "community_participation",
    "meme_strength",
    "community_creativity",
    "long_term_strength",
)

_RISK_ESCALATION = {
    NarrativeRisk.LOW: NarrativeRisk.MEDIUM,
    NarrativeRisk.MEDIUM: NarrativeRisk.HIGH,
    NarrativeRisk.HIGH: NarrativeRisk.HIGH,
}


@dataclass(frozen=True)
class ViralCatalyst:
    """One possible attention trigger with its grading (Part 19, Section 9)."""

    description: str
    probability: CatalystLevel
    impact: CatalystLevel

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("catalyst description must be non-empty")
        # probability/impact are typed as CatalystLevel but nothing enforced
        # it — a non-enum value (e.g. a plain string) passed validation
        # here silently, then crashed later inside summary() when it reads
        # catalyst.probability.value, far from the actual bad input
        # (exactly the deferred-crash pattern already fixed once for
        # catalysts themselves; bug-hunt finding: it recurred one level in).
        if not isinstance(self.probability, CatalystLevel):
            raise TypeError(f"catalyst probability must be a CatalystLevel, got {self.probability!r}")
        if not isinstance(self.impact, CatalystLevel):
            raise TypeError(f"catalyst impact must be a CatalystLevel, got {self.impact!r}")


@dataclass(frozen=True)
class NarrativeInputs:
    """Qualitative narrative judgments, each 0-100 or None if not yet assessed.

    Guidance per slot (Part 19):

    * ``memorability``            -- easy to remember, stands out (Section 3)
    * ``shareability``            -- content can be created around it, spreads
      naturally (Section 3)
    * ``emotional_impact``        -- humor, excitement, identity (Section 3)
    * ``cultural_timing``         -- matches current trends, favorable timing
      (Sections 3 and 11; shared by both rubrics)
    * ``community_participation`` -- users contributing, memes being created
      (Section 3); falls back to the community engine's creativity sub-score
    * ``meme_strength``           -- the Section 4 simplicity / replication /
      identity tests combined into one judgment
    * ``community_creativity``    -- Section 11 slot; falls back to the
      community engine's creativity sub-score
    * ``long_term_strength``      -- ability to remain culturally relevant
      (Sections 11 and 13); Section 8 competition analysis (first-mover
      advantage, uniqueness versus similar narratives) informs this judgment

    ``category`` and ``stage`` classify per Sections 2 and 7 — the Section 2
    per-category considerations (e.g. a celebrity narrative's dependency
    risk) belong in the judgment slots and the three Section 10 risk flags:

    * ``short_term_hype_risk``   -- only price discussion, no identity or
      lasting story
    * ``trend_dependency_risk``  -- requires external attention, depends on
      one event
    * ``copycat_risk``           -- many similar projects, no unique advantage

    ``narrative_summary`` is the Section 12 "Narrative Summary" prose — a
    short explanation of the story behind the token (the Section 1
    questions: why it exists, why people care, why they would share it).

    ``None`` on any flag means "not assessed" — never assumed safe (Rule 8).
    """

    narrative_summary: str | None = None
    memorability: float | None = None
    shareability: float | None = None
    emotional_impact: float | None = None
    cultural_timing: float | None = None
    community_participation: float | None = None
    meme_strength: float | None = None
    community_creativity: float | None = None
    long_term_strength: float | None = None
    category: NarrativeCategory = NarrativeCategory.UNKNOWN
    stage: NarrativeStage = NarrativeStage.UNKNOWN
    short_term_hype_risk: bool | None = None
    trend_dependency_risk: bool | None = None
    copycat_risk: bool | None = None
    catalysts: tuple[ViralCatalyst, ...] = ()

    def __post_init__(self) -> None:
        for name in _JUDGMENT_SLOTS:
            value = getattr(self, name)
            if value is not None and not (0.0 <= value <= 100.0):
                raise ValueError(f"narrative input '{name}' must be within 0-100, got {value}")
        if not isinstance(self.catalysts, tuple) or not all(
            isinstance(c, ViralCatalyst) for c in self.catalysts
        ):
            raise TypeError(
                "narrative input 'catalysts' must be a tuple of ViralCatalyst, "
                f"got {self.catalysts!r}"
            )


@dataclass(frozen=True)
class NarrativeAssessment:
    """Narrative verdict for one token (report format per Part 19, Section 12)."""

    token: TokenIdentity
    narrative_summary: str | None              # Section 12 prose summary
    sub_scores: dict[str, float | None]        # Section 11 components
    viral_sub_scores: dict[str, float | None]  # Section 3 components
    viral_score: float | None
    overall_score: float
    category: NarrativeCategory
    stage: NarrativeStage
    sentiment: SentimentLabel
    narrative_risk: NarrativeRisk
    rating: NarrativeRating
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    catalysts: tuple[ViralCatalyst, ...]
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float

    def summary(self) -> str:
        lines = [
            f"Narrative assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100 [{self.rating.value}]  "
            f"category={self.category.value}  stage={self.stage.value}  "
            f"confidence={self.confidence.value}",
            "  Viral score: "
            + (f"{self.viral_score:.0f}/100" if self.viral_score is not None else "no data")
            + f"    sentiment={self.sentiment.value}  narrative risk={self.narrative_risk.value}",
        ]
        if self.narrative_summary:
            lines.append(f"  Summary: {self.narrative_summary}")
        if self.coverage < 1.0:
            lines.append(
                f"  NOTE: only {self.coverage:.0%} of narrative categories have data; "
                "treat as an incomplete picture"
            )
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>20}: {rendered}")
        if self.strengths:
            lines.append("  Strengths:")
            for item in self.strengths:
                lines.append(f"    + {item}")
        if self.weaknesses:
            lines.append("  Weaknesses:")
            for item in self.weaknesses:
                lines.append(f"    - {item}")
        if self.catalysts:
            lines.append("  Viral catalysts:")
            for catalyst in self.catalysts:
                lines.append(f"    * {catalyst.description} "
                             f"(probability={catalyst.probability.value}, "
                             f"impact={catalyst.impact.value})")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        return "\n".join(lines)


class NarrativeAnalyzer:
    """Scores narrative strength and viral potential per the Part 19 framework."""

    def __init__(
        self,
        thresholds: NarrativeThresholds,
        weights: NarrativeSubWeights,
        viral_weights: ViralSubWeights,
    ) -> None:
        self._t = thresholds
        self._w = weights
        self._vw = viral_weights
        self._logger = get_logger("analyzers.narrative")

    def assess(
        self,
        token: TokenIdentity,
        inputs: NarrativeInputs,
        *,
        community: CommunityAssessment | None = None,
        positive_sentiment_percent: float | None = None,
    ) -> NarrativeAssessment:
        """``community`` supplies the participation/creativity fallback and the
        organic-vs-artificial growth check; ``positive_sentiment_percent`` is
        measured sentiment data (e.g. ``CommunityProfile.positive_sentiment_percent``).
        """
        if positive_sentiment_percent is not None and not (0.0 <= positive_sentiment_percent <= 100.0):
            raise ValueError(
                f"positive_sentiment_percent must be within 0-100, got {positive_sentiment_percent}"
            )

        # An artificial community is not participation evidence — its
        # creativity numbers describe bots, not culture (Part 19 Section 5).
        creativity_fallback = None
        if community is not None and not community.is_artificial:
            creativity_fallback = community.sub_scores.get("creativity")

        timing = self._assess_cultural_timing(inputs)  # shared by both rubrics

        # ---- Viral score (Part 19, Section 3) ----
        viral_parts = [
            self._judgment("memorability", inputs.memorability),
            self._judgment("shareability", inputs.shareability),
            self._judgment("emotional_impact", inputs.emotional_impact),
            timing,
            self._assess_participation(inputs, community, creativity_fallback),
        ]
        viral_sub_scores, viral_score, _ = self._weighted(
            viral_parts, dataclasses.asdict(self._vw),
            ("memorability", "shareability", "emotional_impact",
             "cultural_timing", "community_participation"),
        )

        # ---- Narrative intelligence score (Part 19, Section 11) ----
        nis_parts = [
            self._judgment("meme_strength", inputs.meme_strength),
            timing,
            self._viral_potential(viral_score),
            self._assess_creativity(inputs, creativity_fallback),
            self._assess_long_term(inputs),
        ]
        sub_scores, overall, coverage = self._weighted(
            nis_parts, dataclasses.asdict(self._w),
            ("meme_strength", "cultural_timing", "viral_potential",
             "community_creativity", "long_term_strength"),
        )
        if overall is None:
            raise InsufficientDataError(
                f"no narrative inputs assessed for {token.address} on {token.chain}"
            )

        # Collect findings/unknowns once per distinct part (timing appears in
        # both rubrics and must not be double-counted).
        parts = viral_parts + [p for p in nis_parts if p is not timing]
        findings = [f for part in parts for f in part.findings]
        unknowns = [u for part in parts for u in part.unknowns]
        known = sum(part.known_count for part in parts)

        # community_creativity_proxy is ONE underlying community-engine fact
        # that both _assess_participation and _assess_creativity fall back to
        # (Sections 3 and 11) — when both lack their own judgment, the same
        # fact is observed twice; dedupe so it is tallied once, not twice,
        # in the aggregate confidence count (Rule 8).
        if inputs.community_participation is None and inputs.community_creativity is None:
            if creativity_fallback is not None:
                known -= 1
            else:
                try:
                    unknowns.remove("community_creativity_proxy")
                except ValueError:
                    pass

        sentiment = self._classify_sentiment(positive_sentiment_percent)
        if sentiment is SentimentLabel.NEGATIVE:
            findings.append(Finding(
                "sentiment", RiskTier.ACCEPTABLE_UNCERTAINTY,
                "public sentiment is negative: distrust or declining enthusiasm",
            ))
        # Stage is already observed inside the cultural-timing part; only
        # category and sentiment need separate fact tracking here.
        for name, is_known in (("category", inputs.category is not NarrativeCategory.UNKNOWN),
                               ("sentiment", sentiment is not SentimentLabel.UNKNOWN)):
            if is_known:
                known += 1
            else:
                unknowns.append(name)

        narrative_risk = self._narrative_risk(inputs)
        rating = self._rating(overall)
        strengths, weaknesses = self._evidence_lists(
            sub_scores, viral_sub_scores, findings, community,
        )

        self._logger.info(
            "narrative assessment %s/%s: score=%.0f rating=%s category=%s stage=%s risk=%s",
            token.chain, token.address, overall, rating.value,
            inputs.category.value, inputs.stage.value, narrative_risk.value,
        )

        return NarrativeAssessment(
            token=token,
            narrative_summary=inputs.narrative_summary,
            sub_scores=sub_scores,
            viral_sub_scores=viral_sub_scores,
            viral_score=viral_score,
            overall_score=overall,
            category=inputs.category,
            stage=inputs.stage,
            sentiment=sentiment,
            narrative_risk=narrative_risk,
            rating=rating,
            strengths=strengths,
            weaknesses=weaknesses,
            catalysts=inputs.catalysts,
            confidence=confidence_from_facts(known, len(unknowns)),
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=coverage,
        )

    # ---- Single-judgment components (Sections 3, 4, 11) ----

    @staticmethod
    def _judgment(name: str, value: float | None) -> SubScore:
        s = SubScore(name)
        if s.observe(name, value):
            s.signal(value)
        return s

    # ---- Cultural timing: judgment + life-cycle stage (Sections 3, 7, 11) ----

    def _assess_cultural_timing(self, inputs: NarrativeInputs) -> SubScore:
        s = SubScore("cultural_timing")

        if s.observe("cultural_timing", inputs.cultural_timing):
            s.signal(inputs.cultural_timing)

        stage = inputs.stage if inputs.stage is not NarrativeStage.UNKNOWN else None
        if s.observe("lifecycle_stage", stage):
            s.signal(_STAGE_TIMING_SIGNAL[stage])
            if stage is NarrativeStage.MAINSTREAM:
                s.deduct(0, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         "narrative already has mainstream attention: late buyers may enter")
            elif stage is NarrativeStage.SATURATION:
                s.deduct(0, RiskTier.SERIOUS_WARNING,
                         "narrative is saturated: excessive hype and community fatigue "
                         "point to a potential distribution phase")
            elif stage is NarrativeStage.DECLINE:
                s.deduct(0, RiskTier.SERIOUS_WARNING,
                         "narrative is in decline: attention is leaving")

        return s

    # ---- Community participation: are users contributing? (Sections 3, 5) ----

    def _assess_participation(
        self,
        inputs: NarrativeInputs,
        community: CommunityAssessment | None,
        creativity_fallback: float | None,
    ) -> SubScore:
        s = SubScore("community_participation")

        if s.observe("community_participation", inputs.community_participation):
            s.signal(inputs.community_participation)
        elif s.observe("community_creativity_proxy", creativity_fallback):
            s.signal(creativity_fallback)

        # Organic vs artificial growth (Section 5): the community engine has
        # already hunted bot followers and scripted engagement — reuse its
        # verdict instead of re-deriving it (Rule 18). Fake participation is
        # zero participation; an organic verdict alone says nothing about
        # HOW MUCH participation exists, so it never contributes a score.
        if community is not None and community.is_artificial:
            if s.observe("attention_growth_organic", False):
                s.signal(0.0)
                s.deduct(0, RiskTier.SERIOUS_WARNING,
                         "attention growth appears artificial: community engagement "
                         "was flagged as fake")

        return s

    # ---- Community creativity component (Section 11) ----

    def _assess_creativity(
        self,
        inputs: NarrativeInputs,
        creativity_fallback: float | None,
    ) -> SubScore:
        s = SubScore("community_creativity")
        if s.observe("community_creativity", inputs.community_creativity):
            s.signal(inputs.community_creativity)
        elif s.observe("community_creativity_proxy", creativity_fallback):
            s.signal(creativity_fallback)
        return s

    # ---- Long-term strength + Section 10 risk factors ----

    def _assess_long_term(self, inputs: NarrativeInputs) -> SubScore:
        # requires_signal=True: the Section 10 risk flags alone must not
        # fabricate a base-100 "perfect" score when no long_term_strength
        # judgment exists (Rule 8) — mirrors the organic-verdict guard in
        # _assess_participation.
        s = SubScore("long_term_strength", requires_signal=True)

        if s.observe("long_term_strength", inputs.long_term_strength):
            s.signal(inputs.long_term_strength)

        if s.observe("short_term_hype_risk", inputs.short_term_hype_risk):
            if inputs.short_term_hype_risk:
                s.deduct(_SHORT_TERM_HYPE_DEDUCTION, RiskTier.SERIOUS_WARNING,
                         "short-term hype risk: only price discussion, no lasting story")
        if s.observe("trend_dependency_risk", inputs.trend_dependency_risk):
            if inputs.trend_dependency_risk:
                s.deduct(_TREND_DEPENDENCY_DEDUCTION, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         "trend dependency risk: narrative requires external attention")
        if s.observe("copycat_risk", inputs.copycat_risk):
            if inputs.copycat_risk:
                s.deduct(_COPYCAT_DEDUCTION, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         "copycat risk: many similar projects, no unique advantage")

        return s

    # ---- Viral potential component of the intelligence score (Section 11) ----

    @staticmethod
    def _viral_potential(viral_score: float | None) -> SubScore:
        s = SubScore("viral_potential")
        if s.observe("viral_score", viral_score):
            s.signal(viral_score)
        return s

    # ---- Aggregation ----

    @staticmethod
    def _weighted(
        parts: list[SubScore],
        weight_map: dict[str, float],
        names: tuple[str, ...],
    ) -> tuple[dict[str, float | None], float | None, float]:
        """Coverage-honest weighted average; scores keyed by ``names`` so the
        shared cultural-timing part can appear in both rubrics."""
        sub_scores: dict[str, float | None] = {}
        weighted_sum = 0.0
        available = 0.0
        # strict=True: both callers pass a names tuple parallel to parts (5 and
        # 5); a mismatch would silently drop a sub-score from the average.
        for name, part in zip(names, parts, strict=True):
            score = part.score()
            sub_scores[name] = score
            if score is not None:
                weighted_sum += score * weight_map[name]
                available += weight_map[name]
        if available == 0.0:
            return sub_scores, None, 0.0
        return sub_scores, weighted_sum / available, available

    # ---- Sentiment (Section 6) ----

    def _classify_sentiment(self, percent: float | None) -> SentimentLabel:
        if percent is None:
            return SentimentLabel.UNKNOWN
        if percent >= self._t.positive_sentiment_percent:
            return SentimentLabel.POSITIVE
        if percent <= self._t.negative_sentiment_percent:
            return SentimentLabel.NEGATIVE
        return SentimentLabel.NEUTRAL

    # ---- Narrative risk level (Sections 10 and 12) ----

    @staticmethod
    def _narrative_risk(inputs: NarrativeInputs) -> NarrativeRisk:
        flags = (inputs.short_term_hype_risk, inputs.trend_dependency_risk,
                 inputs.copycat_risk)
        assessed = [flag for flag in flags if flag is not None]
        confirmed = sum(1 for flag in assessed if flag)
        late_stage = inputs.stage in (NarrativeStage.SATURATION, NarrativeStage.DECLINE)

        # Nothing assessed either way = UNKNOWN, not LOW — an unexamined
        # narrative is never labeled safe (Rule 8). A late life-cycle stage
        # is itself evidence of risk (Section 7).
        if not assessed:
            return NarrativeRisk.MEDIUM if late_stage else NarrativeRisk.UNKNOWN

        if confirmed >= 2:
            level = NarrativeRisk.HIGH
        elif confirmed == 1:
            level = NarrativeRisk.MEDIUM
        else:
            level = NarrativeRisk.LOW
        if late_stage:
            level = _RISK_ESCALATION[level]
        return level

    # ---- Evidence-derived strengths & weaknesses (Section 12) ----

    @staticmethod
    def _evidence_lists(
        sub_scores: dict[str, float | None],
        viral_sub_scores: dict[str, float | None],
        findings: list[Finding],
        community: CommunityAssessment | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        strengths: list[str] = []
        weaknesses: list[str] = []

        combined = dict(viral_sub_scores)
        combined.update(sub_scores)  # NIS values win for the shared timing key
        for name, score in combined.items():
            label = name.replace("_", " ")
            if score is None:
                continue
            if score >= _STRENGTH_MIN:
                strengths.append(f"{label} rates {score:.0f}/100")
            elif score <= _WEAKNESS_MAX:
                weaknesses.append(f"{label} rates only {score:.0f}/100")

        if (community is not None and not community.is_artificial
                and community.overall_score >= _STRENGTH_MIN):
            strengths.append("community engagement is organic and independently rated "
                             f"{community.overall_score:.0f}/100")

        for finding in findings:
            if finding.severity in (RiskTier.SERIOUS_WARNING, RiskTier.DESTRUCTIVE):
                weaknesses.append(finding.message)

        return tuple(strengths), tuple(weaknesses)

    @staticmethod
    def _rating(score: float) -> NarrativeRating:
        for minimum, rating in _RATING_BANDS:
            if score >= minimum:
                return rating
        return NarrativeRating.WEAK
