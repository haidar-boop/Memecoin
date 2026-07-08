"""Tests for the community intelligence engine (Spec Part 5)."""

import pytest

from meme_intelligence.analyzers.community_analyzer import CommunityAnalyzer
from meme_intelligence.config.settings import CommunitySubWeights, CommunityThresholds
from meme_intelligence.core.enums import CommunityRating, ConfidenceLevel
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import CommunityProfile, TokenIdentity

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_analyzer() -> CommunityAnalyzer:
    return CommunityAnalyzer(CommunityThresholds(), CommunitySubWeights())


def healthy_profile(**overrides) -> CommunityProfile:
    defaults = dict(
        token=TOKEN,
        source="test",
        twitter_followers=25000,
        twitter_engagement_rate_percent=6.0,
        twitter_growth_rate_7d_percent=40.0,
        bot_follower_percent=5.0,
        telegram_members=8000,
        telegram_active_members=1600,
        telegram_admin_only_talk=False,
        duplicate_message_percent=2.0,
        discord_members=3000,
        discord_active_percent=18.0,
        member_retention_30d_percent=85.0,
        positive_sentiment_percent=75.0,
        user_content_per_day=30.0,
        dev_updates_per_week=4.0,
        dev_responds_to_community=True,
        dev_appears_only_on_pumps=False,
    )
    defaults.update(overrides)
    return CommunityProfile(**defaults)


def test_healthy_community_scores_high():
    assessment = make_analyzer().assess(healthy_profile())
    assert assessment.overall_score >= 85
    assert assessment.rating is CommunityRating.EXCELLENT
    assert not assessment.is_artificial
    assert assessment.coverage == pytest.approx(1.0)


def test_majority_bot_followers_forces_artificial():
    """Fake community is a red-flag override (Part 10 Section 5)."""
    assessment = make_analyzer().assess(healthy_profile(bot_follower_percent=60.0))
    assert assessment.rating is CommunityRating.ARTIFICIAL
    assert assessment.overall_score == 0.0
    assert assessment.is_artificial


def test_big_following_with_dead_engagement_flagged():
    assessment = make_analyzer().assess(
        healthy_profile(twitter_followers=100000, twitter_engagement_rate_percent=0.1)
    )
    assert any("inflated" in f.message for f in assessment.findings)
    assert assessment.overall_score < 85


def test_large_following_alone_does_not_outscore_small_active_community():
    """Part 5 doctrine: 100k dead followers < 5k active supporters."""
    big_dead = make_analyzer().assess(
        healthy_profile(twitter_followers=100000, twitter_engagement_rate_percent=0.1,
                        telegram_members=50000, telegram_active_members=250)
    )
    small_active = make_analyzer().assess(
        healthy_profile(twitter_followers=5000, twitter_engagement_rate_percent=8.0,
                        telegram_members=800, telegram_active_members=200)
    )
    assert small_active.overall_score > big_dead.overall_score


def test_admin_only_telegram_penalized():
    assessment = make_analyzer().assess(healthy_profile(telegram_admin_only_talk=True))
    assert any("only admins" in f.message for f in assessment.findings)


def test_duplicate_messages_penalized():
    assessment = make_analyzer().assess(healthy_profile(duplicate_message_percent=40.0))
    assert any("scripted" in f.message for f in assessment.findings)


def test_dev_only_on_pumps_penalized():
    good = make_analyzer().assess(healthy_profile())
    flaky = make_analyzer().assess(healthy_profile(dev_appears_only_on_pumps=True))
    assert flaky.sub_scores["dev_relationship"] < good.sub_scores["dev_relationship"]


def test_declining_growth_scores_low_but_not_artificial():
    assessment = make_analyzer().assess(healthy_profile(twitter_growth_rate_7d_percent=-20.0))
    assert assessment.sub_scores["growth"] < 50
    assert not assessment.is_artificial


def test_partial_profile_reports_coverage_and_low_confidence():
    sparse = CommunityProfile(token=TOKEN, source="test", twitter_followers=1000,
                              twitter_engagement_rate_percent=3.0)
    assessment = make_analyzer().assess(sparse)
    assert assessment.coverage < 1.0
    assert assessment.confidence is ConfidenceLevel.LOW
    assert assessment.sub_scores["loyalty"] is None
    assert "partial" not in assessment.rating.value  # rating stays a clean enum


def test_no_data_raises():
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(CommunityProfile(token=TOKEN, source="test"))


def test_summary_renders():
    text = make_analyzer().assess(healthy_profile()).summary()
    assert "MEME" in text and "Overall" in text
