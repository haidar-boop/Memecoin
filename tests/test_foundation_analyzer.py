"""Tests for the foundation score combiner (Spec Part 5, Section 12)."""

import pytest

from meme_intelligence.analyzers.foundation_analyzer import (
    FoundationAnalyzer,
    FoundationInputs,
)
from meme_intelligence.config.settings import FoundationSubWeights
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import TokenIdentity

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_analyzer() -> FoundationAnalyzer:
    return FoundationAnalyzer(FoundationSubWeights())


def test_full_inputs_weighted_per_spec():
    inputs = FoundationInputs(
        meme_strength=90, narrative=80, brand=70, dev_communication=60, long_term=50,
    )
    assessment = make_analyzer().assess(TOKEN, inputs, community_quality=85)
    expected = 90 * 0.20 + 80 * 0.20 + 70 * 0.15 + 85 * 0.20 + 60 * 0.15 + 50 * 0.10
    assert assessment.overall_score == pytest.approx(expected)
    assert assessment.coverage == pytest.approx(1.0)
    assert assessment.missing == ()


def test_partial_inputs_renormalize_and_report_missing():
    inputs = FoundationInputs(meme_strength=80, narrative=80)
    assessment = make_analyzer().assess(TOKEN, inputs)
    assert assessment.overall_score == pytest.approx(80.0)
    assert assessment.coverage == pytest.approx(0.40)
    assert "community_quality" in assessment.missing
    assert "brand" in assessment.missing


def test_out_of_range_input_rejected():
    with pytest.raises(ValueError):
        FoundationInputs(meme_strength=150)
    with pytest.raises(ValueError):
        make_analyzer().assess(TOKEN, FoundationInputs(narrative=50), community_quality=101)


def test_no_inputs_raises():
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(TOKEN, FoundationInputs())


def test_summary_renders():
    text = make_analyzer().assess(TOKEN, FoundationInputs(meme_strength=75)).summary()
    assert "MEME" in text and "not assessed" in text
