"""Tests for the shared research pipeline (Spec Part 13 Section 2).

Kept intentionally small: full pipeline coverage doesn't exist yet, so this
file covers the Jupiter liquidity-probe merge behavior (Project 1), a
backward-compatibility sanity check, and the wallet-lookup credit gate
(2026-07-15).
"""

from datetime import datetime, timedelta, timezone

from meme_intelligence.config.settings import Settings
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import DexPair, LiquidityProbeResult, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

TOKEN = TokenIdentity(chain="solana", address="MintAddr1", symbol="MEME")
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def make_pair(**overrides) -> DexPair:
    defaults = dict(chain="solana", pair_address="Pair1", base_token=TOKEN,
                    liquidity_usd=80000.0)
    defaults.update(overrides)
    return DexPair(**defaults)


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


# ---- Wallet-lookup credit gate (2026-07-15) --------------------------------
# A wallet lookup spends a real, metered Helius/Birdeye credit. Running it
# unconditionally on every analyzed token exhausted the operator's free
# Helius tier within days (DECISIONS_LOG 2026-07-11) and starved his trading
# wallet's own balance reads. These tests pin the gate that makes leaving
# MEMEINTEL_WALLET_ENABLE_IN_MONITOR on affordable: a lookup only runs on a
# candidate that could still plausibly earn a buy-side alert.

GOOD_PROFILE_KWARGS = dict(
    token=TOKEN, source="goplus",
    is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
    is_open_source=True, is_mintable=False, ownership_renounced=True,
    holder_count=2000, top_holder_percent=3.0, top10_holder_percent=20.0,
    lp_locked_percent=95.0, buy_tax_percent=0.0, sell_tax_percent=0.0,
)

# Stacked SERIOUS_WARNING deductions (concentration, no LP lock, freeze
# authority, hidden owner, high taxes...) drag the score to ~40 without
# tripping any single DESTRUCTIVE finding (verified empirically) — used by
# both the "below floor" and "floor lowered" gate tests below.
WEAK_PROFILE_KWARGS = {
    **GOOD_PROFILE_KWARGS,
    "is_open_source": False, "is_freezable": True, "ownership_renounced": False,
    "hidden_owner": True, "has_blacklist": True, "trading_pausable": True,
    "can_take_back_ownership": True, "top_holder_percent": 35.0,
    "top10_holder_percent": 85.0, "lp_locked_percent": 0.0,
    "buy_tax_percent": 15.0, "sell_tax_percent": 15.0,
    "creator_percent": 15.0, "owner_percent": 15.0,
    "personal_slippage_modifiable": True, "slippage_modifiable": True,
    "trading_cooldown": True, "anti_whale_modifiable": True,
}


class RecordingWallet:
    """Wallet-service double: records gather() calls, reports no data (a
    no-data answer must never block analysis — mirrors the controller-test
    double of the same name)."""

    def __init__(self):
        self.gather_calls: list[str] = []

    async def gather(self, token):
        self.gather_calls.append(token.address)
        raise InsufficientDataError("no wallet data in this test double")


def make_gate_pipeline(**settings_overrides) -> tuple[ResearchPipeline, RecordingWallet]:
    wallet = RecordingWallet()
    settings = Settings.from_env(env={}) if not settings_overrides else Settings.from_env(
        env={f"MEMEINTEL_{k}": v for k, v in settings_overrides.items()})
    pipeline = ResearchPipeline(
        settings, FakeGoPlus(SecurityProfile(**GOOD_PROFILE_KWARGS)),
        wallet_service=wallet, now_func=lambda: NOW)
    return pipeline, wallet


async def test_wallet_lookup_runs_for_a_small_fresh_clean_candidate():
    """The exact profile the operator wants pitched: under both ceilings,
    inside the freshness window, clean security -> worth the credit."""
    pipeline, wallet = make_gate_pipeline()
    pair = make_pair(liquidity_usd=45_000.0, market_cap=80_000.0,
                     pair_created_at=NOW - timedelta(hours=3))
    result = await pipeline.analyze_pair(pair)
    assert result is not None
    assert wallet.gather_calls == [TOKEN.address]


async def test_wallet_lookup_skipped_when_oversized():
    """Fixture's own default ceilings: $50k liquidity / $100k mcap."""
    pipeline, wallet = make_gate_pipeline()
    pair = make_pair(liquidity_usd=2_000_000.0, market_cap=5_000_000.0,
                     pair_created_at=NOW - timedelta(hours=3))
    result = await pipeline.analyze_pair(pair)
    assert result is not None                 # analysis still runs
    assert wallet.gather_calls == []           # just no wallet spend


async def test_wallet_lookup_skipped_when_too_old():
    pipeline, wallet = make_gate_pipeline()
    pair = make_pair(liquidity_usd=45_000.0, market_cap=80_000.0,
                     pair_created_at=NOW - timedelta(hours=30))
    result = await pipeline.analyze_pair(pair)
    assert result is not None
    assert wallet.gather_calls == []


async def test_wallet_lookup_skipped_when_untradeable():
    """Missing/zero liquidity or market cap -- Rule 8: absence is not a
    green light, mirrors AutomationRules._untradeable exactly."""
    pipeline, wallet = make_gate_pipeline()
    pair = make_pair(liquidity_usd=None, market_cap=80_000.0,
                     pair_created_at=NOW - timedelta(hours=3))
    result = await pipeline.analyze_pair(pair)
    assert result is not None
    assert wallet.gather_calls == []


async def test_wallet_lookup_skipped_when_destructive():
    wallet = RecordingWallet()
    settings = Settings.from_env(env={})
    profile = SecurityProfile(**{**GOOD_PROFILE_KWARGS, "is_honeypot": True})
    pipeline = ResearchPipeline(settings, FakeGoPlus(profile),
                                wallet_service=wallet, now_func=lambda: NOW)
    pair = make_pair(liquidity_usd=45_000.0, market_cap=80_000.0,
                     pair_created_at=NOW - timedelta(hours=3))
    result = await pipeline.analyze_pair(pair)
    assert result is not None
    assert result.security.is_destructive
    assert wallet.gather_calls == []


async def test_wallet_lookup_skipped_below_security_score_floor():
    """A coin scoring below 'Moderate Risk' (default floor 50) is unlikely
    to ever clear an alert gate -- not worth the credit even though it
    isn't outright destructive."""
    wallet = RecordingWallet()
    settings = Settings.from_env(env={})
    weak_profile = SecurityProfile(**WEAK_PROFILE_KWARGS)
    pipeline = ResearchPipeline(settings, FakeGoPlus(weak_profile),
                                wallet_service=wallet, now_func=lambda: NOW)
    pair = make_pair(liquidity_usd=45_000.0, market_cap=80_000.0,
                     pair_created_at=NOW - timedelta(hours=3))
    result = await pipeline.analyze_pair(pair)
    assert result is not None
    assert result.security.overall_score < 50.0   # sanity: the fixture is weak
    assert not result.security.is_destructive      # but not destructive either
    assert wallet.gather_calls == []


async def test_force_wallet_check_bypasses_the_gate():
    """A held or explicitly-checked token always gets a wallet lookup,
    whatever the gate says -- the caller (controller.is_holding, or /check)
    decides this is deliberate, rare spend, not scan volume."""
    pipeline, wallet = make_gate_pipeline()
    oversized = make_pair(liquidity_usd=2_000_000.0, market_cap=5_000_000.0,
                          pair_created_at=NOW - timedelta(hours=30))
    result = await pipeline.analyze_pair(oversized, force_wallet_check=True)
    assert result is not None
    assert wallet.gather_calls == [TOKEN.address]


async def test_credit_gate_min_security_score_configurable():
    """Lowering the floor via settings lets a weaker-but-not-destructive
    coin through -- the threshold is not hardcoded (Rule 17)."""
    weak_profile = SecurityProfile(**WEAK_PROFILE_KWARGS)
    settings = Settings.from_env(env={"MEMEINTEL_WALLET_CREDIT_GATE_MIN_SECURITY_SCORE": "0"})
    wallet = RecordingWallet()
    pipeline = ResearchPipeline(settings, FakeGoPlus(weak_profile),
                                wallet_service=wallet, now_func=lambda: NOW)
    pair = make_pair(liquidity_usd=45_000.0, market_cap=80_000.0,
                     pair_created_at=NOW - timedelta(hours=3))
    result = await pipeline.analyze_pair(pair)
    assert result.security.overall_score < 50.0
    assert wallet.gather_calls == [TOKEN.address]   # floor lowered to 0 -> passes
