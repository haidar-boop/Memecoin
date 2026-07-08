"""Tests for the security analysis engine (Spec Parts 4/18/33)."""

import pytest

from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.config.settings import SecuritySubWeights, SecurityThresholds
from meme_intelligence.core.enums import ConfidenceLevel, RiskTier
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_analyzer() -> SecurityAnalyzer:
    return SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights())


def clean_profile(**overrides) -> SecurityProfile:
    """A fully-known, healthy profile; tests override individual facts."""
    defaults = dict(
        token=TOKEN,
        source="goplus",
        is_honeypot=False,
        cannot_buy=False,
        cannot_sell_all=False,
        is_open_source=True,
        is_proxy=False,
        is_mintable=False,
        ownership_renounced=True,
        hidden_owner=False,
        can_take_back_ownership=False,
        has_blacklist=False,
        trading_pausable=False,
        is_freezable=False,
        balance_mutable=False,
        selfdestruct=False,
        buy_tax_percent=0.0,
        sell_tax_percent=0.0,
        tax_modifiable=False,
        fake_token=False,
        is_airdrop_scam=False,
        anti_whale_modifiable=False,
        slippage_modifiable=False,
        personal_slippage_modifiable=False,
        trading_cooldown=False,
        honeypot_same_creator_count=0,
        holder_count=2000,
        top_holder_percent=3.0,
        top10_holder_percent=20.0,
        creator_percent=2.0,
        owner_percent=0.0,
        lp_locked_percent=95.0,
    )
    defaults.update(overrides)
    return SecurityProfile(**defaults)


def healthy_market() -> DexPair:
    return DexPair(chain="solana", pair_address="Pair1", base_token=TOKEN, liquidity_usd=80000.0)


def test_clean_token_scores_high():
    assessment = make_analyzer().assess(clean_profile(), healthy_market())
    assert assessment.overall_score >= 90
    assert assessment.band == "Excellent"
    assert assessment.tier is RiskTier.ACCEPTABLE_UNCERTAINTY
    assert not assessment.is_destructive
    assert assessment.coverage == pytest.approx(1.0)


def test_honeypot_forces_zero_and_destructive_tier():
    assessment = make_analyzer().assess(clean_profile(is_honeypot=True), healthy_market())
    assert assessment.overall_score == 0.0
    assert assessment.tier is RiskTier.DESTRUCTIVE
    assert any("honeypot" in f.message for f in assessment.destructive_findings)


def test_non_sellable_token_is_destructive():
    assessment = make_analyzer().assess(clean_profile(cannot_sell_all=True), healthy_market())
    assert assessment.tier is RiskTier.DESTRUCTIVE


def test_fake_token_is_destructive():
    assessment = make_analyzer().assess(clean_profile(fake_token=True), healthy_market())
    assert assessment.tier is RiskTier.DESTRUCTIVE


def test_mint_authority_is_serious_not_fatal():
    assessment = make_analyzer().assess(clean_profile(is_mintable=True), healthy_market())
    assert assessment.tier is RiskTier.SERIOUS_WARNING
    assert 0 < assessment.overall_score < 100
    assert assessment.sub_scores["contract"] == pytest.approx(75.0)


def test_extreme_sell_tax_penalized_heavily():
    mild = make_analyzer().assess(clean_profile(sell_tax_percent=12.0), healthy_market())
    extreme = make_analyzer().assess(clean_profile(sell_tax_percent=40.0), healthy_market())
    assert extreme.overall_score < mild.overall_score < 100


def test_unlocked_lp_is_serious_warning():
    assessment = make_analyzer().assess(clean_profile(lp_locked_percent=10.0), healthy_market())
    assert assessment.tier is RiskTier.SERIOUS_WARNING
    assert any("liquidity can be pulled" in f.message for f in assessment.findings)


def test_low_liquidity_penalized():
    thin = DexPair(chain="solana", pair_address="P", base_token=TOKEN, liquidity_usd=1000.0)
    assessment = make_analyzer().assess(clean_profile(), thin)
    assert assessment.sub_scores["liquidity"] < 70


def test_concentration_penalties_scale():
    warn = make_analyzer().assess(clean_profile(top_holder_percent=15.0), healthy_market())
    severe = make_analyzer().assess(clean_profile(top_holder_percent=40.0), healthy_market())
    assert severe.overall_score < warn.overall_score


def test_creator_honeypot_history_penalized():
    assessment = make_analyzer().assess(
        clean_profile(honeypot_same_creator_count=3), healthy_market()
    )
    assert assessment.sub_scores["developer"] == pytest.approx(60.0)
    assert assessment.tier is RiskTier.SERIOUS_WARNING


def test_missing_market_data_leaves_liquidity_partial():
    """Without market data, LP lock facts still score liquidity, at lower coverage confidence."""
    profile = clean_profile()
    assessment = make_analyzer().assess(profile, market=None)
    assert assessment.sub_scores["liquidity"] is not None  # lp_locked_percent still known
    assert "liquidity_usd" in assessment.unknown_fields


def test_mostly_unknown_profile_has_low_confidence_and_partial_coverage():
    sparse = SecurityProfile(token=TOKEN, source="goplus", is_mintable=False)
    assessment = make_analyzer().assess(sparse, market=None)
    assert assessment.confidence is ConfidenceLevel.LOW
    assert assessment.coverage < 1.0
    assert assessment.sub_scores["distribution"] is None


def test_all_unknown_raises_insufficient_data():
    empty = SecurityProfile(token=TOKEN, source="goplus")
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(empty, market=None)


def test_unknowns_never_treated_as_safe():
    """A token with unknown honeypot status must not outscore one confirmed safe."""
    confirmed = make_analyzer().assess(clean_profile(), healthy_market())
    unknown = make_analyzer().assess(clean_profile(is_honeypot=None), healthy_market())
    assert "is_honeypot" in unknown.unknown_fields
    assert unknown.overall_score <= confirmed.overall_score


def test_summary_renders():
    text = make_analyzer().assess(clean_profile(), healthy_market()).summary()
    assert "MEME" in text and "Overall" in text
    assert "NOT safe" not in text  # full coverage needs no warning


def test_summary_warns_on_partial_coverage():
    sparse = SecurityProfile(token=TOKEN, source="goplus", is_mintable=False)
    text = make_analyzer().assess(sparse, market=None).summary()
    assert "partial data" in text
    assert "NOT safe" in text
