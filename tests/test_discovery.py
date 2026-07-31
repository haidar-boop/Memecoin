"""Tests for the discovery engine (Spec Parts 3/15/27)."""

from datetime import datetime, timedelta, timezone

from meme_intelligence.config.settings import DiscoverySettings
from meme_intelligence.core.models import DexPair, TokenIdentity
from meme_intelligence.scanners.discovery import DiscoveryEngine

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def make_engine(**overrides) -> DiscoveryEngine:
    return DiscoveryEngine(DiscoverySettings(**overrides), now_func=lambda: NOW)


def make_pool(
    address="TokenA",
    pair="PoolA",
    liquidity=20000.0,
    volume=10000.0,
    age_hours=2.0,
    buys=100,
    sells=60,
) -> DexPair:
    return DexPair(
        chain="solana",
        pair_address=pair,
        base_token=TokenIdentity(chain="solana", address=address, symbol=address[:4]),
        liquidity_usd=liquidity,
        volume_24h=volume,
        buys_24h=buys,
        sells_24h=sells,
        pair_created_at=NOW - timedelta(hours=age_hours),
    )


def test_healthy_pool_becomes_candidate():
    candidates, rejected = make_engine().evaluate([make_pool()])
    assert len(candidates) == 1 and not rejected
    candidate = candidates[0]
    assert 0 < candidate.discovery_score <= 100
    assert set(candidate.components) == {"freshness", "liquidity", "volume", "activity"}
    assert candidate.reasons


def test_low_liquidity_rejected():
    candidates, rejected = make_engine().evaluate([make_pool(liquidity=500.0)])
    assert not candidates
    assert "below minimum" in rejected[0].reason


def test_unknown_liquidity_rejected():
    candidates, rejected = make_engine().evaluate([make_pool(liquidity=None)])
    assert not candidates
    assert "cannot verify" in rejected[0].reason


def test_stale_pool_rejected():
    candidates, rejected = make_engine().evaluate([make_pool(age_hours=30.0)])
    assert not candidates
    assert "exceeds discovery window" in rejected[0].reason


def test_dedupe_keeps_deepest_pool_per_token():
    shallow = make_pool(pair="Pool1", liquidity=10000.0)
    deep = make_pool(pair="Pool2", liquidity=60000.0)
    candidates, _ = make_engine().evaluate([shallow, deep])
    assert len(candidates) == 1
    assert candidates[0].pair.pair_address == "Pool2"


def test_fresher_pool_scores_higher():
    fresh = make_pool(address="Fresh", age_hours=0.5)
    older = make_pool(address="Older", age_hours=20.0)
    candidates, _ = make_engine().evaluate([fresh, older])
    scores = {c.pair.base_token.address: c.discovery_score for c in candidates}
    assert scores["Fresh"] > scores["Older"]


def test_more_liquidity_scores_higher_up_to_target():
    small = make_pool(address="Small", liquidity=6000.0)
    big = make_pool(address="Big", liquidity=50000.0)
    over = make_pool(address="Over", liquidity=500000.0)
    candidates, _ = make_engine().evaluate([small, big, over])
    comp = {c.pair.base_token.address: c.components["liquidity"] for c in candidates}
    assert comp["Small"] < comp["Big"]
    assert comp["Big"] == comp["Over"] == 25.0  # capped at target


def test_results_sorted_by_score_descending():
    pools = [
        make_pool(address="Weak", liquidity=6000.0, volume=1500.0, age_hours=20.0, buys=5, sells=5),
        make_pool(address="Strong", liquidity=60000.0, volume=80000.0, age_hours=1.0, buys=300, sells=150),
    ]
    candidates, _ = make_engine().evaluate(pools)
    assert [c.pair.base_token.address for c in candidates] == ["Strong", "Weak"]


def test_missing_optional_metrics_score_zero_not_crash():
    pool = make_pool(volume=None, buys=None, sells=None)
    candidates, _ = make_engine().evaluate([pool])
    candidate = candidates[0]
    assert candidate.components["volume"] == 0.0
    assert candidate.components["activity"] == 0.0
    assert "volume unknown" in candidate.reasons


# ---- Paginated discovery (operator 2026-07-31: "Never analyzed" on a coin
# that went big — one ~20-pool page was a keyhole busy launch hours scrolled
# straight past between scanner cycles). ----

import pytest

from meme_intelligence.config.settings import Settings, ConfigurationError, WorkflowSettings
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.scanners.discovery import scan_new_pools


class PagedFakeGecko:
    """Serves distinct pools per page; records every (network, page) call."""

    def __init__(self, pages_of_pools, fail_pages=()):
        self._pages = pages_of_pools          # list of lists, index 0 = page 1
        self._fail = set(fail_pages)
        self.calls = []

    async def get_new_pools(self, network, page=1):
        self.calls.append((network, page))
        if page in self._fail:
            raise CollectorError("provider hiccup")
        return self._pages[page - 1] if page <= len(self._pages) else []


async def test_pages_widen_the_field_of_view():
    pages = [[make_pool(address=f"P{p}T{i}", pair=f"Pool{p}{i}")
              for i in range(3)] for p in range(3)]
    gecko = PagedFakeGecko(pages)
    candidates, rejected = await scan_new_pools(gecko, make_engine(), ["solana"], pages=3)
    assert gecko.calls == [("solana", 1), ("solana", 2), ("solana", 3)]
    assert len(candidates) + len(rejected) == 9    # all three pages evaluated


async def test_default_stays_one_page():
    gecko = PagedFakeGecko([[make_pool()]])
    await scan_new_pools(gecko, make_engine(), ["solana"])
    assert gecko.calls == [("solana", 1)]          # Rule 18: default unchanged


async def test_later_page_failure_keeps_earlier_pools():
    pages = [[make_pool(address=f"T{i}", pair=f"Pl{i}") for i in range(3)], [], []]
    gecko = PagedFakeGecko(pages, fail_pages={2})
    candidates, rejected = await scan_new_pools(gecko, make_engine(), ["solana"], pages=3)
    assert len(candidates) + len(rejected) == 3    # page 1 kept, no raise
    assert ("solana", 3) not in gecko.calls        # stopped at the failure


async def test_first_page_failure_still_propagates():
    gecko = PagedFakeGecko([[make_pool()]], fail_pages={1})
    with pytest.raises(CollectorError):
        await scan_new_pools(gecko, make_engine(), ["solana"], pages=3)


async def test_empty_page_stops_pagination():
    gecko = PagedFakeGecko([[make_pool()]])      # page 2 will be empty
    await scan_new_pools(gecko, make_engine(), ["solana"], pages=3)
    assert gecko.calls == [("solana", 1), ("solana", 2)]  # no wasted page-3 call


def test_discovery_pages_setting_defaults_and_validates():
    assert WorkflowSettings().discovery_pages == 4
    s = Settings.from_env(env={"MEMEINTEL_WORKFLOW_DISCOVERY_PAGES": "1"})
    assert s.workflow.discovery_pages == 1
    with pytest.raises(ConfigurationError, match="discovery_pages"):
        WorkflowSettings(discovery_pages=0)
