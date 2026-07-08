"""Tests for the master scoring & decision engine (Spec Parts 10/31)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.analyzers.community_analyzer import CommunityAnalyzer
from meme_intelligence.analyzers.onchain_analyzer import OnChainAnalyzer, derive_onchain_profile
from meme_intelligence.analyzers.risk_analyzer import RiskAnalyzer
from meme_intelligence.analyzers.scoring_engine import ScoringEngine, derive_timing_score
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer
from meme_intelligence.config.settings import (
    ClassificationBands,
    CommunitySubWeights,
    CommunityThresholds,
    OnChainSubWeights,
    OnChainThresholds,
    RiskSubWeights,
    ScoringWeights,
    SecuritySubWeights,
    SecurityThresholds,
    TokenSubWeights,
    TokenThresholds,
)
from meme_intelligence.core.enums import Classification, MarketCapStage, MarketPhase
from meme_intelligence.core.models import (
    CommunityProfile,
    DexPair,
    SecurityProfile,
    TokenIdentity,
)

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_engine() -> ScoringEngine:
    return ScoringEngine(ScoringWeights(), ClassificationBands(), now_func=lambda: NOW)


def make_pair(**overrides) -> DexPair:
    defaults = dict(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=60_000.0,
        volume_24h=90_000.0, buys_24h=400, sells_24h=250,
        buyers_24h=300, sellers_24h=180, price_change_24h=5.0,
        pair_created_at=NOW - timedelta(hours=3),
    )
    defaults.update(overrides)
    return DexPair(**defaults)


def clean_profile(**overrides) -> SecurityProfile:
    defaults = dict(
        token=TOKEN, source="goplus",
        is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
        is_open_source=True, is_proxy=False, is_mintable=False,
        ownership_renounced=True, hidden_owner=False, can_take_back_ownership=False,
        has_blacklist=False, trading_pausable=False, is_freezable=False,
        balance_mutable=False, selfdestruct=False,
        buy_tax_percent=0.0, sell_tax_percent=0.0, tax_modifiable=False,
        fake_token=False, is_airdrop_scam=False, anti_whale_modifiable=False,
        slippage_modifiable=False, personal_slippage_modifiable=False,
        trading_cooldown=False, honeypot_same_creator_count=0,
        holder_count=2500, top_holder_percent=3.0, top10_holder_percent=22.0,
        creator_percent=1.5, owner_percent=0.0, lp_locked_percent=95.0,
    )
    defaults.update(overrides)
    return SecurityProfile(**defaults)


def healthy_community():
    profile = CommunityProfile(
        token=TOKEN, source="test",
        twitter_followers=25000, twitter_engagement_rate_percent=6.0,
        twitter_growth_rate_7d_percent=40.0, bot_follower_percent=5.0,
        telegram_members=8000, telegram_active_members=1600,
        telegram_admin_only_talk=False, duplicate_message_percent=2.0,
        member_retention_30d_percent=85.0, positive_sentiment_percent=75.0,
        user_content_per_day=30.0, dev_updates_per_week=4.0,
        dev_responds_to_community=True, dev_appears_only_on_pumps=False,
    )
    return CommunityAnalyzer(CommunityThresholds(), CommunitySubWeights()).assess(profile)


def build_inputs(security_overrides=None, pair_overrides=None):
    pair = make_pair(**(pair_overrides or {}))
    profile = clean_profile(**(security_overrides or {}))
    security = SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights()).assess(profile, pair)
    onchain = OnChainAnalyzer(OnChainThresholds(), OnChainSubWeights()).assess(
        derive_onchain_profile(pair, profile)
    )
    token = TokenAnalyzer(TokenThresholds(), TokenSubWeights()).assess(pair, profile)
    risk = RiskAnalyzer(RiskSubWeights()).assess(
        security, pair=pair, token=token, onchain=onchain
    )
    return pair, security, onchain, token, risk


def test_weighted_score_matches_manual_calculation():
    pair, security, onchain, token, _ = build_inputs()
    master = make_engine().evaluate(
        security, onchain=onchain, token_structure=token,
        momentum_score=70.0, narrative_score=80.0, timing_score=60.0,
    )
    w = ScoringWeights()
    expected_sum = (
        token.overall_score * w.foundation   # foundation input = token structure alone
        + security.overall_score * w.security
        + onchain.overall_score * w.blockchain
        + 70.0 * w.momentum + 80.0 * w.narrative + 60.0 * w.timing
    )
    available = w.foundation + w.security + w.blockchain + w.momentum + w.narrative + w.timing
    assert master.final_score == pytest.approx(expected_sum / available)
    assert master.coverage == pytest.approx(available)
    assert master.category_scores.community is None


def test_healthy_token_classifies_by_bands():
    pair, security, onchain, token, risk = build_inputs()
    master = make_engine().evaluate(
        security, community=healthy_community(), onchain=onchain,
        token_structure=token, risk=risk,
        momentum_score=85.0, narrative_score=85.0,
        timing_score=derive_timing_score(pair, token, onchain, now_func=lambda: NOW),
    )
    assert master.classification in (
        Classification.WATCHLIST, Classification.STRONG_CANDIDATE, Classification.ELITE_OPPORTUNITY,
    )
    assert not master.overrides
    assert len(master.decision_trace) == 6


def test_honeypot_override_forces_avoid():
    pair, security, onchain, token, risk = build_inputs({"is_honeypot": True})
    master = make_engine().evaluate(
        security, onchain=onchain, token_structure=token, risk=risk,
        momentum_score=95.0, narrative_score=95.0, timing_score=95.0,
    )
    assert master.classification is Classification.AVOID
    assert any("destructive security risk" in o for o in master.overrides)


def test_weak_liquidity_caps_at_speculative():
    pair, security, onchain, token, _ = build_inputs(
        {"lp_locked_percent": 5.0},
        {"liquidity_usd": 1000.0, "market_cap": 400_000.0},
    )
    master = make_engine().evaluate(
        security, community=healthy_community(), onchain=onchain, token_structure=token,
        momentum_score=95.0, narrative_score=95.0, timing_score=95.0,
    )
    order = [Classification.AVOID, Classification.SPECULATIVE, Classification.WATCHLIST,
             Classification.STRONG_CANDIDATE, Classification.ELITE_OPPORTUNITY]
    assert order.index(master.classification) <= order.index(Classification.SPECULATIVE)
    assert any("cap classification" in s.action for s in master.decision_trace)


def test_unknown_categories_recorded_not_failed():
    pair, security, onchain, token, _ = build_inputs()
    master = make_engine().evaluate(security, onchain=onchain, token_structure=token)
    unknowns = [s for s in master.decision_trace if s.answer == "unknown"]
    assert unknowns  # community, narrative, risk unknown
    assert master.classification is not Classification.AVOID


def test_foundation_input_averages_foundation_and_token():
    pair, security, onchain, token, _ = build_inputs()
    engine = make_engine()
    only_token = engine.evaluate(security, token_structure=token)
    assert only_token.category_scores.foundation == pytest.approx(token.overall_score)


def test_out_of_range_inputs_rejected():
    _, security, _, _, _ = build_inputs()
    with pytest.raises(ValueError):
        make_engine().evaluate(security, momentum_score=150.0)


def test_timing_derivation():
    pair, security, onchain, token, _ = build_inputs()
    score = derive_timing_score(pair, token, onchain, now_func=lambda: NOW)
    # fresh pair (90) + early stage (85) + accumulation phase (80) -> 85
    assert score == pytest.approx((90.0 + 85.0 + _phase_points(onchain.phase)) / 3)
    assert derive_timing_score(None, None, None) is None


def _phase_points(phase: MarketPhase) -> float:
    return {MarketPhase.ACCUMULATION: 80.0, MarketPhase.EXPANSION: 70.0,
            MarketPhase.UNCLEAR: 50.0, MarketPhase.DISTRIBUTION: 20.0}[phase]


def test_summary_renders_with_trace():
    pair, security, onchain, token, risk = build_inputs()
    text = make_engine().evaluate(
        security, onchain=onchain, token_structure=token, risk=risk,
    ).summary()
    assert "MASTER ASSESSMENT" in text
    assert "Decision trace" in text
    assert "Is the contract safe?" in text
