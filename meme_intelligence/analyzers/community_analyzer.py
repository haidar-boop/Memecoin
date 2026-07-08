"""Community intelligence engine (Spec Part 5, Part 18 Section 8).

Turns a normalized :class:`CommunityProfile` into a scored
:class:`CommunityAssessment` across the five Part 5 Section 11 categories:
engagement, growth, loyalty, creativity, and developer relationship.

Core doctrine (Part 5): a large following is NOT a strong community.
100,000 followers with no real engagement is weaker than 5,000 highly
active supporters — so engagement quality outweighs raw size everywhere,
and fake-community indicators (bot followers, duplicate messages,
admin-only channels) are hunted explicitly. A *confirmed* fake community
is a destructive finding: it forces the ARTIFICIAL rating and, at the
scoring engine, an Avoid classification (Part 10, Section 5).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.common import (
    Finding,
    SubScore,
    confidence_from_facts,
    scale,
)
from meme_intelligence.config.settings import CommunitySubWeights, CommunityThresholds
from meme_intelligence.core.enums import CommunityRating, ConfidenceLevel, RiskTier
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import CommunityProfile, TokenIdentity

# Rating bands over the overall community score (Part 12, Section 5).
_RATING_BANDS = (
    (85.0, CommunityRating.EXCELLENT),
    (70.0, CommunityRating.STRONG),
    (50.0, CommunityRating.AVERAGE),
    (0.0, CommunityRating.WEAK),
)

# A community can decline without being fake; growth below zero maps into
# the 0-30 signal range rather than a fake-community flag.
_GROWTH_FLOOR_SIGNAL = 30.0


@dataclass(frozen=True)
class CommunityAssessment:
    """Community verdict for one token (report format per Part 5, Section 13)."""

    token: TokenIdentity
    source: str
    sub_scores: dict[str, float | None]
    overall_score: float
    rating: CommunityRating
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float

    @property
    def is_artificial(self) -> bool:
        return self.rating is CommunityRating.ARTIFICIAL

    def summary(self) -> str:
        lines = [
            f"Community assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100 [{self.rating.value}]  "
            f"confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(
                f"  NOTE: only {self.coverage:.0%} of community categories have data; "
                "treat as an incomplete picture"
            )
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>16}: {rendered}")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        return "\n".join(lines)


class CommunityAnalyzer:
    """Scores community strength per the Part 5 framework."""

    def __init__(self, thresholds: CommunityThresholds, weights: CommunitySubWeights):
        self._t = thresholds
        self._w = weights
        self._logger = get_logger("analyzers.community")

    def assess(self, profile: CommunityProfile) -> CommunityAssessment:
        parts = [
            self._assess_engagement(profile),
            self._assess_growth(profile),
            self._assess_loyalty(profile),
            self._assess_creativity(profile),
            self._assess_dev_relationship(profile),
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
                f"no community data available for {profile.token.address} on {profile.token.chain}"
            )

        overall = weighted_sum / available_weight
        artificial = any(f.severity is RiskTier.DESTRUCTIVE for f in findings)
        if artificial:
            overall = 0.0  # fake community => red-flag override (Part 10, Section 5)

        rating = CommunityRating.ARTIFICIAL if artificial else self._rating(overall)
        confidence = confidence_from_facts(sum(p.known_count for p in parts), len(unknowns))

        self._logger.info(
            "community assessment %s/%s: score=%.0f rating=%s findings=%d unknown=%d",
            profile.token.chain, profile.token.address, overall,
            rating.value, len(findings), len(unknowns),
        )

        return CommunityAssessment(
            token=profile.token,
            source=profile.source,
            sub_scores=sub_scores,
            overall_score=overall,
            rating=rating,
            confidence=confidence,
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=available_weight,
        )

    # ---- Engagement: are people actually interacting? (Part 5 Sections 6-7) ----

    def _assess_engagement(self, p: CommunityProfile) -> SubScore:
        s = SubScore("engagement")

        if s.observe("twitter_engagement_rate_percent", p.twitter_engagement_rate_percent):
            s.signal(scale(p.twitter_engagement_rate_percent, 0.0,
                           self._t.excellent_engagement_rate_percent))
            # Large following + near-zero engagement is the classic bought-
            # followers pattern (Part 5 Section 10).
            if (
                p.twitter_followers is not None
                and p.twitter_followers >= self._t.min_followers_for_fake_check
                and p.twitter_engagement_rate_percent < self._t.fake_engagement_rate_percent
            ):
                s.deduct(40, RiskTier.SERIOUS_WARNING,
                         f"{p.twitter_followers:,} followers but "
                         f"{p.twitter_engagement_rate_percent:.2f}% engagement: "
                         "follower count likely inflated")

        # Observe the derived ratio, not the raw count: active members without
        # a total membership is uncomputable and must stay unknown (Rule 8).
        telegram_active_percent = None
        if p.telegram_active_members is not None and p.telegram_members:
            telegram_active_percent = 100.0 * p.telegram_active_members / p.telegram_members
        if s.observe("telegram_active_percent", telegram_active_percent):
            s.signal(scale(telegram_active_percent, 0.0, self._t.telegram_active_target_percent))

        if s.observe("discord_active_percent", p.discord_active_percent):
            s.signal(scale(p.discord_active_percent, 0.0, self._t.telegram_active_target_percent))

        if s.observe("duplicate_message_percent", p.duplicate_message_percent):
            if p.duplicate_message_percent > self._t.duplicate_message_warn_percent:
                s.deduct(40, RiskTier.SERIOUS_WARNING,
                         f"{p.duplicate_message_percent:.0f}% of messages are near-identical: "
                         "engagement looks scripted")

        return s

    # ---- Growth: is the community expanding naturally? ----

    def _assess_growth(self, p: CommunityProfile) -> SubScore:
        s = SubScore("growth")

        if s.observe("twitter_growth_rate_7d_percent", p.twitter_growth_rate_7d_percent):
            rate = p.twitter_growth_rate_7d_percent
            if rate >= 0:
                s.signal(_GROWTH_FLOOR_SIGNAL + scale(rate, 0.0, self._t.target_growth_rate_7d_percent)
                         * (100.0 - _GROWTH_FLOOR_SIGNAL) / 100.0)
            else:
                s.signal(max(0.0, _GROWTH_FLOOR_SIGNAL + rate))  # decline erodes the floor

        if s.observe("bot_follower_percent", p.bot_follower_percent):
            if p.bot_follower_percent >= self._t.bot_follower_artificial_percent:
                s.flag_destructive(
                    f"{p.bot_follower_percent:.0f}% bot followers: fake community"
                )
            elif p.bot_follower_percent >= self._t.bot_follower_warn_percent:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         f"{p.bot_follower_percent:.0f}% of followers appear to be bots")

        return s

    # ---- Loyalty: do members stay and believe? ----

    def _assess_loyalty(self, p: CommunityProfile) -> SubScore:
        s = SubScore("loyalty")
        if s.observe("member_retention_30d_percent", p.member_retention_30d_percent):
            s.signal(p.member_retention_30d_percent)
        if s.observe("positive_sentiment_percent", p.positive_sentiment_percent):
            s.signal(p.positive_sentiment_percent)
        return s

    # ---- Creativity: are users making memes and content? ----

    def _assess_creativity(self, p: CommunityProfile) -> SubScore:
        s = SubScore("creativity")
        if s.observe("user_content_per_day", p.user_content_per_day):
            s.signal(scale(p.user_content_per_day, 0.0, self._t.target_user_content_per_day))
        if s.observe("telegram_admin_only_talk", p.telegram_admin_only_talk):
            if p.telegram_admin_only_talk:
                s.deduct(40, RiskTier.SERIOUS_WARNING,
                         "only admins are talking: no organic community conversation")
            else:
                s.signal(100.0)
        return s

    # ---- Developer relationship: does the team maintain trust? ----

    def _assess_dev_relationship(self, p: CommunityProfile) -> SubScore:
        s = SubScore("dev_relationship")
        if s.observe("dev_updates_per_week", p.dev_updates_per_week):
            s.signal(scale(p.dev_updates_per_week, 0.0, self._t.target_dev_updates_per_week))
        if s.observe("dev_responds_to_community", p.dev_responds_to_community):
            s.signal(100.0 if p.dev_responds_to_community else 20.0)
        if s.observe("dev_appears_only_on_pumps", p.dev_appears_only_on_pumps):
            if p.dev_appears_only_on_pumps:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         "developers only appear during price pumps")
        return s

    @staticmethod
    def _rating(score: float) -> CommunityRating:
        for minimum, rating in _RATING_BANDS:
            if score >= minimum:
                return rating
        return CommunityRating.WEAK
