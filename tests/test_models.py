"""Tests for score aggregation and classification banding (Spec Parts 10/20/31)."""

import pytest

from meme_intelligence.config.settings import ClassificationBands, ScoringWeights
from meme_intelligence.core.enums import Classification
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import CategoryScores, classify, compute_weighted_score

WEIGHTS = ScoringWeights()
BANDS = ClassificationBands()


def test_full_coverage_weighted_score():
    scores = CategoryScores(
        foundation=80, security=90, community=70, blockchain=75,
        momentum=60, narrative=85, timing=50,
    )
    result = compute_weighted_score(scores, WEIGHTS)
    expected = (80 + 90 + 70 + 75 + 60 + 85) * 0.15 + 50 * 0.10
    assert result.total == pytest.approx(expected)
    assert result.coverage == pytest.approx(1.0)
    assert result.missing == ()


def test_partial_coverage_renormalizes_and_reports_missing():
    scores = CategoryScores(security=80, community=60)  # only 30% of weight available
    result = compute_weighted_score(scores, WEIGHTS)
    assert result.total == pytest.approx(70.0)  # equal weights renormalize to the mean
    assert result.coverage == pytest.approx(0.30)
    assert set(result.missing) == {"foundation", "blockchain", "momentum", "narrative", "timing"}


def test_no_data_raises_instead_of_fabricating():
    with pytest.raises(InsufficientDataError):
        compute_weighted_score(CategoryScores(), WEIGHTS)


def test_score_out_of_range_rejected():
    with pytest.raises(ValueError, match="0-100"):
        CategoryScores(security=101)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (95.0, Classification.ELITE_OPPORTUNITY),
        (90.0, Classification.ELITE_OPPORTUNITY),
        (85.0, Classification.STRONG_CANDIDATE),
        (75.0, Classification.WATCHLIST),
        (65.0, Classification.SPECULATIVE),
        (59.9, Classification.AVOID),
        (0.0, Classification.AVOID),
    ],
)
def test_classification_bands(score, expected):
    assert classify(score, BANDS) is expected
