"""Tests for the on-chain intelligence engine (Spec Part 6)."""

import pytest

from meme_intelligence.analyzers.onchain_analyzer import (
    OnChainAnalyzer,
    derive_onchain_profile,
)
from meme_intelligence.config.settings import OnChainSubWeights, OnChainThresholds
from meme_intelligence.core.enums import ConfidenceLevel, MarketPhase
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import (
    DexPair,
    OnChainProfile,
    SecurityProfile,
    TokenIdentity,
)

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_analyzer() -> OnChainAnalyzer:
    return OnChainAnalyzer(OnChainThresholds(), OnChainSubWeights())


def healthy_profile(**overrides) -> OnChainProfile:
    defaults = dict(
        token=TOKEN,
        source="test",
        holder_count=3000,
        top_holder_percent=2.5,
        top10_holder_percent=18.0,
        creator_percent=1.0,
        owner_percent=0.5,
        buys_24h=400,
        sells_24h=250,
        unique_buyers_24h=300,
        unique_sellers_24h=180,
        volume_24h_usd=120000.0,
        liquidity_usd=90000.0,
        price_change_24h_percent=5.0,
    )
    defaults.update(overrides)
    return OnChainProfile(**defaults)


def test_healthy_profile_scores_well():
    assessment = make_analyzer().assess(healthy_profile())
    assert assessment.overall_score >= 80
    # smart money / whales / token flow have no data yet -> partial coverage
    assert assessment.coverage == pytest.approx(0.50)
    assert assessment.sub_scores["smart_money"] is None
    assert assessment.sub_scores["token_flow"] is None


def test_wash_trading_detected():
    """Many trades from few wallets = artificial volume (Part 6 Section 12)."""
    washy = healthy_profile(buys_24h=3000, sells_24h=2500,
                            unique_buyers_24h=200, unique_sellers_24h=180)
    assessment = make_analyzer().assess(washy)
    assert any("wash trading" in f.message for f in assessment.findings)
    assert assessment.sub_scores["volume_quality"] < 50


def test_volume_disproportionate_to_holders_flagged():
    pumped = healthy_profile(holder_count=60, volume_24h_usd=500000.0)
    assessment = make_analyzer().assess(pumped)
    assert any("without matching holder base" in f.message for f in assessment.findings)


def test_high_volume_alone_is_not_rewarded():
    """Part 6 final rule: never assume high volume means demand."""
    modest = make_analyzer().assess(healthy_profile(volume_24h_usd=50000.0))
    inflated = make_analyzer().assess(
        healthy_profile(volume_24h_usd=5000000.0, unique_buyers_24h=30, unique_sellers_24h=20,
                        buys_24h=2000, sells_24h=1800)
    )
    assert inflated.sub_scores["volume_quality"] < modest.sub_scores["volume_quality"]


def test_concentrated_holders_lower_health():
    healthy = make_analyzer().assess(healthy_profile())
    concentrated = make_analyzer().assess(
        healthy_profile(top_holder_percent=18.0, top10_holder_percent=65.0)
    )
    assert concentrated.sub_scores["holder_health"] < healthy.sub_scores["holder_health"]


def test_holder_growth_rewarded_and_decline_penalized():
    growing = make_analyzer().assess(healthy_profile(holder_count_24h_ago=2400))
    shrinking = make_analyzer().assess(healthy_profile(holder_count_24h_ago=3600))
    assert growing.sub_scores["holder_health"] > shrinking.sub_scores["holder_health"]
    assert any("shrank" in f.message for f in shrinking.findings)


def test_smart_money_selling_penalized_when_data_available():
    selling = healthy_profile(smart_wallet_count=4, smart_wallet_net_flow_usd=-50000.0)
    assessment = make_analyzer().assess(selling)
    assert any("net sellers" in f.message for f in assessment.findings)
    assert assessment.coverage > 0.50  # smart-money category now has data


def test_phase_classification():
    analyzer = make_analyzer()
    expansion = healthy_profile(price_change_24h_percent=45.0, buys_24h=700, sells_24h=300)
    accumulation = healthy_profile(price_change_24h_percent=2.0, buys_24h=300, sells_24h=250)
    distribution = healthy_profile(price_change_24h_percent=-15.0, buys_24h=200, sells_24h=500)
    unknown = healthy_profile(price_change_24h_percent=None)

    assert analyzer.assess(expansion).phase is MarketPhase.EXPANSION
    assert analyzer.assess(accumulation).phase is MarketPhase.ACCUMULATION
    assert analyzer.assess(distribution).phase is MarketPhase.DISTRIBUTION
    assert analyzer.assess(unknown).phase is MarketPhase.UNCLEAR


def test_derive_profile_from_market_and_security():
    pair = DexPair(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        liquidity_usd=50000.0, volume_24h=80000.0,
        buys_24h=150, sells_24h=90, buyers_24h=120, sellers_24h=70,
        price_change_24h=12.0,
    )
    security = SecurityProfile(
        token=TOKEN, source="goplus",
        holder_count=1200, top_holder_percent=4.0, top10_holder_percent=25.0,
        creator_percent=2.0,
    )
    profile = derive_onchain_profile(pair, security)
    assert profile.holder_count == 1200
    assert profile.unique_buyers_24h == 120
    assert profile.volume_24h_usd == 80000.0
    assert profile.source == "derived:market+security"

    assessment = make_analyzer().assess(profile)
    assert assessment.overall_score > 0
    assert assessment.confidence in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM)


def test_derive_without_security_still_works():
    pair = DexPair(chain="solana", pair_address="Pool1", base_token=TOKEN,
                   volume_24h=10000.0, buys_24h=50, sells_24h=30,
                   buyers_24h=40, sellers_24h=25, liquidity_usd=20000.0)
    profile = derive_onchain_profile(pair)
    assert profile.source == "derived:market"
    assessment = make_analyzer().assess(profile)
    assert assessment.sub_scores["holder_health"] is None
    assert assessment.sub_scores["volume_quality"] is not None


def test_no_data_raises():
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(OnChainProfile(token=TOKEN, source="test"))
