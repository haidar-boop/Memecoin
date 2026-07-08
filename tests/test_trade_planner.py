"""Tests for the trade planning engine (Spec Part 8)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.analyzers.onchain_analyzer import OnChainAnalyzer, derive_onchain_profile
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer
from meme_intelligence.config.settings import (
    OnChainSubWeights,
    OnChainThresholds,
    SecuritySubWeights,
    SecurityThresholds,
    TokenSubWeights,
    TokenThresholds,
    TradeScoreWeights,
    TradingSettings,
)
from meme_intelligence.core.enums import (
    CheckStatus,
    ConvictionLevel,
    MarketRegime,
    SetupType,
)
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.scanners.discovery import TokenCandidate
from meme_intelligence.trading.trade_planner import TradePlanner

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_planner() -> TradePlanner:
    return TradePlanner(TradingSettings(), TradeScoreWeights(), now_func=lambda: NOW)


def make_pair(age_hours=2.0, **overrides) -> DexPair:
    defaults = dict(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=60_000.0,
        volume_24h=90_000.0, buys_24h=400, sells_24h=250,
        buyers_24h=300, sellers_24h=180, price_change_24h=5.0,
        pair_created_at=NOW - timedelta(hours=age_hours),
    )
    defaults.update(overrides)
    return DexPair(**defaults)


def clean_security_profile(**overrides) -> SecurityProfile:
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


def full_inputs(security_overrides=None, pair_overrides=None):
    """Run the real analyzers so planner inputs match production shapes."""
    pair = make_pair(**(pair_overrides or {}))
    profile = clean_security_profile(**(security_overrides or {}))
    security = SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights()).assess(profile, pair)
    onchain = OnChainAnalyzer(OnChainThresholds(), OnChainSubWeights()).assess(
        derive_onchain_profile(pair, profile)
    )
    token = TokenAnalyzer(TokenThresholds(), TokenSubWeights()).assess(pair, profile)
    return pair, security, onchain, token


def make_candidate(pair, score=75.0) -> TokenCandidate:
    return TokenCandidate(pair=pair, discovery_score=score, components={},
                          reasons=(), discovered_at=NOW)


def test_healthy_early_token_gets_plan_with_guidance():
    pair, security, onchain, token = full_inputs()
    plan = make_planner().build_plan(
        pair, security, discovery=make_candidate(pair),
        onchain=onchain, token=token, regime=MarketRegime.NEUTRAL,
    )
    assert plan.setup_type is SetupType.EARLY_DISCOVERY
    assert plan.conviction in (ConvictionLevel.HIGH, ConvictionLevel.MEDIUM)
    assert plan.max_position_percent is not None
    assert plan.trade_score > 60
    assert plan.invalidation_conditions  # never enter without knowing when you're wrong
    assert plan.fomo_questions


def test_destructive_security_forces_no_trade():
    pair, security, onchain, token = full_inputs({"is_honeypot": True})
    plan = make_planner().build_plan(pair, security, onchain=onchain, token=token)
    assert plan.conviction is ConvictionLevel.NO_TRADE
    assert plan.setup_type is SetupType.WATCH_ONLY
    assert plan.max_position_percent is None
    assert "Do not enter" in plan.entry_reason


def test_thin_evidence_caps_conviction_at_speculative():
    """Discovery is not confirmation (Part 31 Section 6)."""
    pair = make_pair()
    sparse_profile = SecurityProfile(token=TOKEN, source="goplus", is_honeypot=False,
                                     is_mintable=False)
    security = SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights()).assess(sparse_profile)
    plan = make_planner().build_plan(pair, security)  # no onchain/community/token/discovery
    assert plan.score_coverage < 0.5
    assert plan.conviction is ConvictionLevel.SPECULATIVE


def test_bear_regime_downgrades_conviction():
    pair, security, onchain, token = full_inputs()
    neutral = make_planner().build_plan(pair, security, discovery=make_candidate(pair),
                                        onchain=onchain, token=token,
                                        regime=MarketRegime.NEUTRAL)
    bear = make_planner().build_plan(pair, security, discovery=make_candidate(pair),
                                     onchain=onchain, token=token,
                                     regime=MarketRegime.BEAR)
    order = [ConvictionLevel.NO_TRADE, ConvictionLevel.SPECULATIVE,
             ConvictionLevel.MEDIUM, ConvictionLevel.HIGH]
    assert order.index(bear.conviction) < order.index(neutral.conviction)


def test_checklist_unknowns_are_not_passes():
    pair = make_pair()
    sparse_profile = SecurityProfile(token=TOKEN, source="goplus", is_honeypot=False,
                                     is_mintable=False)
    security = SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights()).assess(sparse_profile)
    plan = make_planner().build_plan(pair, security)
    statuses = {item.name: item.status for item in plan.checklist}
    assert statuses["Community organic"] is CheckStatus.UNKNOWN
    assert statuses["On-chain health"] is CheckStatus.UNKNOWN


def test_unknown_security_facts_become_confirmation_tasks():
    pair = make_pair()
    sparse_profile = SecurityProfile(token=TOKEN, source="goplus", is_honeypot=False,
                                     is_mintable=False)
    security = SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights()).assess(sparse_profile)
    plan = make_planner().build_plan(pair, security)
    assert any("LP lock" in task for task in plan.confirmations_required)
    assert any("community" in task.lower() for task in plan.confirmations_required)


def test_serious_findings_become_invalidation_watchpoints():
    pair, security, onchain, token = full_inputs({"is_mintable": True})
    plan = make_planner().build_plan(pair, security, onchain=onchain, token=token)
    assert any("mint authority" in c for c in plan.invalidation_conditions)


def test_watch_only_setup_carries_no_position_guidance():
    """Distribution phase on an old pair => watch only, no sizing (Part 14 preferred action)."""
    pair, security, onchain, token = full_inputs(
        pair_overrides={"age_hours": 30 * 24.0, "price_change_24h": -15.0,
                        "buys_24h": 200, "sells_24h": 500}
    )
    plan = make_planner().build_plan(pair, security, onchain=onchain, token=token)
    assert plan.setup_type is SetupType.WATCH_ONLY
    assert plan.max_position_percent is None
    assert plan.conviction is not ConvictionLevel.NO_TRADE  # not destructive, just not now
    assert "Monitor only" in plan.entry_reason
    assert "WATCH ONLY" in plan.render()


def test_older_token_in_expansion_is_confirmation_setup():
    pair, security, onchain, token = full_inputs(
        pair_overrides={"age_hours": 72.0, "price_change_24h": 40.0,
                        "buys_24h": 700, "sells_24h": 300}
    )
    plan = make_planner().build_plan(pair, security, onchain=onchain, token=token)
    assert plan.setup_type is SetupType.CONFIRMATION


def test_render_produces_readable_plan():
    pair, security, onchain, token = full_inputs()
    text = make_planner().build_plan(pair, security, discovery=make_candidate(pair),
                                     onchain=onchain, token=token).render()
    assert "TRADE PLAN" in text
    assert "research only" in text
    assert "Invalidation conditions" in text
    assert "Would I buy this if the price was not moving?" in text
