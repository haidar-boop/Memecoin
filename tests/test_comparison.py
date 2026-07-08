"""Tests for the multi-token comparison (Spec Part 16, Section 7)."""

from datetime import datetime, timedelta, timezone

from meme_intelligence.ai.comparison import rank_results, render_comparison
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import MarketRegime
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings.from_env(env={})


def make_pair(symbol: str, liquidity=90_000.0, **overrides) -> DexPair:
    token = TokenIdentity(chain="solana", address=f"Token{symbol}", symbol=symbol)
    defaults = dict(
        chain="solana", pair_address=f"Pool{symbol}", base_token=token,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=liquidity,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )
    defaults.update(overrides)
    return DexPair(**defaults)


def make_profile(token: TokenIdentity, honeypot=False, mintable=False) -> SecurityProfile:
    return SecurityProfile(
        token=token, source="goplus",
        is_honeypot=honeypot, cannot_buy=False, cannot_sell_all=False,
        is_open_source=True, is_proxy=False, is_mintable=mintable,
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


class MappedGoPlus:
    def __init__(self, profiles):
        self.profiles = profiles

    async def get_token_security(self, chain, address):
        return self.profiles.get(address)


async def analyze(pairs_and_profiles):
    profiles = {pair.base_token.address: profile for pair, profile in pairs_and_profiles}
    pipeline = ResearchPipeline(SETTINGS, MappedGoPlus(profiles), now_func=lambda: NOW)
    results = []
    for pair, _ in pairs_and_profiles:
        results.append(await pipeline.analyze_pair(pair, regime=MarketRegime.NEUTRAL))
    return results


async def test_ranking_by_score_with_override_forced_last():
    clean = make_pair("GOOD")
    weaker = make_pair("MEHH", liquidity=8_000.0)
    trap = make_pair("TRAP")  # great numbers, but a honeypot
    results = await analyze([
        (clean, make_profile(clean.base_token)),
        (weaker, make_profile(weaker.base_token, mintable=True)),
        (trap, make_profile(trap.base_token, honeypot=True)),
    ])

    ranked = rank_results(results)
    symbols = [r.pair.base_token.symbol for r in ranked]
    assert symbols[-1] == "TRAP"  # override loses to every non-overridden token
    assert symbols[0] == "GOOD"


async def test_render_contains_table_and_ranking():
    a = make_pair("AAAA")
    b = make_pair("BBBB", liquidity=8_000.0)
    results = await analyze([
        (a, make_profile(a.base_token)),
        (b, make_profile(b.base_token)),
    ])
    text = render_comparison(results)
    assert "TOKEN COMPARISON" in text
    assert "AAAA" in text and "BBBB" in text
    assert "security" in text and "FINAL" in text
    assert "RANKING" in text
    assert "1. AAAA" in text
    assert "no data" in text  # community/narrative columns honest about gaps


async def test_override_reason_shown_in_ranking():
    good = make_pair("GOOD")
    trap = make_pair("TRAP")
    results = await analyze([
        (good, make_profile(good.base_token)),
        (trap, make_profile(trap.base_token, honeypot=True)),
    ])
    text = render_comparison(results)
    assert "red-flag override" in text


def test_empty_comparison():
    assert render_comparison([]) == "Nothing to compare."
