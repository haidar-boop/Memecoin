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
    from datetime import timedelta as _td

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
