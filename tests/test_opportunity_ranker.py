"""Tests for the watchlist opportunity ranker (Spec Part 28, Sections 5-6)."""

import pytest

from meme_intelligence.analyzers.opportunity_ranker import OpportunityRanker
from meme_intelligence.config.settings import OpportunityWeights
from meme_intelligence.core.models import CategoryScores


def make_ranker() -> OpportunityRanker:
    return OpportunityRanker(OpportunityWeights())


def full_scores(**overrides) -> CategoryScores:
    defaults = dict(foundation=70.0, security=90.0, community=60.0,
                    blockchain=80.0, momentum=75.0, narrative=85.0, timing=65.0)
    defaults.update(overrides)
    return CategoryScores(**defaults)


def test_weights_match_part_28_section_5():
    w = OpportunityWeights()
    assert (w.growth_potential, w.momentum, w.foundation, w.risk, w.timing) == (
        0.30, 0.25, 0.20, 0.15, 0.10)
    assert w.growth_potential + w.momentum + w.foundation + w.risk + w.timing == pytest.approx(1.0)


def test_full_ranking_applies_section_5_weights():
    # narrative=85 (growth), momentum=75, foundation=70, timing=65, risk_score=40
    # -> risk component = 100 - 40 = 60
    rank = make_ranker().rank(full_scores(), risk_score=40.0)
    expected = (85 * 0.30 + 75 * 0.25 + 70 * 0.20 + 60 * 0.15 + 65 * 0.10)
    assert rank.score == pytest.approx(expected, abs=0.1)
    assert rank.coverage == pytest.approx(1.0)
    assert rank.components["growth_potential"] == 85.0
    assert rank.components["risk"] == 60.0


def test_risk_is_inverted_lower_risk_scores_higher():
    low_risk = make_ranker().rank(full_scores(), risk_score=10.0)
    high_risk = make_ranker().rank(full_scores(), risk_score=90.0)
    assert low_risk.score > high_risk.score  # less risk = more opportunity


def test_missing_factor_excluded_and_renormalized_not_zeroed():
    """Rule 8: a None factor drops out and the remaining weights
    renormalize — it is never scored as 0, which would fabricate weakness."""
    scores = full_scores(narrative=None)  # no growth-potential data
    rank = make_ranker().rank(scores, risk_score=40.0)
    assert rank.components["growth_potential"] is None
    assert rank.coverage == pytest.approx(0.70)  # 1.0 - 0.30 growth weight
    # score is the weighted average over the AVAILABLE factors only
    avail = (75 * 0.25 + 70 * 0.20 + 60 * 0.15 + 65 * 0.10) / 0.70
    assert rank.score == pytest.approx(avail, abs=0.1)


def test_no_data_at_all_scores_zero_with_zero_coverage():
    rank = make_ranker().rank(
        CategoryScores(narrative=None, momentum=None, foundation=None, timing=None),
        risk_score=None)
    assert rank.score == 0.0
    assert rank.coverage == 0.0


def test_missing_risk_only_drops_the_risk_term():
    rank = make_ranker().rank(full_scores(), risk_score=None)
    assert rank.components["risk"] is None
    assert rank.coverage == pytest.approx(0.85)  # 1.0 - 0.15 risk weight


def test_summary_is_readable():
    rank = make_ranker().rank(full_scores(), risk_score=40.0)
    text = rank.summary()
    assert "opportunity" in text
    assert "growth_potential" in text
