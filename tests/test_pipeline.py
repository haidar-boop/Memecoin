"""Tests for the shared research pipeline (Spec Part 13 Section 2).

Kept intentionally small: full pipeline coverage doesn't exist yet, so this
file covers only the new Jupiter liquidity-probe merge behavior (Project 1)
plus a backward-compatibility sanity check.
"""


from meme_intelligence.config.settings import Settings
from meme_intelligence.core.models import DexPair, LiquidityProbeResult, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

TOKEN = TokenIdentity(chain="solana", address="MintAddr1", symbol="MEME")


def make_pair() -> DexPair:
    return DexPair(chain="solana", pair_address="Pair1", base_token=TOKEN, liquidity_usd=80000.0)


def make_profile(**overrides) -> SecurityProfile:
    defaults = dict(
        token=TOKEN, source="goplus",
        is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
        is_open_source=True, is_mintable=False, ownership_renounced=True,
        holder_count=2000, top_holder_percent=3.0, top10_holder_percent=20.0,
        lp_locked_percent=95.0,
    )
    defaults.update(overrides)
    return SecurityProfile(**defaults)


class FakeGoPlus:
    def __init__(self, profile: SecurityProfile) -> None:
        self._profile = profile

    async def get_token_security(self, chain, address):
        return self._profile


class FakeJupiter:
    def __init__(self, result: LiquidityProbeResult) -> None:
        self._result = result
        self.calls: list[tuple] = []

    async def check_round_trip_liquidity(self, mint, *, probe_sol_amount, slippage_bps,
                                         sell_confirm_fraction=0.05):
        self.calls.append((mint, probe_sol_amount, slippage_bps))
        return self._result


async def test_jupiter_probe_merges_into_security_profile_and_scoring():
    goplus = FakeGoPlus(make_profile())
    probe_result = LiquidityProbeResult(
        token=TOKEN, source="jupiter",
        live_buy_route_found=True, live_sell_route_found=False,
    )
    jupiter = FakeJupiter(probe_result)
    pipeline = ResearchPipeline(Settings(), goplus, jupiter_client=jupiter)

    result = await pipeline.analyze_pair(make_pair())

    assert result is not None
    assert jupiter.calls == [(TOKEN.address, Settings().liquidity_probe.probe_sol_amount, 500)]
    assert result.security_profile.live_buy_route_found is True
    assert result.security_profile.live_sell_route_found is False
    assert result.security.is_destructive
    assert any("no route to sell" in f.message for f in result.security.destructive_findings)


async def test_pipeline_without_jupiter_client_is_unchanged():
    goplus = FakeGoPlus(make_profile())
    pipeline = ResearchPipeline(Settings(), goplus)  # jupiter_client defaults to None

    result = await pipeline.analyze_pair(make_pair())

    assert result is not None
    assert result.security_profile.live_buy_route_found is None
    assert result.security_profile.live_sell_route_found is None
    assert not result.security.is_destructive


# ---- Wallet-intelligence credit gate (Rule 11, 2026-07-17 rebuild) ----

from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)

# A small, fresh, clean candidate — the profile the credit gate SHOULD
# spend a wallet lookup on (inside the 3h freshness window and under any
# ceiling; scores well above the 50 floor).
GOOD_PAIR_KWARGS = dict(
    chain="solana", pair_address="PairG", base_token=TOKEN,
    market_cap=80_000.0, liquidity_usd=45_000.0, volume_24h=20_000.0,
    pair_created_at=NOW - timedelta(hours=1),
)

# Weak but NOT destructive: empirically scores ~35 with SecurityAnalyzer —
# below the credit gate's 50 floor, above nothing that would abort analysis.
WEAK_PROFILE_KWARGS = dict(
    token=TOKEN, source="goplus",
    is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
    is_open_source=False, is_proxy=True, is_mintable=True,
    ownership_renounced=False, hidden_owner=True, can_take_back_ownership=True,
    has_blacklist=True, trading_pausable=True, is_freezable=True,
    balance_mutable=False, selfdestruct=False,
    buy_tax_percent=8.0, sell_tax_percent=14.0, tax_modifiable=True,
    fake_token=False, is_airdrop_scam=False, anti_whale_modifiable=True,
    slippage_modifiable=True, personal_slippage_modifiable=True,
    trading_cooldown=True, honeypot_same_creator_count=0,
    holder_count=25, top_holder_percent=25.0, top10_holder_percent=80.0,
    creator_percent=12.0, owner_percent=9.0, lp_locked_percent=5.0,
)


class RecordingWallet:
    """WalletDataService stand-in that records whether it was asked to spend."""

    def __init__(self):
        self.calls: list[str] = []

    async def gather(self, token):
        self.calls.append(token.address)
        from meme_intelligence.core.errors import CollectorError
        raise CollectorError("no real data in tests")  # analysis continues


def gate_pipeline(profile=None, env=None):
    wallet = RecordingWallet()
    settings = Settings.from_env(env=env or {})
    pipeline = ResearchPipeline(settings, FakeGoPlus(profile or make_profile()),
                                wallet_service=wallet, now_func=lambda: NOW)
    return pipeline, wallet


async def test_wallet_lookup_runs_for_a_small_fresh_clean_candidate():
    pipeline, wallet = gate_pipeline()
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    assert result is not None
    assert wallet.calls == [TOKEN.address]          # gate opened: worth the spend


async def test_wallet_lookup_skipped_when_too_old():
    pipeline, wallet = gate_pipeline()
    stale = dict(GOOD_PAIR_KWARGS, pair_created_at=NOW - timedelta(hours=5))
    result = await pipeline.analyze_pair(DexPair(**stale))
    assert result is not None                        # analysis still complete
    assert wallet.calls == []                        # but no credits spent


async def test_wallet_lookup_skipped_when_untradeable():
    pipeline, wallet = gate_pipeline()
    dead = dict(GOOD_PAIR_KWARGS, liquidity_usd=None, market_cap=None)
    await pipeline.analyze_pair(DexPair(**dead))
    assert wallet.calls == []


async def test_wallet_lookup_skipped_when_oversized():
    env = {"MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD": "50000"}
    pipeline, wallet = gate_pipeline(env=env)
    big = dict(GOOD_PAIR_KWARGS, liquidity_usd=2_800_000.0)
    await pipeline.analyze_pair(DexPair(**big))
    assert wallet.calls == []


async def test_wallet_lookup_skipped_below_security_score_floor():
    pipeline, wallet = gate_pipeline(profile=SecurityProfile(**WEAK_PROFILE_KWARGS))
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    assert result is not None
    assert not result.security.is_destructive        # weak, not invalid
    assert result.security.overall_score < 50.0      # fixture sanity
    assert wallet.calls == []


async def test_wallet_lookup_skipped_when_destructive():
    pipeline, wallet = gate_pipeline(profile=make_profile(is_honeypot=True))
    await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    assert wallet.calls == []


async def test_force_wallet_check_bypasses_the_gate():
    """Operator holdings and manual /check lookups always get wallet data —
    the deliberate, rare spend the gate must not block."""
    pipeline, wallet = gate_pipeline(profile=SecurityProfile(**WEAK_PROFILE_KWARGS))
    stale = dict(GOOD_PAIR_KWARGS, pair_created_at=NOW - timedelta(days=2))
    await pipeline.analyze_pair(DexPair(**stale), force_wallet_check=True)
    assert wallet.calls == [TOKEN.address]


async def test_unknown_age_does_not_block_the_lookup():
    pipeline, wallet = gate_pipeline()
    ageless = dict(GOOD_PAIR_KWARGS, pair_created_at=None)
    await pipeline.analyze_pair(DexPair(**ageless))
    assert wallet.calls == [TOKEN.address]           # Rule 8: unknown != old


def test_credit_gate_floor_configurable_and_validated():
    import pytest
    from meme_intelligence.config.settings import WalletIntelSettings
    from meme_intelligence.core.errors import ConfigurationError

    assert WalletIntelSettings().credit_gate_min_security_score == 50.0
    s = Settings.from_env(
        env={"MEMEINTEL_WALLET_CREDIT_GATE_MIN_SECURITY_SCORE": "70"})
    assert s.wallet.credit_gate_min_security_score == 70.0
    with pytest.raises(ConfigurationError, match="credit_gate_min_security_score"):
        WalletIntelSettings(credit_gate_min_security_score=101.0)


# ---- Credit-gate spend bounds (2026-07-17 review fixes) ----

def gate_pipeline_with_clock(env=None):
    clock = {"now": NOW}
    wallet = RecordingWallet()
    settings = Settings.from_env(env=env or {})
    pipeline = ResearchPipeline(settings, FakeGoPlus(make_profile()),
                                wallet_service=wallet,
                                now_func=lambda: clock["now"])
    return pipeline, wallet, clock


def fresh_pair(address, clock_now=None):
    token = TokenIdentity(chain="solana", address=address, symbol="T")
    kwargs = dict(GOOD_PAIR_KWARGS, base_token=token, pair_address=f"P{address}")
    if clock_now is not None:
        kwargs["pair_created_at"] = clock_now - timedelta(hours=1)
    return DexPair(**kwargs)


async def test_daily_budget_caps_gated_lookups_but_not_forced():
    env = {"MEMEINTEL_WALLET_CREDIT_GATE_MAX_LOOKUPS_PER_DAY": "2",
           "MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES": "0"}
    pipeline, wallet, clock = gate_pipeline_with_clock(env=env)
    for i in range(4):
        await pipeline.analyze_pair(fresh_pair(f"Tok{i}"))
    assert len(wallet.calls) == 2                       # budget held at 2
    # Forced lookups (holdings//check/plan) are never starved by the budget.
    await pipeline.analyze_pair(fresh_pair("Held1"), force_wallet_check=True)
    assert wallet.calls[-1] == "Held1"


async def test_daily_budget_resets_on_the_next_utc_day():
    env = {"MEMEINTEL_WALLET_CREDIT_GATE_MAX_LOOKUPS_PER_DAY": "1",
           "MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES": "0"}
    pipeline, wallet, clock = gate_pipeline_with_clock(env=env)
    await pipeline.analyze_pair(fresh_pair("DayA1"))
    await pipeline.analyze_pair(fresh_pair("DayA2"))
    assert len(wallet.calls) == 1
    clock["now"] = NOW + timedelta(days=1)
    await pipeline.analyze_pair(fresh_pair("DayB1", clock_now=clock["now"]))
    assert len(wallet.calls) == 2                       # fresh day, fresh budget


async def test_cooldown_skips_repeat_lookup_on_the_same_token():
    env = {"MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES": "60"}
    pipeline, wallet, clock = gate_pipeline_with_clock(env=env)
    await pipeline.analyze_pair(fresh_pair("Hot1"))
    await pipeline.analyze_pair(fresh_pair("Hot1"))     # recheck 0 min later
    assert wallet.calls == ["Hot1"]                     # not re-spent
    clock["now"] = NOW + timedelta(minutes=61)
    await pipeline.analyze_pair(fresh_pair("Hot1", clock_now=clock["now"]))
    assert wallet.calls == ["Hot1", "Hot1"]             # cooldown elapsed


async def test_forced_lookup_stamps_the_cooldown_for_gated_ones():
    env = {"MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES": "60"}
    pipeline, wallet, clock = gate_pipeline_with_clock(env=env)
    await pipeline.analyze_pair(fresh_pair("Mix1"), force_wallet_check=True)
    await pipeline.analyze_pair(fresh_pair("Mix1"))     # gated, 0 min later
    assert wallet.calls == ["Mix1"]                     # redundant spend avoided


def test_spend_bound_settings_validated():
    import pytest
    from meme_intelligence.config.settings import WalletIntelSettings
    from meme_intelligence.core.errors import ConfigurationError

    assert WalletIntelSettings().credit_gate_max_lookups_per_day == 200
    assert WalletIntelSettings().credit_gate_cooldown_minutes == 60.0
    with pytest.raises(ConfigurationError, match="max_lookups_per_day"):
        WalletIntelSettings(credit_gate_max_lookups_per_day=-1)
    with pytest.raises(ConfigurationError, match="cooldown_minutes"):
        WalletIntelSettings(credit_gate_cooldown_minutes=-5.0)


# ---- Social-intelligence credit gate (Roadmap item 5; direct copy of the
# wallet-intelligence gate tests above) ----

from meme_intelligence.core.errors import CollectorError  # noqa: E402
from meme_intelligence.core.models import CommunityProfile  # noqa: E402


class RecordingSocial:
    """LunarCrushClient stand-in that records whether it was asked to spend."""

    def __init__(self, profile=None):
        self.calls: list[str] = []
        self._profile = profile

    async def get_community_profile(self, token):
        self.calls.append(token.address)
        if self._profile is None:
            raise CollectorError("no real data in tests")
        return self._profile


def social_gate_pipeline(profile=None, env=None, social_profile=None):
    social = RecordingSocial(social_profile)
    settings = Settings.from_env(env=env or {})
    pipeline = ResearchPipeline(settings, FakeGoPlus(profile or make_profile()),
                                social_client=social, now_func=lambda: NOW)
    return pipeline, social


async def test_social_lookup_runs_for_a_small_fresh_clean_candidate():
    pipeline, social = social_gate_pipeline()
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    assert result is not None
    assert social.calls == [TOKEN.address]


async def test_social_lookup_skipped_when_too_old():
    pipeline, social = social_gate_pipeline()
    stale = dict(GOOD_PAIR_KWARGS, pair_created_at=NOW - timedelta(hours=5))
    result = await pipeline.analyze_pair(DexPair(**stale))
    assert result is not None
    assert social.calls == []


async def test_social_lookup_skipped_when_untradeable():
    pipeline, social = social_gate_pipeline()
    dead = dict(GOOD_PAIR_KWARGS, liquidity_usd=None, market_cap=None)
    await pipeline.analyze_pair(DexPair(**dead))
    assert social.calls == []


async def test_social_lookup_skipped_below_security_score_floor():
    pipeline, social = social_gate_pipeline(profile=SecurityProfile(**WEAK_PROFILE_KWARGS))
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    assert result is not None
    assert not result.security.is_destructive
    assert result.security.overall_score < 50.0
    assert social.calls == []


async def test_social_lookup_skipped_when_destructive():
    pipeline, social = social_gate_pipeline(profile=make_profile(is_honeypot=True))
    await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    assert social.calls == []


async def test_force_social_check_bypasses_the_gate():
    pipeline, social = social_gate_pipeline(profile=SecurityProfile(**WEAK_PROFILE_KWARGS))
    stale = dict(GOOD_PAIR_KWARGS, pair_created_at=NOW - timedelta(days=2))
    await pipeline.analyze_pair(DexPair(**stale), force_social_check=True)
    assert social.calls == [TOKEN.address]


def social_gate_pipeline_with_clock(env=None, social_profile=None):
    clock = {"now": NOW}
    social = RecordingSocial(social_profile)
    settings = Settings.from_env(env=env or {})
    pipeline = ResearchPipeline(settings, FakeGoPlus(make_profile()),
                                social_client=social,
                                now_func=lambda: clock["now"])
    return pipeline, social, clock


async def test_social_daily_budget_caps_gated_lookups_but_not_forced():
    env = {"MEMEINTEL_SOCIAL_CREDIT_GATE_MAX_LOOKUPS_PER_DAY": "2",
           "MEMEINTEL_SOCIAL_CREDIT_GATE_COOLDOWN_MINUTES": "0"}
    pipeline, social, clock = social_gate_pipeline_with_clock(env=env)
    for i in range(4):
        await pipeline.analyze_pair(fresh_pair(f"STok{i}"))
    assert len(social.calls) == 2
    await pipeline.analyze_pair(fresh_pair("SHeld1"), force_social_check=True)
    assert social.calls[-1] == "SHeld1"


async def test_social_cooldown_skips_repeat_lookup_on_the_same_token():
    env = {"MEMEINTEL_SOCIAL_CREDIT_GATE_COOLDOWN_MINUTES": "60"}
    pipeline, social, clock = social_gate_pipeline_with_clock(env=env)
    await pipeline.analyze_pair(fresh_pair("SHot1"))
    await pipeline.analyze_pair(fresh_pair("SHot1"))
    assert social.calls == ["SHot1"]
    clock["now"] = NOW + timedelta(minutes=61)
    await pipeline.analyze_pair(fresh_pair("SHot1", clock_now=clock["now"]))
    assert social.calls == ["SHot1", "SHot1"]


# ---- Full-chain integration: merged CoinGecko + LunarCrush profile ----

COINGECKO_PROFILE = CommunityProfile(
    token=TOKEN, source="coingecko",
    telegram_members=94_142,
    reddit_subscribers=12_000,
    reddit_posts_per_day=3.0,
    user_content_per_day=18.0,
    positive_sentiment_percent=80.0,
)

LUNARCRUSH_PROFILE = CommunityProfile(
    token=TOKEN, source="lunarcrush",
    positive_sentiment_percent=91.0,  # would be ignored: CoinGecko's wins
    user_content_per_day=None,        # CoinGecko's 18.0 wins regardless
    social_volume_24h=7000,
    social_dominance_percent=3.2,
    galaxy_score=64.0,
    alt_rank=88,
    social_trend="up",
)


class FakeCoinGeckoLikeClient:
    async def get_community_profile(self, token):
        return COINGECKO_PROFILE


async def test_merged_coingecko_and_lunarcrush_profile_reflects_both_sources():
    import pytest
    social = RecordingSocial(LUNARCRUSH_PROFILE)
    pipeline = ResearchPipeline(Settings.from_env(env={}), FakeGoPlus(make_profile()),
                                community_client=FakeCoinGeckoLikeClient(),
                                social_client=social, now_func=lambda: NOW,
                                )
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS),
                                         force_social_check=True)
    assert result is not None
    assert result.community is not None
    # CoinGecko-only fields untouched.
    assert result.community_profile.telegram_members == 94_142
    assert result.community_profile.reddit_subscribers == 12_000
    # CoinGecko's value wins where both sources have data.
    assert result.community_profile.positive_sentiment_percent == pytest.approx(80.0)
    assert result.community_profile.user_content_per_day == pytest.approx(18.0)
    # LunarCrush fills the gaps CoinGecko never had.
    assert result.community_profile.social_volume_24h == 7000
    assert result.community_profile.social_dominance_percent == pytest.approx(3.2)
    assert result.community_profile.galaxy_score == pytest.approx(64.0)
    assert result.community_profile.alt_rank == 88
    assert result.community_profile.social_trend == "up"
    assert result.community_profile.source == "coingecko+lunarcrush"


async def test_lunarcrush_failure_never_breaks_the_coingecko_only_path():
    """Rule 9: a LunarCrush error must not erase or block CoinGecko's
    already-successful community data."""
    social = RecordingSocial(None)  # raises CollectorError on every call
    pipeline = ResearchPipeline(Settings.from_env(env={}), FakeGoPlus(make_profile()),
                                community_client=FakeCoinGeckoLikeClient(),
                                social_client=social, now_func=lambda: NOW)
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS),
                                         force_social_check=True)
    assert result is not None
    assert result.community is not None
    assert result.community_profile.source == "coingecko"  # unmerged, CoinGecko-only
    assert result.community_profile.telegram_members == 94_142


def test_social_spend_bound_settings_validated():
    import pytest as _pytest
    from meme_intelligence.config.settings import SocialIntelSettings
    from meme_intelligence.core.errors import ConfigurationError

    assert SocialIntelSettings().credit_gate_max_lookups_per_day == 200
    assert SocialIntelSettings().credit_gate_cooldown_minutes == 60.0
    with _pytest.raises(ConfigurationError, match="max_lookups_per_day"):
        SocialIntelSettings(credit_gate_max_lookups_per_day=-1)
    with _pytest.raises(ConfigurationError, match="cooldown_minutes"):
        SocialIntelSettings(credit_gate_cooldown_minutes=-5.0)


# ---- The single hardest requirement: fully dormant by default ----

async def test_social_layer_is_a_complete_no_op_with_default_settings():
    """With no social_client wired (the default construction, matching what
    build_social_service() returns when MEMEINTEL_LUNARCRUSH_API_KEY is
    empty), the social block must never fire — not even for a candidate
    that would otherwise clear every gate."""
    settings = Settings.from_env(env={})
    assert settings.social.enable_in_monitor is False
    pipeline = ResearchPipeline(settings, FakeGoPlus(make_profile()), now_func=lambda: NOW)
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS), force_social_check=True)
    assert result is not None
    assert result.community is None
    assert result.community_profile is None


# ---- 2026-07-21 fix: GoPlus / Jupiter probe / community fetch run concurrently ----

import asyncio  # noqa: E402


class SlowFakeGoPlus:
    def __init__(self, profile: SecurityProfile, delay: float) -> None:
        self._profile = profile
        self._delay = delay

    async def get_token_security(self, chain, address):
        await asyncio.sleep(self._delay)
        return self._profile


class SlowFakeJupiter:
    def __init__(self, result: LiquidityProbeResult, delay: float) -> None:
        self._result = result
        self._delay = delay

    async def check_round_trip_liquidity(self, mint, *, probe_sol_amount, slippage_bps,
                                         sell_confirm_fraction=0.05):
        await asyncio.sleep(self._delay)
        return self._result


class SlowFakeCommunity:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def get_community_profile(self, token):
        await asyncio.sleep(self._delay)
        return COINGECKO_PROFILE


async def test_goplus_jupiter_and_community_fetch_concurrently_not_sequentially():
    """The bug this fixes: /check took multiple minutes because GoPlus, the
    Jupiter probe, and CoinGecko ran one after another, so a slow provider's
    full latency stacked on top of the other two instead of overlapping.
    None of the three depend on each other's result, so wall-clock time for
    this leg of analyze_pair should track the SLOWEST of them, not their
    sum."""
    delay = 0.2  # each fake "network call" takes this long
    goplus = SlowFakeGoPlus(make_profile(), delay)
    jupiter = SlowFakeJupiter(
        LiquidityProbeResult(token=TOKEN, source="jupiter",
                             live_buy_route_found=True, live_sell_route_found=True),
        delay)
    community = SlowFakeCommunity(delay)
    pipeline = ResearchPipeline(Settings.from_env(env={}), goplus,
                                jupiter_client=jupiter, community_client=community,
                                now_func=lambda: NOW)

    loop = asyncio.get_event_loop()
    started = loop.time()
    result = await pipeline.analyze_pair(DexPair(**GOOD_PAIR_KWARGS))
    elapsed = loop.time() - started

    assert result is not None
    assert result.community is not None  # all three results still landed correctly
    assert result.security_profile.live_buy_route_found is True
    # Sequential would take >= 3 * delay (0.6s); concurrent should land near
    # 1 * delay. The 2x margin comfortably separates the two without being a
    # flaky, tight timing assertion.
    assert elapsed < delay * 2, (
        f"expected concurrent fetch near {delay}s, took {elapsed:.2f}s -- "
        "looks sequential again")
