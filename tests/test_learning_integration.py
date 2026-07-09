"""Integration tests: mind layer wired into the scanner + backtester (Section 10)."""

from datetime import datetime, timedelta, timezone

from meme_intelligence.alerts.notification_engine import NotificationEngine
from meme_intelligence.analytics.backtesting import refresh_outcomes
from meme_intelligence.config.settings import AlertEngineSettings, BacktestSettings, Settings
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.database.storage import Storage
from meme_intelligence.learning.service import LearningService
from meme_intelligence.learning.store import LearningStore
from meme_intelligence.workflow.controller import ContinuousScanner

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _pair(address="TokenA"):
    token = TokenIdentity(chain="solana", address=address, symbol="MEMA")
    return DexPair(
        chain="solana", pair_address=f"Pool{address}", base_token=token,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=90_000.0,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )


def _profile(token):
    return SecurityProfile(
        token=token, source="goplus", is_honeypot=False, cannot_sell_all=False,
        is_mintable=False, ownership_renounced=True, is_freezable=False,
        buy_tax_percent=0.0, sell_tax_percent=0.0, honeypot_same_creator_count=0,
        holder_count=2500, top_holder_percent=3.0, top10_holder_percent=22.0,
        creator_percent=1.5, lp_locked_percent=95.0)


class _FakeGecko:
    def __init__(self, pools):
        self.pools = pools

    async def get_new_pools(self, network):
        return self.pools


class _FakeGoPlus:
    def __init__(self, profiles):
        self.profiles = profiles

    async def get_token_security(self, chain, address):
        return self.profiles.get(address)


class _Sink:
    async def send(self, event):
        pass


async def _fake_sleep(_seconds):
    pass


def _learning(enable_in_monitor: bool) -> LearningService:
    env = {"MEMEINTEL_LEARNING_STATE_DIR": ":memory:"}
    if enable_in_monitor:
        env["MEMEINTEL_LEARNING_ENABLE_IN_MONITOR"] = "true"
    settings = Settings.from_env(env=env)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    return settings, LearningService(settings, store=store, now_func=lambda: NOW)


def _scanner(settings, storage, learning, pools, profiles):
    notifier = NotificationEngine([_Sink()], AlertEngineSettings(), time_func=lambda: 0.0)
    return ContinuousScanner(
        settings, storage, notifier,
        gecko_client=_FakeGecko(pools), goplus_client=_FakeGoPlus(profiles),
        learning_service=learning, now_func=lambda: NOW, sleep_func=_fake_sleep)


async def test_scanner_feeds_mind_layer_when_enabled():
    pair = _pair()
    settings, learning = _learning(enable_in_monitor=True)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = _scanner(settings, storage, learning, [pair],
                           {pair.base_token.address: _profile(pair.base_token)})
        history = await scanner.run(max_cycles=1)

    # The analyzed coin was recorded and a trajectory snapshot captured.
    coin_id = learning.store.coin_id(pair.base_token)
    assert coin_id is not None
    assert len(learning.store.snapshots_for(coin_id)) >= 1
    assert history[0].learned == 1


async def test_scanner_skips_mind_layer_when_flag_off():
    pair = _pair()
    # Flag off -> the scanner nulls the wired service and never feeds it.
    settings = Settings.from_env(env={"MEMEINTEL_LEARNING_STATE_DIR": ":memory:"})
    store = LearningStore(":memory:", now_func=lambda: NOW)
    learning = LearningService(settings, store=store, now_func=lambda: NOW)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = _scanner(settings, storage, learning, [pair],
                           {pair.base_token.address: _profile(pair.base_token)})
        history = await scanner.run(max_cycles=1)

    assert learning.store.coin_id(pair.base_token) is None
    assert history[0].learned == 0


# ---- Backtester -> resolve_outcome feed ----

class _FakeStorage:
    """Minimal storage exposing exactly what refresh_outcomes calls."""

    def __init__(self, prediction, later_snapshot):
        self._prediction = prediction
        self._later = later_snapshot
        self.recorded = []

    def predictions(self):
        return [self._prediction]

    def outcomes_for_snapshot(self, snapshot_id):
        return {}

    def snapshots_for_token(self, token_id):
        return [self._later]

    def record_outcome(self, **kwargs):
        self.recorded.append(kwargs)


class _RecordingLearning:
    def __init__(self):
        self.calls = []

    def resolve_outcome(self, address, chain, horizon_hours, forward_return_percent, *, is_rug):
        self.calls.append((address, chain, horizon_hours, forward_return_percent, is_rug))


async def test_backtester_feeds_resolve_outcome():
    predicted_at = NOW - timedelta(hours=25)
    prediction = {
        "snapshot_id": 1, "token_id": 7, "created_at": predicted_at.isoformat(),
        "price_usd": 1.0, "chain": "solana", "address": "TokenA",
    }
    # A later snapshot right at the +1h window target, price doubled, alive.
    later = {"id": 2, "created_at": (predicted_at + timedelta(hours=1)).isoformat(),
             "price_usd": 2.0, "liquidity_usd": 5000.0}
    storage = _FakeStorage(prediction, later)
    learning = _RecordingLearning()
    settings = BacktestSettings()

    await refresh_outcomes(storage, None, settings=settings,
                           learning_service=learning, now_func=lambda: NOW)

    assert storage.recorded  # outcome measured
    assert learning.calls, "learning.resolve_outcome should have been fed"
    address, chain, horizon, ret, is_rug = learning.calls[0]
    assert (address, chain) == ("TokenA", "solana")
    assert horizon == 1.0
    assert ret == 100.0        # +100% (1.0 -> 2.0)
    assert is_rug is False     # liquidity above the survival floor


async def test_backtester_marks_dead_token_as_rug():
    predicted_at = NOW - timedelta(hours=25)
    prediction = {
        "snapshot_id": 1, "token_id": 7, "created_at": predicted_at.isoformat(),
        "price_usd": 1.0, "chain": "solana", "address": "DeadTok",
    }
    later = {"id": 2, "created_at": (predicted_at + timedelta(hours=1)).isoformat(),
             "price_usd": 0.4, "liquidity_usd": 100.0}  # below survival floor
    storage = _FakeStorage(prediction, later)
    learning = _RecordingLearning()
    await refresh_outcomes(storage, None, settings=BacktestSettings(),
                           learning_service=learning, now_func=lambda: NOW)
    assert learning.calls[0][4] is True  # is_rug
