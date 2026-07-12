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
