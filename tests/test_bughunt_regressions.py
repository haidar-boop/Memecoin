"""Regression tests for the bug-hunt fixes (correctness & 24/7 resilience).

Each test pins a specific defect found and fixed during the exhaustive bug
hunt so it cannot silently regress. Grouped by the module it guards.
"""

from datetime import datetime, timezone

import pytest

from meme_intelligence.config.settings import (
    ClassificationBands,
    ScoringWeights,
    SecuritySubWeights,
    Settings,
    _convert,
)
from meme_intelligence.core.errors import ConfigurationError, TransientCollectorError
from meme_intelligence.core.models import (
    CategoryScores,
    DexPair,
    TokenIdentity,
    classify,
    compute_weighted_score,
)

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


# ---- core/models.py: weighted-score clamp + classify epsilon (loop-killer) ----

def test_classify_tolerates_float_overshoot():
    # A weighted average of 100s can land at 100.0000000001; that must classify,
    # not raise a ValueError that escapes the scanner's handler and kills the loop.
    assert classify(100.0 + 1e-9, ClassificationBands()) is not None
    assert classify(-1e-9, ClassificationBands()) is not None


def test_classify_still_rejects_genuinely_out_of_range():
    with pytest.raises(ValueError):
        classify(150.0, ClassificationBands())
    with pytest.raises(ValueError):
        classify(-40.0, ClassificationBands())


def test_weighted_score_is_clamped_to_100():
    scores = CategoryScores(
        foundation=100.0, security=100.0, community=100.0, blockchain=100.0,
        momentum=100.0, narrative=100.0, timing=100.0,
    )
    result = compute_weighted_score(scores, ScoringWeights())
    assert 0.0 <= result.total <= 100.0


# ---- collectors/market_data.py: non-finite + bad timestamp (batch abort) ----

def test_to_float_rejects_non_finite():
    from meme_intelligence.collectors.market_data import _to_float

    assert _to_float("nan") is None
    assert _to_float(float("inf")) is None
    assert _to_float(float("-inf")) is None
    assert _to_float("12.5") == 12.5
    assert _to_float(None) is None


def test_from_ms_timestamp_never_crashes_on_bad_values():
    from meme_intelligence.collectors.market_data import _from_ms_timestamp

    assert _from_ms_timestamp(float("nan")) is None
    assert _from_ms_timestamp(1e30) is None       # out-of-range epoch, no OverflowError
    assert _from_ms_timestamp(0) is None
    assert _from_ms_timestamp(1_700_000_000_000) is not None


# ---- collectors/market_data.py: GeckoTerminal multi-underscore network id ----

def test_geckoterminal_base_id_split_on_last_underscore():
    from meme_intelligence.collectors.market_data import GeckoTerminalClient

    item = {
        "attributes": {"address": "PoolAddr", "name": "WIF / SOL"},
        "relationships": {"base_token": {"data": {"id": "polygon_pos_0xAbC123"}}},
    }
    pair = GeckoTerminalClient._parse_pool(item)
    assert pair.chain == "polygon_pos"
    assert pair.base_token.address == "0xAbC123"


# ---- collectors/security_data.py: Solana transfer-fee authority + burn addr ----

def test_solana_transfer_fee_upgradable_parsed_from_status():
    from meme_intelligence.collectors.security_data import GoPlusClient

    on = {"transfer_fee_upgradable": {"authority": [], "status": "1"}}
    off = {"transfer_fee_upgradable": {"authority": [], "status": "0"}}
    assert GoPlusClient._parse_solana("solana", "A", on).tax_modifiable is True
    assert GoPlusClient._parse_solana("solana", "A", off).tax_modifiable is False
    assert GoPlusClient._parse_solana("solana", "A", {}).tax_modifiable is None


def test_burn_address_matches_exactly_not_by_substring():
    from meme_intelligence.collectors.security_data import _holder_percents, _is_burn_address

    assert _is_burn_address("0x000000000000000000000000000000000000dEaD") is True
    assert _is_burn_address("0x0000000000000000000000000000000000000000") is True
    # A real holder whose address merely contains "dead" must NOT be treated as burn.
    assert _is_burn_address("DeADbeefWa11etAddr1111111111111111111111111") is False
    kept = _holder_percents([{"address": "DeADbeefWa11etAddr", "percent": "0.10"}])
    assert kept == [10.0]


# ---- config/settings.py: NaN env floats + per-weight range ----

def test_convert_rejects_non_finite_floats():
    with pytest.raises(ConfigurationError):
        _convert("nan", 0.15, "MEMEINTEL_WEIGHTS_SECURITY")
    with pytest.raises(ConfigurationError):
        _convert("inf", 0.15, "MEMEINTEL_WEIGHTS_SECURITY")


def test_sub_weight_rejects_out_of_range_even_when_sum_is_one():
    # Sums to exactly 1.0, but a negative weight would invert a component.
    with pytest.raises(ConfigurationError):
        SecuritySubWeights(contract=-0.10, liquidity=0.35, distribution=0.25,
                           developer=0.25, manipulation=0.25)


# ---- scanners/discovery.py: per-network isolation ----

def _pool(addr):
    return DexPair(
        chain="solana", pair_address="P" + addr,
        base_token=TokenIdentity(chain="solana", address=addr),
        liquidity_usd=10_000.0,
    )


class _SplitGecko:
    def __init__(self, pools):
        self.pools = pools

    async def get_new_pools(self, network):
        if network == "eth":
            raise TransientCollectorError("eth provider down")
        return self.pools


async def test_discovery_isolates_one_failing_network():
    from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools

    engine = DiscoveryEngine(Settings.from_env(env={}).discovery)
    candidates, _ = await scan_new_pools(_SplitGecko([_pool("A")]), engine, ["solana", "eth"])
    assert len(candidates) == 1  # solana pools survive eth's outage


async def test_discovery_total_failure_propagates():
    from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools

    engine = DiscoveryEngine(Settings.from_env(env={}).discovery)
    with pytest.raises(TransientCollectorError):
        await scan_new_pools(_SplitGecko([]), engine, ["eth"])


# ---- alerts/notification_engine.py: sink isolation + cooldown-after-delivery ----

def _event():
    from meme_intelligence.alerts.notification_engine import AlertEvent
    from meme_intelligence.core.enums import AlertPriority

    return AlertEvent(
        priority=AlertPriority.HIGH, alert_type="test",
        token=TokenIdentity(chain="solana", address="A"), title="t", reasons=(),
    )


class _BoomSink:
    def __init__(self):
        self.calls = 0

    async def send(self, event):
        self.calls += 1
        raise RuntimeError("sink down")


class _OkSink:
    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(event)


async def test_total_send_failure_does_not_set_cooldown():
    from meme_intelligence.alerts.notification_engine import NotificationEngine
    from meme_intelligence.config.settings import AlertEngineSettings

    boom = _BoomSink()
    engine = NotificationEngine([boom], AlertEngineSettings(), time_func=lambda: 0.0)
    event = _event()
    assert await engine.dispatch([event]) == []   # nothing delivered, no crash
    assert await engine.dispatch([event]) == []   # cooldown NOT recorded -> retried
    assert boom.calls == 2                          # the alert was not permanently muted


async def test_one_failing_sink_does_not_block_the_others():
    from meme_intelligence.alerts.notification_engine import NotificationEngine
    from meme_intelligence.config.settings import AlertEngineSettings

    boom, ok = _BoomSink(), _OkSink()
    engine = NotificationEngine([boom, ok], AlertEngineSettings(), time_func=lambda: 0.0)
    event = _event()
    delivered = await engine.dispatch([event])
    assert delivered == [event]      # the working sink still delivered
    assert ok.sent == [event]
    # Delivered -> cooldown set -> immediate re-dispatch is suppressed.
    assert await engine.dispatch([event]) == []


# ============================================================================
# Round 2 — the scoring-shape / correctness fixes (24 remaining findings)
# ============================================================================

from meme_intelligence.core.enums import WatchlistTier
from meme_intelligence.core.errors import CollectorError, InsufficientDataError

S = Settings.from_env(env={})
TOKEN = TokenIdentity(chain="solana", address="A", symbol="MEM")


def _mpair(**kw):
    base = dict(chain="solana", pair_address="P", base_token=TOKEN)
    base.update(kw)
    return DexPair(**base)


# ---- analyzers/common.py #12: destructive category scores 0, not 100 ----

def test_subscore_destructive_scores_zero():
    from meme_intelligence.analyzers.common import SubScore
    s = SubScore("growth")
    assert s.observe("bot_follower_percent", 60.0)  # known fact, no signal
    s.flag_destructive("fake community")
    assert s.score() == 0.0  # was 100.0 before the fix


def test_subscore_deduction_style_unaffected():
    from meme_intelligence.analyzers.common import SubScore
    from meme_intelligence.core.enums import RiskTier
    s = SubScore("contract")
    assert s.observe("is_mintable", True)
    s.deduct(25, RiskTier.SERIOUS_WARNING, "mintable")
    assert s.score() == 75.0  # no destructive finding -> unchanged


# ---- analyzers/momentum_analyzer.py #3 / #15 / #16 ----

def _momentum():
    from meme_intelligence.analyzers.momentum_analyzer import MomentumAnalyzer
    return MomentumAnalyzer(S.momentum, S.momentum_weights, now_func=lambda: NOW)


def test_consistent_downtrend_scores_below_mixed():
    down = _momentum().assess(_mpair(price_change_1h=-4.0, price_change_6h=-10.0, price_change_24h=-20.0))
    mixed = _momentum().assess(_mpair(price_change_1h=-4.0, price_change_6h=5.0, price_change_24h=-20.0))
    assert down.sub_scores["price"] < mixed.sub_scores["price"]  # was equal (both 60) before


def test_suspect_volume_quality_scores_onchain_low():
    from types import SimpleNamespace
    onchain = SimpleNamespace(sub_scores={"volume_quality": 20.0})
    a = _momentum().assess(_mpair(price_change_1h=2.0), onchain=onchain)
    assert a.sub_scores["onchain"] <= 10  # was ~75 before the fix
    assert any("suspect volume" in f.message for f in a.findings)


def test_unsupported_spike_only_scores_price_low():
    a = _momentum().assess(_mpair(price_change_1h=45.0))  # 24h/6h unknown
    assert a.sub_scores["price"] <= 30  # was ~85 before the fix


# ---- analyzers/token_analyzer.py #17: zero mcap is not a valid anchor ----

def test_effective_mcap_treats_zero_as_missing():
    from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer
    analyzer = TokenAnalyzer(S.token, S.token_weights)
    assert analyzer._effective_mcap(_mpair(market_cap=0.0, fdv=None)) is None
    assert analyzer._effective_mcap(_mpair(market_cap=0.0, fdv=800_000.0)) == 800_000.0
    assert analyzer._effective_mcap(_mpair(market_cap=500_000.0)) == 500_000.0


# ---- collectors/security_data.py #29: EVM tax_modifiable not double-counted ----

def test_evm_tax_modifiable_is_none_not_duplicated():
    from meme_intelligence.collectors.security_data import GoPlusClient
    profile = GoPlusClient._parse_evm("ethereum", "0xabc", {"slippage_modifiable": "1"})
    assert profile.slippage_modifiable is True
    assert profile.tax_modifiable is None  # was True (double-deducted) before


# ---- core/provider_pool.py #40: duplicate names keep independent state ----

async def test_provider_pool_duplicate_names_independent_state():
    from meme_intelligence.core.provider_pool import ProviderPool

    class FakeProv:
        def __init__(self, name, fail):
            self.name, self.fail, self.calls = name, fail, 0

        async def fetch(self, x):
            self.calls += 1
            if self.fail:
                raise CollectorError("boom")
            return f"{self.name}:{x}"

    a, b = FakeProv("dup", True), FakeProv("dup", False)
    pool = ProviderPool([a, b], failure_threshold=2, time_func=lambda: 0.0)
    assert await pool.call("fetch", "x") == "dup:x"  # a fails, b serves
    health = pool.health()
    assert len(health) == 2  # not collapsed to one shared row


# ---- core/cache.py #41: single-flight on concurrent miss ----

async def test_cache_single_flight_on_concurrent_miss():
    import asyncio
    from meme_intelligence.core.cache import TTLCache
    cache = TTLCache(time_func=lambda: 0.0)
    calls = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "value"

    tasks = [asyncio.create_task(cache.get_or_set("k", factory)) for _ in range(5)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    assert results == ["value"] * 5
    assert calls == 1                 # was 5 before the fix
    assert cache._inflight_locks == {}  # guard dict cleaned up


# ---- collectors/market_service.py #20: skip same-source self-confirmation ----

async def test_cross_check_skips_same_source_snapshot():
    from meme_intelligence.collectors.market_service import MarketDataService

    def mk(liq):
        return DexPair(chain="solana", pair_address="P",
                       base_token=TokenIdentity(chain="solana", address="A"), liquidity_usd=liq)

    class FP:
        def __init__(self, name, pairs):
            self.name, self.pairs = name, pairs

        async def get_token_pairs(self, addr, chain=None):
            return self.pairs

    svc = MarketDataService([FP("primary", []), FP("origin", [mk(50_000.0)]), FP("independent", [mk(2_000.0)])])
    verdict, note = await svc.cross_check_liquidity(mk(50_000.0))
    assert verdict is False and "disagree" in note  # origin's identical snapshot no longer self-confirms


# ---- database/storage.py #43: EVM identity case-insensitive, Solana preserved ----

def test_storage_evm_identity_case_insensitive():
    from meme_intelligence.database.storage import Storage
    with Storage(":memory:", now_func=lambda: NOW) as s:
        checksummed = TokenIdentity(chain="ethereum",
                                    address="0xAbCdEf0000000000000000000000000000000001", symbol="PEPE")
        lower = TokenIdentity(chain="ethereum", address="0xabcdef0000000000000000000000000000000001")
        assert s.upsert_token(checksummed) == s.upsert_token(lower)  # one identity
        s.update_watchlist(checksummed, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        assert len(s.get_watchlist()) == 1
        s.record_security_facts(checksummed, {"is_honeypot": False})
        assert s.latest_security_facts(lower) == {"is_honeypot": False}


def test_storage_solana_case_preserved():
    from meme_intelligence.database.storage import Storage
    with Storage(":memory:", now_func=lambda: NOW) as s:
        a = TokenIdentity(chain="solana", address="So1AbcXYZ")
        b = TokenIdentity(chain="solana", address="so1abcxyz")
        assert s.upsert_token(a) != s.upsert_token(b)  # base58 is case-sensitive
