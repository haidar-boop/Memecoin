"""Tests for the momentum & market-timing engine (Spec Parts 14/26)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.analyzers.momentum_analyzer import MomentumAnalyzer
from meme_intelligence.config.settings import MomentumSubWeights, MomentumThresholds
from meme_intelligence.core.enums import EntryZone, PreferredAction
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import DexPair, TokenIdentity

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_analyzer() -> MomentumAnalyzer:
    return MomentumAnalyzer(MomentumThresholds(), MomentumSubWeights(), now_func=lambda: NOW)


def make_pair(
    change_24h=15.0, change_6h=8.0, change_1h=2.0,
    volume_24h=120_000.0, volume_1h=8_000.0,
    buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
    age_hours=3.0,
) -> DexPair:
    return DexPair(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        price_change_24h=change_24h, price_change_6h=change_6h, price_change_1h=change_1h,
        volume_24h=volume_24h, volume_1h=volume_1h,
        buys_24h=buys_24h, sells_24h=sells_24h, buys_1h=buys_1h, sells_1h=sells_1h,
        pair_created_at=NOW - timedelta(hours=age_hours) if age_hours is not None else None,
    )


def test_healthy_accelerating_momentum_scores_high():
    # 1h volume x24 = 192k vs 120k baseline -> accelerating; buyers accelerating too
    assessment = make_analyzer().assess(make_pair())
    assert assessment.overall_score >= 70
    assert assessment.entry_zone is EntryZone.EARLY
    assert assessment.preferred_action is PreferredAction.CONSIDER_RESEARCH_ENTRY
    assert assessment.sub_scores["social"] is None  # collectors pending
    assert assessment.coverage == pytest.approx(0.75)


def test_price_alone_is_not_momentum():
    """Part 26 Section 1: a price pump with fading volume must not score high."""
    pumping_fading = make_pair(
        change_24h=60.0, change_6h=40.0, change_1h=25.0,
        volume_24h=200_000.0, volume_1h=1_000.0,   # hourly rate collapsed
        buys_1h=5, sells_1h=20,                     # buyers gone
    )
    assessment = make_analyzer().assess(pumping_fading)
    assert assessment.sub_scores["volume"] <= 35
    assert assessment.sub_scores["onchain"] <= 40
    assert assessment.overall_score < 70


def test_vertical_spike_penalized():
    calm = make_analyzer().assess(make_pair(change_1h=5.0))
    spiked = make_analyzer().assess(make_pair(change_1h=45.0))
    assert spiked.sub_scores["price"] < calm.sub_scores["price"]
    assert any("vertical" in f.message for f in spiked.findings)


def test_fading_trend_detected():
    fading = make_pair(change_24h=30.0, change_6h=5.0, change_1h=-8.0)
    consistent = make_pair(change_24h=30.0, change_6h=10.0, change_1h=3.0)
    a_fading = make_analyzer().assess(fading)
    a_consistent = make_analyzer().assess(consistent)
    assert a_fading.sub_scores["price"] < a_consistent.sub_scores["price"]


def test_late_zone_on_extreme_extension():
    extended = make_pair(change_24h=250.0, age_hours=10.0)
    assessment = make_analyzer().assess(extended)
    assert assessment.entry_zone is EntryZone.LATE
    assert assessment.preferred_action is PreferredAction.MONITOR


def test_older_token_with_momentum_is_confirmation_zone():
    older = make_pair(age_hours=72.0)
    assessment = make_analyzer().assess(older)
    assert assessment.entry_zone is EntryZone.CONFIRMATION


def test_weak_momentum_actions():
    dead = make_pair(change_24h=-30.0, change_6h=-10.0, change_1h=-5.0,
                     volume_24h=50_000.0, volume_1h=200.0,
                     buys_1h=2, sells_1h=15, age_hours=80.0)
    assessment = make_analyzer().assess(dead)
    assert assessment.preferred_action in (PreferredAction.AVOID, PreferredAction.MONITOR)


def test_social_lens_scores_when_provided():
    assessment = make_analyzer().assess(make_pair(), social_growth_7d_percent=40.0)
    assert assessment.sub_scores["social"] == 100.0
    assert assessment.coverage == pytest.approx(1.0)


def test_no_data_raises():
    empty = DexPair(chain="solana", pair_address="P", base_token=TOKEN)
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(empty)


def test_summary_renders():
    text = make_analyzer().assess(make_pair()).summary()
    assert "Momentum assessment" in text and "zone=" in text
