"""Tests for multi-provider failover and cross-source verification (Spec Part 15)."""

import pytest

from meme_intelligence.collectors.market_service import MarketDataService
from meme_intelligence.core.errors import (
    AllProvidersFailedError,
    CollectorError,
    TransientCollectorError,
)
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

    error = None   # optional explicit exception (else a transient outage)

    async def get_token_pairs(self, token_address, chain=None):
        self.calls += 1
        if self.fail:
            raise self.error or TransientCollectorError(f"{self.name} is down")
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


async def test_cross_check_excludes_the_actual_source_after_failover():
    """Bug-hunt: cross_check_liquidity hardcoded `self._providers[1:]`,
    assuming the pair it's verifying always came from providers[0]. When
    failover means the SECOND provider actually answered (the first is
    down), that hardcoded skip let the second provider "confirm" its own
    data as independent. Track the real source instead."""
    primary = FakeProvider("primary", fail=True)
    verifier = FakeProvider("verifier", [make_pair(liquidity=50_000.0)])
    service = MarketDataService([primary, verifier])

    pair = await service.get_best_pair("TokenAddr1", chain="solana")
    assert pair is not None  # failover succeeded via "verifier"

    # primary is still down, so the ONLY other provider is the one that
    # actually supplied `pair` — correct behavior is "cannot verify",
    # NOT a self-confirmed True.
    verdict, note = await service.cross_check_liquidity(pair)
    assert verdict is None
    assert "could not verify" in note


async def test_cross_check_both_sources_agree_on_zero_liquidity():
    """Bug-hunt: `low > 0` alone reported a genuinely dead pool (both
    sources agreeing on exactly $0) as unable-to-verify instead of
    confirming the agreement."""
    primary = FakeProvider("primary", [make_pair(liquidity=0.0)])
    verifier = FakeProvider("verifier", [make_pair(liquidity=0.0)])
    service = MarketDataService([primary, verifier])
    verdict, note = await service.cross_check_liquidity(make_pair(liquidity=0.0))
    assert verdict is True
    assert "confirmed" in note


async def test_all_providers_failing_raises():
    service = MarketDataService([FakeProvider("a", fail=True), FakeProvider("b", fail=True)])
    with pytest.raises(AllProvidersFailedError):
        await service.get_token_pairs("TokenAddr1", chain="solana")


# ---- Name/symbol search (copycat screen support) ----


class SearchingProvider(FakeProvider):
    def __init__(self, name, results=None, search_fail=False):
        super().__init__(name)
        self.results = results if results is not None else []
        self.search_fail = search_fail
        self.search_calls = 0

    async def search_pairs(self, query):
        self.search_calls += 1
        if self.search_fail:
            raise TransientCollectorError(f"{self.name} search is down")
        return self.results


async def test_search_skips_providers_without_search_support():
    """GeckoTerminal has no search endpoint — the service must skip it and
    use the first provider that CAN search, not crash on the missing method."""
    no_search = FakeProvider("geckoterminal")
    searcher = SearchingProvider("dexscreener", [make_pair()])
    service = MarketDataService([no_search, searcher])
    results = await service.search_pairs("MEME")
    assert results and searcher.search_calls == 1


async def test_search_fails_over_and_degrades_to_empty():
    """A failing search provider falls through to the next; nobody able to
    search returns [] (no evidence — Rule 8), never an exception."""
    broken = SearchingProvider("a", search_fail=True)
    working = SearchingProvider("b", [make_pair()])
    assert await MarketDataService([broken, working]).search_pairs("MEME")
    assert await MarketDataService([broken]).search_pairs("MEME") == []
    assert await MarketDataService([FakeProvider("no-search")]).search_pairs("MEME") == []


# ---- Confirmed-empty lookups (review finding, 2026-07-29) ----
#
# ProviderPool.call_with_provider returns the first result that does not RAISE,
# and an empty list does not raise. DexScreener answers HTTP 200 {"pairs": null}
# for a token it has not indexed (verified live 2026-07-29), which parses to [].
# For a coin discovered through GeckoTerminal that DexScreener does not carry,
# "not indexed here" was therefore indistinguishable from "no market left" —
# and the backtester read the latter as a token death, writing a fabricated
# -100% / RUG label and permanently blacklisting the deployer.


async def test_empty_from_the_first_provider_is_not_an_empty_market():
    """The decisive case: DexScreener silent, GeckoTerminal holding the pool."""
    silent = FakeProvider("dexscreener", [])
    knows = FakeProvider("geckoterminal", [make_pair(liquidity=42_000.0)])
    service = MarketDataService([silent, knows])

    # The plain lookup still short-circuits on the first non-raising provider.
    assert await service.get_token_pairs("TokenAddr1", chain="solana") == []
    # The confirming lookup keeps asking, and finds the live pool.
    pairs = await service.get_token_pairs_confirmed("TokenAddr1", chain="solana")
    assert [p.liquidity_usd for p in pairs] == [42_000.0]


async def test_every_provider_empty_confirms_an_empty_market():
    """A real death must still be reportable, or genuine rugs go unrecorded."""
    service = MarketDataService([FakeProvider("a", []), FakeProvider("b", [])])
    assert await service.get_token_pairs_confirmed("TokenAddr1", chain="solana") == []


async def test_unanswered_provider_leaves_emptiness_unconfirmed():
    """One provider empty, the other DOWN: nobody has established the market is
    gone, so this is missing data and must raise rather than report [] (Rule 8).
    Reporting [] here is exactly what fabricates a death."""
    service = MarketDataService([FakeProvider("a", []), FakeProvider("b", fail=True)])
    with pytest.raises(AllProvidersFailedError):
        await service.get_token_pairs_confirmed("TokenAddr1", chain="solana")


async def test_confirmation_sweep_only_runs_when_the_answer_is_empty():
    """Rule 11: the normal path must not double its provider calls."""
    primary = FakeProvider("primary", [make_pair()])
    backup = FakeProvider("backup", [make_pair(liquidity=1.0)])
    service = MarketDataService([primary, backup])
    await service.get_token_pairs_confirmed("TokenAddr1", chain="solana")
    assert primary.calls == 1 and backup.calls == 0


# ---- Cross-check must compare the SAME pool (2026-07-31 bug hunt) ----


async def test_cross_check_never_verifies_against_a_different_pool():
    """A verifier that does not carry this pair cannot verify it. Comparing
    its deepest OTHER pool produced false 'sources disagree' downgrades and
    false 'liquidity confirmed' stamps."""
    tracked = make_pair(pair_address="MainPool", liquidity=80_000.0)
    side = make_pair(pair_address="SidePool", liquidity=5_000.0)
    service = MarketDataService([FakeProvider("dexscreener", [tracked]),
                                 FakeProvider("geckoterminal", [side])])
    await service.get_token_pairs(tracked.base_token.address)   # record provenance
    verdict, note = await service.cross_check_liquidity(tracked)
    assert verdict is None                       # unknown, not a disagreement
    assert "could not verify" in note


async def test_cross_check_still_confirms_when_the_same_pool_is_carried():
    tracked = make_pair(pair_address="MainPool", liquidity=80_000.0)
    same = make_pair(pair_address="MainPool", liquidity=76_000.0)
    service = MarketDataService([FakeProvider("dexscreener", [tracked]),
                                 FakeProvider("geckoterminal", [same])])
    await service.get_token_pairs(tracked.base_token.address)
    verdict, note = await service.cross_check_liquidity(tracked)
    assert verdict is True and "confirmed" in note


# ---- Confirmation-sweep review findings (2026-07-31) ----


async def test_the_sweep_records_provenance_so_the_source_cannot_self_confirm():
    """A pool only the SECOND provider carries used to come back without
    provenance, so cross_check_liquidity fell back to asking everyone and let
    that same provider 'confirm' its own number — presenting one source to the
    operator as two."""
    only = make_pair(pair_address="GeckoPool", liquidity=42_000.0)
    service = MarketDataService([FakeProvider("dexscreener", []),
                                 FakeProvider("geckoterminal", [only])])
    pairs = await service.get_token_pairs_confirmed(only.base_token.address)
    assert [p.pair_address for p in pairs] == ["GeckoPool"]

    verdict, note = await service.cross_check_liquidity(only)
    assert verdict is None                       # honest unknown, not self-confirmed
    assert "geckoterminal" not in note.lower() or "could not verify" in note


async def test_a_permanent_not_indexed_error_does_not_block_archiving_forever():
    """A 404 is that provider's ANSWER ('no market here'), not an outage. Read
    as an outage, a genuinely dead coin could never be archived and its entry
    re-burned provider calls on every recheck."""
    dead = FakeProvider("dexscreener", [])
    missing = FakeProvider("geckoterminal", [])
    missing.fail = True
    missing.error = CollectorError("404 not indexed")
    service = MarketDataService([dead, missing])
    pairs = await service.get_token_pairs_confirmed(TOKEN.address)
    assert pairs == []                           # confirmed empty -> archivable


async def test_a_transient_outage_still_leaves_emptiness_unconfirmed():
    """The other half: real provider trouble must still raise, so an outage is
    never mistaken for death (Rule 8)."""
    dead = FakeProvider("dexscreener", [])
    down = FakeProvider("geckoterminal", [], fail=True)
    service = MarketDataService([dead, down])
    with pytest.raises(AllProvidersFailedError):
        await service.get_token_pairs_confirmed(TOKEN.address)
