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
