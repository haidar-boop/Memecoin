"""Tests for the final intelligence report renderer (Spec Part 12)."""

from datetime import datetime, timedelta, timezone

from meme_intelligence.ai.report_generator import build_report
from meme_intelligence.analyzers.onchain_analyzer import OnChainAnalyzer, derive_onchain_profile
from meme_intelligence.analyzers.risk_analyzer import RiskAnalyzer
from meme_intelligence.analyzers.scoring_engine import ScoringEngine, derive_timing_score
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import Classification
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.trading.trade_planner import TradePlanner

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")
SETTINGS = Settings.from_env(env={})


def build_all(honeypot=False):
    pair = DexPair(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=60_000.0,
        volume_24h=90_000.0, buys_24h=400, sells_24h=250,
        buyers_24h=300, sellers_24h=180, price_change_24h=5.0,
        pair_created_at=NOW - timedelta(hours=2),
    )
    profile = SecurityProfile(
        token=TOKEN, source="goplus",
        is_honeypot=honeypot, cannot_buy=False, cannot_sell_all=False,
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
    security = SecurityAnalyzer(SETTINGS.security, SETTINGS.security_weights).assess(profile, pair)
    onchain = OnChainAnalyzer(SETTINGS.onchain, SETTINGS.onchain_weights).assess(
        derive_onchain_profile(pair, profile)
    )
    token = TokenAnalyzer(SETTINGS.token, SETTINGS.token_weights).assess(pair, profile)
    risk = RiskAnalyzer(SETTINGS.risk_weights).assess(security, pair=pair, token=token, onchain=onchain)
    master = ScoringEngine(SETTINGS.weights, SETTINGS.bands, now_func=lambda: NOW).evaluate(
        security, onchain=onchain, token_structure=token, risk=risk,
        timing_score=derive_timing_score(pair, token, onchain, now_func=lambda: NOW),
    )
    plan = TradePlanner(SETTINGS.trading, SETTINGS.trade_weights,
                        now_func=lambda: NOW).build_plan(
        pair, security, onchain=onchain, token=token,
    )
    return pair, security, onchain, token, risk, master, plan


def test_report_contains_all_canonical_sections():
    pair, security, onchain, token, risk, master, plan = build_all()
    report = build_report(pair, master, security, onchain=onchain, token=token,
                          risk=risk, plan=plan)
    text = report.text
    for expected in (
        "MEME COIN INTELLIGENCE REPORT",
        "WHY THIS TOKEN COULD SUCCEED",
        "WHY THIS TOKEN COULD FAIL",
        "COMPLETE SCORING TABLE",
        "FINAL CLASSIFICATION",
        "FINAL VERDICT",
        "Would this pass a professional research filter?",
        "Biggest risk:",
        "What would change this opinion:",
    ):
        assert expected in text, f"missing section: {expected}"


def test_bull_and_bear_cases_are_evidence_derived():
    pair, security, onchain, token, risk, master, plan = build_all()
    text = build_report(pair, master, security, onchain=onchain, token=token,
                        risk=risk, plan=plan).text
    assert "Security profile is" in text          # from actual security band
    assert "Early-stage market cap" in text        # from actual token staging


def test_qualified_token_includes_trade_plan():
    pair, security, onchain, token, risk, master, plan = build_all()
    text = build_report(pair, master, security, onchain=onchain, token=token,
                        risk=risk, plan=plan).text
    if master.classification in (Classification.WATCHLIST, Classification.STRONG_CANDIDATE,
                                 Classification.ELITE_OPPORTUNITY):
        assert "TRADE PLAN" in text
    else:
        assert "not applicable" in text


def test_honeypot_report_shows_override_and_fails_filter():
    pair, security, onchain, token, risk, master, plan = build_all(honeypot=True)
    text = build_report(pair, master, security, onchain=onchain, token=token,
                        risk=risk, plan=plan).text
    assert "RED FLAG OVERRIDE" in text
    assert "Would this pass a professional research filter? NO" in text
    assert "not applicable" in text  # no trade planning for Avoid
    assert "honeypot" in text


def test_minimal_report_without_optional_sections():
    pair, security, _, _, _, master, _ = build_all()
    text = build_report(pair, master, security).text
    assert "MEME COIN INTELLIGENCE REPORT" in text
    assert "FINAL VERDICT" in text
