"""Tests for the token structure engine (Spec Part 7)."""

import pytest

from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer, competition_score
from meme_intelligence.config.settings import TokenSubWeights, TokenThresholds
from meme_intelligence.core.enums import MarketCapStage, ValuationClassification
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_analyzer() -> TokenAnalyzer:
    return TokenAnalyzer(TokenThresholds(), TokenSubWeights())


def make_pair(
    market_cap=500_000.0,
    fdv=550_000.0,
    liquidity=40_000.0,
    volume=150_000.0,
) -> DexPair:
    return DexPair(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=market_cap, fdv=fdv, liquidity_usd=liquidity, volume_24h=volume,
    )


def security_with_holders(top10=25.0) -> SecurityProfile:
    return SecurityProfile(token=TOKEN, source="goplus", top10_holder_percent=top10)


def test_stage_classification():
    analyzer = make_analyzer()
    assert analyzer.assess(make_pair(market_cap=200_000)).stage is MarketCapStage.EARLY
    assert analyzer.assess(make_pair(market_cap=5_000_000, fdv=5_000_000)).stage is MarketCapStage.GROWTH
    assert analyzer.assess(make_pair(market_cap=500_000_000, fdv=500_000_000)).stage is MarketCapStage.MATURE


def test_no_valuation_anchor_raises():
    """Without any market-cap anchor, structure analysis is impossible —
    the analyzer refuses rather than guessing (Rule 8)."""
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(make_pair(market_cap=None, fdv=None, volume=1000))


def test_fdv_fallback_when_mcap_missing():
    assessment = make_analyzer().assess(make_pair(market_cap=None, fdv=800_000))
    assert assessment.stage is MarketCapStage.EARLY


def test_severe_dilution_flagged_and_overvalued():
    diluted = make_pair(market_cap=1_000_000, fdv=5_000_000)
    assessment = make_analyzer().assess(diluted)
    assert any("supply overhang" in f.message for f in assessment.findings)
    assert assessment.valuation is ValuationClassification.OVERVALUED


def test_early_with_demand_and_depth_is_undervalued():
    strong = make_pair(market_cap=500_000, fdv=520_000, liquidity=40_000, volume=150_000)
    assessment = make_analyzer().assess(strong)
    assert assessment.valuation is ValuationClassification.UNDERVALUED


def test_mature_without_demand_is_expensive():
    stale = make_pair(market_cap=500_000_000, fdv=500_000_000,
                      liquidity=10_000_000, volume=2_000_000)  # 0.4% volume/mcap
    assessment = make_analyzer().assess(stale)
    assert assessment.valuation is ValuationClassification.EXPENSIVE


def test_thin_liquidity_ratio_penalized():
    thin = make_pair(market_cap=10_000_000, fdv=10_000_000, liquidity=50_000)  # 0.5%
    assessment = make_analyzer().assess(thin)
    assert any("move price violently" in f.message for f in assessment.findings)
    assert assessment.sub_scores["liquidity"] < 50


def test_excessive_churn_flagged():
    churny = make_pair(market_cap=100_000, fdv=100_000, volume=1_000_000)  # 1000% churn
    assessment = make_analyzer().assess(churny)
    assert any("rarely organic" in f.message for f in assessment.findings)


def test_supply_uses_security_concentration():
    spread = make_analyzer().assess(make_pair(), security_with_holders(top10=22.0))
    packed = make_analyzer().assess(make_pair(), security_with_holders(top10=65.0))
    assert spread.sub_scores["supply"] > packed.sub_scores["supply"]


def test_qualitative_slots_default_unknown():
    assessment = make_analyzer().assess(make_pair())
    assert assessment.sub_scores["competition"] is None
    assert assessment.sub_scores["catalysts"] is None
    assert assessment.coverage < 1.0


def test_qualitative_slots_accepted_and_validated():
    assessment = make_analyzer().assess(make_pair(), competition=70, catalysts=60)
    assert assessment.sub_scores["competition"] == 70
    # circulating fraction is known from mcap/FDV, so all six categories have data
    assert assessment.coverage == pytest.approx(1.0)
    assert "top10_holder_percent" in assessment.unknown_fields
    with pytest.raises(ValueError):
        make_analyzer().assess(make_pair(), competition=120)


def test_competition_score_percentile():
    pair = make_pair(liquidity=50_000, volume=100_000)
    weaker = make_pair(liquidity=10_000, volume=5_000)
    stronger = make_pair(liquidity=500_000, volume=900_000)
    assert competition_score(pair, [weaker, stronger]) == pytest.approx(50.0)
    assert competition_score(pair, [weaker]) == pytest.approx(100.0)
    assert competition_score(pair, []) is None


def test_no_data_raises():
    empty = DexPair(chain="solana", pair_address="P", base_token=TOKEN)
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(empty)


def test_summary_renders():
    text = make_analyzer().assess(make_pair()).summary()
    assert "MEME" in text and "stage=" in text
