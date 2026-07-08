"""Tests for multi-provider failover and cross-source verification (Spec Part 15)."""

import pytest

from meme_intelligence.collectors.market_service import MarketDataService
from meme_intelligence.core.errors import AllProvidersFailedError, TransientCollectorError
from meme_intelligence.core.models import DexPair, TokenIdentity

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_pair(pair_address="Pool1", liquidity=50_000.0) -> DexPair:
    return DexPair(chain="solana", pair_address=pair_address, base_token=TOKEN,
                   liquidity_usd=liquidity)


class FakeProvider:
    def __init__(self, name, pairs=None, fail=False):
        self.name = name
        self.pairs = pairs if pairs is not None else []
        self.fail = fail
        self.calls = 0

    async def get_token_pairs(self, token_address, chain=None):
        self.calls += 1
        if self.fail:
            raise TransientCollectorError(f"{self.name} is down")
        return self.pairs


async def test_primary_serves_when_healthy():
    primary = FakeProvider("primary", [make_pair()])
    backup = FakeProvider("backup", [make_pair(liquidity=49_000.0)])
    service = MarketDataService([primary, backup])
    pairs = await service.get_token_pairs("TokenAddr1", chain="solana")
    assert pairs and backup.calls == 0


async def test_failover_to_backup_provider():
    """Part 15 Section 4: if one source fails, automatically switch."""
    primary = FakeProvider("primary", fail=True)
    backup = FakeProvider("backup", [make_pair()])
    service = MarketDataService([primary, backup])
    pairs = await service.get_token_pairs("TokenAddr1", chain="solana")
    assert pairs and primary.calls == 1 and backup.calls == 1


async def test_get_best_pair_picks_deepest_and_handles_total_failure():
    provider = FakeProvider("p", [make_pair("PoolA", 10_000.0), make_pair("PoolB", 90_000.0)])
    service = MarketDataService([provider])
    best = await service.get_best_pair("TokenAddr1", chain="solana")
    assert best.pair_address == "PoolB"

    dead = MarketDataService([FakeProvider("p", fail=True)])
    assert await dead.get_best_pair("TokenAddr1", chain="solana") is None


async def test_cross_check_agreement():
    primary = FakeProvider("primary", [make_pair(liquidity=50_000.0)])
    verifier = FakeProvider("verifier", [make_pair(liquidity=55_000.0)])
    service = MarketDataService([primary, verifier])
    verdict, note = await service.cross_check_liquidity(make_pair(liquidity=50_000.0))
    assert verdict is True
    assert "confirmed" in note


async def test_cross_check_disagreement_reduces_confidence():
    """Part 32 Section 7: sources disagree -> reduce confidence, don't pick one."""
    primary = FakeProvider("primary", [])
    verifier = FakeProvider("verifier", [make_pair(liquidity=2_000.0)])
    service = MarketDataService([primary, verifier])
    verdict, note = await service.cross_check_liquidity(make_pair(liquidity=50_000.0))
    assert verdict is False
    assert "disagree" in note


async def test_cross_check_unavailable_is_unknown_not_confirmed():
    primary = FakeProvider("primary", [])
    verifier = FakeProvider("verifier", fail=True)
    service = MarketDataService([primary, verifier])
    verdict, note = await service.cross_check_liquidity(make_pair())
    assert verdict is None
    assert "could not verify" in note

    single = MarketDataService([primary])
    verdict, note = await single.cross_check_liquidity(make_pair())
    assert verdict is None and "no second source" in note


async def test_all_providers_failing_raises():
    service = MarketDataService([FakeProvider("a", fail=True), FakeProvider("b", fail=True)])
    with pytest.raises(AllProvidersFailedError):
        await service.get_token_pairs("TokenAddr1", chain="solana")
