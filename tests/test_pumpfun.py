"""Tests for the Pump.fun launch discovery integration (Spec Part 32.5 Section 3).

Stream and frontend-API fixtures are real payloads captured live from
PumpPortal / frontend-api-v3.pump.fun on 2026-07-08, so parsing is tested
against the actual provider shapes, not invented ones.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.alerts.notification_engine import NotificationEngine
from meme_intelligence.collectors.pumpfun import PumpFunFrontendClient, PumpPortalClient
from meme_intelligence.config.settings import (
    AlertEngineSettings,
    ConfigurationError,
    PumpFunSettings,
    Settings,
)
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import (
    DexPair,
    PumpFunCoinState,
    PumpFunLaunch,
    TokenIdentity,
)
from meme_intelligence.core.rate_limiter import RateLimiter
from meme_intelligence.database.storage import Storage
from meme_intelligence.scanners.launch_monitor import (
    LaunchMonitor,
    collect_launch_candidates,
)
from meme_intelligence.workflow.controller import ContinuousScanner

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings.from_env(env={})

# Captured live from wss://pumpportal.fun/api/data (2026-07-08).
CREATE_EVENT = {
    "signature": "4jkxkagarpSKQMFwfcWB1pSgnXPraF4vubXJ5jm4jekEBMwvhrqzQURkr3x7eRbXnByjhSqtMJR5bKE4EKkZua78",
    "mint": "BbB756wjcoE13RveuoZrM4NWTbQTP7HC7UScVPFqpump",
    "traderPublicKey": "5qhRDuF2MRYiaAyJH7r282X62EQuhBh8zhm9utp1EvM1",
    "txType": "create",
    "initialBuy": 21039215.720652,
    "solAmount": 0.600000001,
    "bondingCurveKey": "2YQ1r4k3N35Lu16Vf1ohwrESfFTkw7dpaJpKT9oGthmU",
    "vTokensInBondingCurve": 1051960784.279348,
    "vSolInBondingCurve": 30.60000000099999,
    "marketCapSol": 29.088536814575935,
    "name": "The Pixelated Bull",
    "symbol": "PANSEM",
    "uri": "https://ipfs.io/ipfs/QmdNS3zqySoCvW9JfZEX9BdPF8q7o7bYcTPomEx5uCMVTN",
    "is_mayhem_mode": False,
    "pool": "pump",
}

# Captured live from frontend-api-v3.pump.fun/coins/<mint> (2026-07-08).
COIN_PAYLOAD = {
    "mint": "BbB756wjcoE13RveuoZrM4NWTbQTP7HC7UScVPFqpump",
    "name": "The Pixelated Bull",
    "symbol": "PANSEM",
    "creator": "5qhRDuF2MRYiaAyJH7r282X62EQuhBh8zhm9utp1EvM1",
    "created_timestamp": 1783547128000,
    "complete": False,
    "virtual_sol_reserves": 30000000001,
    "virtual_token_reserves": 1073000000000000,
    "total_supply": 1000000000000000,
    "last_trade_timestamp": 1783547161000,
    "market_cap": 27.95899347716682,
    "nsfw": False,
    "is_banned": False,
    "real_sol_reserves": 1,
    "real_token_reserves": 793100000000000,
    "reply_count": 0,
    "ath_market_cap": 2156.2780109299756,
    "usd_market_cap": 2158.4504834347954,
}

MINT = CREATE_EVENT["mint"]
TOKEN = TokenIdentity(chain="solana", address=MINT, name="The Pixelated Bull",
                      symbol="PANSEM")


class Clock:
    """Mutable test clock for time-travel in monitor scheduling tests."""

    def __init__(self, start: datetime = NOW):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_launch(address=MINT, launchpad="pump", name="The Pixelated Bull",
                symbol="PANSEM", initial_buy_percent=2.1, market_cap_sol=29.0):
    return PumpFunLaunch(
        token=TokenIdentity(chain="solana", address=address, name=name, symbol=symbol),
        source="pumpportal", launchpad=launchpad,
        creator="5qhRDuF2MRYiaAyJH7r282X62EQuhBh8zhm9utp1EvM1",
        created_at=NOW, initial_buy_percent=initial_buy_percent,
        initial_buy_sol=0.6, market_cap_sol=market_cap_sol,
    )


def make_state(clock=None, *, usd_market_cap=15_000.0, market_cap_sol=60.0,
               reply_count=12, complete=False, is_banned=False, nsfw=False,
               last_trade_minutes_ago=2.0):
    now = clock() if clock else NOW
    return PumpFunCoinState(
        token=TOKEN, source="pumpfun", fetched_at=now,
        market_cap_sol=market_cap_sol, usd_market_cap=usd_market_cap,
        reply_count=reply_count, complete=complete,
        curve_progress_percent=35.0, is_banned=is_banned, nsfw=nsfw,
        created_at=now - timedelta(minutes=20),
        last_trade_at=now - timedelta(minutes=last_trade_minutes_ago),
    )


# ---- PumpPortalClient message handling ----

def make_stream_client(clock=None) -> PumpPortalClient:
    return PumpPortalClient("wss://example.invalid/api/data",
                            now_func=clock or (lambda: NOW))


def test_create_event_parsed_into_launch():
    client = make_stream_client()
    client._handle_message(json.dumps(CREATE_EVENT))
    launches = client.drain_launches()
    assert len(launches) == 1
    launch = launches[0]
    assert launch.token.address == MINT
    assert launch.token.chain == "solana"
    assert launch.token.name == "The Pixelated Bull"
    assert launch.token.symbol == "PANSEM"
    assert launch.launchpad == "pump"
    assert launch.creator == CREATE_EVENT["traderPublicKey"]
    assert launch.initial_buy_sol == pytest.approx(0.6)
    assert launch.initial_buy_percent == pytest.approx(2.104, abs=0.01)
    assert launch.market_cap_sol == pytest.approx(29.09, abs=0.01)
    assert launch.bonding_curve == CREATE_EVENT["bondingCurveKey"]
    assert launch.created_at == NOW
    # drain clears the buffer
    assert client.drain_launches() == []


def test_migration_event_buffered_separately():
    client = make_stream_client()
    client._handle_message(json.dumps({"txType": "migrate", "mint": MINT,
                                       "signature": "abc", "pool": "pump-amm"}))
    assert client.drain_launches() == []
    migrations = client.drain_migrations()
    assert len(migrations) == 1
    assert migrations[0].address == MINT


def test_acks_and_malformed_messages_ignored():
    client = make_stream_client()
    client._handle_message(json.dumps({"message": "Successfully subscribed"}))
    client._handle_message("this is not json {")
    client._handle_message(json.dumps(["not", "a", "dict"]))
    client._handle_message(json.dumps({"txType": "create"}))  # no mint
    assert client.drain_launches() == []
    assert client.drain_migrations() == []


def test_launch_without_optional_fields_still_parses():
    client = make_stream_client()
    client._handle_message(json.dumps({"txType": "create", "mint": "M1"}))
    launches = client.drain_launches()
    assert len(launches) == 1
    assert launches[0].token.name is None
    assert launches[0].initial_buy_percent is None
    assert launches[0].market_cap_sol is None


# ---- PumpFunFrontendClient normalization ----

def make_frontend_client() -> PumpFunFrontendClient:
    return PumpFunFrontendClient(rate_limiter=RateLimiter(100.0, burst=10),
                                 cache=TTLCache())


def patch_frontend(monkeypatch, client, payload=None, error=None):
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        client.requested_path = path
        if error is not None:
            raise error
        return payload

    monkeypatch.setattr(client, "_get_json", fake_get_json)


async def test_coin_state_normalized(monkeypatch):
    client = make_frontend_client()
    patch_frontend(monkeypatch, client, payload=COIN_PAYLOAD)
    state = await client.get_coin_state(TOKEN)
    assert state is not None
    assert state.token.address == MINT
    assert state.market_cap_sol == pytest.approx(27.96, abs=0.01)
    assert state.usd_market_cap == pytest.approx(2158.45, abs=0.01)
    assert state.reply_count == 0
    assert state.complete is False
    assert state.is_banned is False
    assert state.nsfw is False
    # fresh launch: essentially nothing bought off the curve yet
    assert state.curve_progress_percent == pytest.approx(0.0, abs=0.01)
    assert state.created_at == datetime.fromtimestamp(1783547128, tz=timezone.utc)
    assert state.last_trade_at == datetime.fromtimestamp(1783547161, tz=timezone.utc)
    assert state.ath_market_cap_sol == pytest.approx(2156.28, abs=0.01)
    assert MINT in client.requested_path


async def test_coin_state_curve_progress_mid_curve(monkeypatch):
    client = make_frontend_client()
    payload = dict(COIN_PAYLOAD, real_token_reserves=396_550_000_000_000)  # half sold
    patch_frontend(monkeypatch, client, payload=payload)
    state = await client.get_coin_state(TOKEN)
    assert state.curve_progress_percent == pytest.approx(50.0)


async def test_coin_state_missing_fields_stay_none(monkeypatch):
    client = make_frontend_client()
    patch_frontend(monkeypatch, client, payload={"mint": MINT})
    state = await client.get_coin_state(TOKEN)
    assert state.usd_market_cap is None
    assert state.reply_count is None
    assert state.complete is None
    assert state.curve_progress_percent is None


async def test_malformed_timestamps_do_not_crash_coin_state(monkeypatch):
    """Bug-hunt: OverflowError/ValueError from an out-of-range/NaN/Infinity
    timestamp escaped _from_ms_timestamp and killed the whole scanner."""
    client = make_frontend_client()
    for bad in (1e30, float("nan"), float("inf"), 1e17, "1e30", "nan"):
        payload = dict(COIN_PAYLOAD, created_timestamp=bad, last_trade_timestamp=bad)
        patch_frontend(monkeypatch, client, payload=payload)
        state = await client.get_coin_state(TOKEN)
        assert state is not None  # must not raise
        assert state.created_at is None
        assert state.last_trade_at is None


async def test_unknown_coin_404_returns_none(monkeypatch):
    client = make_frontend_client()
    patch_frontend(monkeypatch, client,
                   error=CollectorError("pumpfun: unexpected status 404", status_code=404))
    assert await client.get_coin_state(TOKEN) is None


async def test_other_frontend_errors_propagate(monkeypatch):
    client = make_frontend_client()
    patch_frontend(monkeypatch, client,
                   error=CollectorError("pumpfun: unexpected status 403", status_code=403))
    with pytest.raises(CollectorError):
        await client.get_coin_state(TOKEN)


async def test_non_solana_chain_returns_none():
    client = make_frontend_client()
    token = TokenIdentity(chain="ethereum", address="0xabc")
    assert await client.get_coin_state(token) is None


# ---- LaunchMonitor: Section 7 basic filtering ----

def make_monitor(clock, **overrides) -> LaunchMonitor:
    base = {"min_usd_market_cap": 10_000.0, "min_reply_count": 5,
            "min_market_cap_growth_ratio": 1.5, "max_last_trade_age_minutes": 30.0}
    base.update(overrides)
    return LaunchMonitor(PumpFunSettings(**base), now_func=clock)


def test_basic_filter_accepts_clean_launch():
    clock = Clock()
    monitor = make_monitor(clock)
    accepted, rejections = monitor.ingest([make_launch()])
    assert accepted == 1
    assert rejections == []
    assert monitor.tracked_count == 1


def test_basic_filter_rejections():
    clock = Clock()
    monitor = make_monitor(clock)
    bad = [
        make_launch(address="M1", launchpad="bonk"),                       # wrong launchpad
        make_launch(address="M2", name=None, symbol=None),                 # anonymous
        make_launch(address="M3", initial_buy_percent=45.0),               # insider grab
    ]
    accepted, rejections = monitor.ingest(bad)
    assert accepted == 0
    assert len(rejections) == 3
    assert "launchpad" in rejections[0].reason
    assert "anonymous" in rejections[1].reason
    assert "insider" in rejections[2].reason
    assert monitor.tracked_count == 0


def test_duplicate_launches_ignored():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    accepted, rejections = monitor.ingest([make_launch()])
    assert accepted == 0 and rejections == []
    assert monitor.tracked_count == 1


def test_tracking_capacity_bounded():
    clock = Clock()
    monitor = make_monitor(clock, max_pending=2)
    launches = [make_launch(address=f"M{i}") for i in range(4)]
    accepted, rejections = monitor.ingest(launches)
    assert accepted == 2
    assert len(rejections) == 2
    assert all("capacity" in r.reason for r in rejections)


# ---- LaunchMonitor: rechecks, promotion, expiry ----

class FakeFrontend:
    def __init__(self, states=None, error=None):
        self.states = states or {}
        self.error = error
        self.calls: list[str] = []

    async def get_coin_state(self, token):
        self.calls.append(token.address)
        if self.error is not None:
            raise self.error
        return self.states.get(token.address)


async def test_recheck_not_due_until_interval_passes():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    frontend = FakeFrontend({MINT: make_state(clock)})
    await monitor.recheck_due(frontend)
    assert frontend.calls == []  # first check only after one interval
    clock.advance(seconds=121)
    await monitor.recheck_due(frontend)
    assert frontend.calls == [MINT]


async def test_recheck_budget_respected():
    clock = Clock()
    monitor = make_monitor(clock, max_rechecks_per_cycle=3)
    monitor.ingest([make_launch(address=f"M{i}") for i in range(6)])
    clock.advance(seconds=121)
    frontend = FakeFrontend()  # returns None for all: counted as misses
    await monitor.recheck_due(frontend)
    assert len(frontend.calls) == 3  # Rule 11: bounded API pressure


async def test_promotion_when_all_gates_pass():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    clock.advance(seconds=121)
    frontend = FakeFrontend({MINT: make_state(clock)})
    await monitor.recheck_due(frontend)
    candidates = monitor.ready_candidates()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.launch.token.address == MINT
    assert any("market cap" in r for r in candidate.reasons)
    assert any("grew" in r for r in candidate.reasons)
    assert any("replies" in r for r in candidate.reasons)


@pytest.mark.parametrize("failing", [
    {"usd_market_cap": 5_000.0},          # below activity gate
    {"usd_market_cap": None},             # unknown never passes (Rule 8)
    {"market_cap_sol": 30.0},             # barely grew (29 -> 30 < 1.5x)
    {"market_cap_sol": None},
    {"reply_count": 1},                   # no community interest
    {"reply_count": None},
    {"last_trade_minutes_ago": 90.0},     # stale: nobody trading anymore
])
async def test_promotion_blocked_when_any_gate_fails(failing):
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    clock.advance(seconds=121)
    frontend = FakeFrontend({MINT: make_state(clock, **failing)})
    await monitor.recheck_due(frontend)
    assert monitor.ready_candidates() == []
    assert monitor.tracked_count == 1  # still tracked, retried later


async def test_graduated_state_promotes_immediately():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    clock.advance(seconds=121)
    # every other gate would fail, but complete=True is the strongest signal
    frontend = FakeFrontend({MINT: make_state(
        clock, complete=True, usd_market_cap=None, reply_count=None)})
    await monitor.recheck_due(frontend)
    candidates = monitor.ready_candidates()
    assert len(candidates) == 1
    assert "graduated" in candidates[0].reasons[0]


async def test_banned_or_nsfw_dropped():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch(address="M1"), make_launch(address="M2")])
    clock.advance(seconds=121)
    frontend = FakeFrontend({
        "M1": make_state(clock, is_banned=True),
        "M2": make_state(clock, nsfw=True),
    })
    await monitor.recheck_due(frontend)
    assert monitor.tracked_count == 0


async def test_repeated_frontend_misses_drop_launch():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    frontend = FakeFrontend()  # always None (404 / not indexed)
    for _ in range(3):
        clock.advance(seconds=121)
        await monitor.recheck_due(frontend)
    assert monitor.tracked_count == 0
    assert len(frontend.calls) == 3


async def test_frontend_failure_is_survivable():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    clock.advance(seconds=121)
    frontend = FakeFrontend(error=CollectorError("pumpfun: server exploded"))
    await monitor.recheck_due(frontend)  # must not raise (Rule 6)
    assert monitor.tracked_count == 1    # kept for a later retry


def test_pending_launches_expire_after_ttl():
    clock = Clock()
    monitor = make_monitor(clock, pending_ttl_hours=24.0)
    monitor.ingest([make_launch()])
    clock.advance(hours=25)
    assert monitor.ready_candidates() == []
    assert monitor.tracked_count == 0


def test_migration_event_fast_paths_tracked_launch():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    monitor.note_migration(TokenIdentity(chain="solana", address=MINT))
    candidates = monitor.ready_candidates()
    assert len(candidates) == 1
    assert "graduated" in candidates[0].reasons[0]
    # untracked mints are ignored, not adopted
    monitor.note_migration(TokenIdentity(chain="solana", address="Unknown"))
    assert monitor.tracked_count == 1


def test_confirm_and_defer_lifecycle():
    clock = Clock()
    monitor = make_monitor(clock)
    monitor.ingest([make_launch()])
    monitor.note_migration(TokenIdentity(chain="solana", address=MINT))
    assert len(monitor.ready_candidates()) == 1

    monitor.defer(TOKEN)  # market has not indexed it yet
    assert monitor.ready_candidates() == []  # not due again immediately
    clock.advance(seconds=121)
    assert len(monitor.ready_candidates()) == 1  # retried after the interval

    monitor.confirm(TOKEN)
    assert monitor.tracked_count == 0


# ---- collect_launch_candidates composition ----

class FakeStream:
    def __init__(self, launches=None, migrations=None):
        self._launches = list(launches or [])
        self._migrations = list(migrations or [])
        self.started = False

    async def start(self):
        self.started = True

    def drain_launches(self):
        drained, self._launches = self._launches, []
        return drained

    def drain_migrations(self):
        drained, self._migrations = self._migrations, []
        return drained


async def test_collect_launch_candidates_full_pass():
    clock = Clock()
    monitor = make_monitor(clock)
    stream = FakeStream(launches=[make_launch()])
    frontend = FakeFrontend({MINT: make_state(clock)})

    assert await collect_launch_candidates(stream, frontend, monitor) == []
    clock.advance(seconds=121)
    candidates = await collect_launch_candidates(stream, frontend, monitor)
    assert len(candidates) == 1


# ---- ContinuousScanner wiring (Part 32.5 Sections 2/7 end-to-end) ----

from tests.test_controller import (  # reuse the established scanner harness
    FakeGecko,
    FakeGoPlus,
    FakeMarketService,
    RecordingSink,
    clean_profile,
    make_pair,
)


def pumpfun_scanner(storage, *, stream, frontend, market, profiles, sink=None,
                    clock=None):
    async def fake_sleep(seconds):
        pass

    notifier = NotificationEngine([sink or RecordingSink()], AlertEngineSettings(),
                                  time_func=lambda: 0.0)
    return ContinuousScanner(
        SETTINGS, storage, notifier,
        gecko_client=FakeGecko([]),
        goplus_client=FakeGoPlus(profiles),
        market_service=market,
        pumpportal_client=stream,
        pumpfun_client=frontend,
        now_func=clock or (lambda: NOW),
        sleep_func=fake_sleep,
    )


async def test_launch_confirmed_and_analyzed_end_to_end():
    """Launch stream -> filter -> graduation -> market confirmation -> pipeline."""
    launch_pair = make_pair(address=MINT, symbol="PANSEM")
    sink = RecordingSink()
    stream = FakeStream(launches=[make_launch()],
                        migrations=[TokenIdentity(chain="solana", address=MINT)])
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = pumpfun_scanner(
            storage, stream=stream, frontend=FakeFrontend(),
            market=FakeMarketService({MINT: launch_pair}, verdict=(True, "confirmed")),
            profiles={MINT: clean_profile(TOKEN)}, sink=sink,
        )
        history = await scanner.run(max_cycles=1)

        assert stream.started  # scanner started the listener
        assert history[0].analyzed == 1
        assert storage.score_history(TOKEN)
        journal_thesis = [e for e in storage.get_watchlist()]
        assert journal_thesis and "pump.fun launch" in (journal_thesis[0].thesis or "")


async def test_unconfirmed_launch_deferred_not_lost():
    """Section 2: no independent market view -> the candidate waits, and is
    analyzed once a pair appears (never analyzed from launch data alone)."""
    clock = Clock()
    launch_pair = make_pair(address=MINT, symbol="PANSEM")
    market = FakeMarketService({})  # market providers have not indexed it yet
    stream = FakeStream(launches=[make_launch()],
                        migrations=[TokenIdentity(chain="solana", address=MINT)])
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = pumpfun_scanner(
            storage, stream=stream, frontend=FakeFrontend(), market=market,
            profiles={MINT: clean_profile(TOKEN)}, clock=clock,
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 0
        assert history[0].launches_tracked == 1  # deferred, still tracked

        # The token gets indexed; the deferred candidate confirms next pass.
        market.pairs_by_address[MINT] = launch_pair
        clock.advance(seconds=121)
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1
        assert history[0].launches_tracked == 0  # confirmed and released


async def test_launch_stage_absent_without_clients():
    """Backward compatibility (Rule 18): scanners built without pump.fun
    clients behave exactly as before."""
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        async def fake_sleep(seconds):
            pass

        notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                      time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            SETTINGS, storage, notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address:
                                      clean_profile(pair.base_token)}),
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1
        assert history[0].launches_tracked == 0


async def test_launch_monitor_disabled_without_market_service():
    """Section 2: without independent confirmation the stage stays off."""
    async def fake_sleep(seconds):
        pass

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                      time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            SETTINGS, storage, notifier,
            gecko_client=FakeGecko([]), goplus_client=FakeGoPlus({}),
            pumpportal_client=FakeStream(), pumpfun_client=FakeFrontend(),
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        assert scanner._launch_monitor is None


# ---- Settings validation ----

def test_pumpfun_settings_validation():
    with pytest.raises(ConfigurationError):
        PumpFunSettings(max_creator_buy_percent=0.0)
    with pytest.raises(ConfigurationError):
        PumpFunSettings(max_creator_buy_percent=150.0)
    with pytest.raises(ConfigurationError):
        PumpFunSettings(launchpads="  ,  ")
    with pytest.raises(ConfigurationError):
        PumpFunSettings(min_usd_market_cap=-1.0)
    assert PumpFunSettings(launchpads="pump, bonk").launchpad_list == ["pump", "bonk"]


def test_pumpfun_settings_from_env():
    settings = Settings.from_env(env={
        "MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_PUMPFUN_MIN_REPLY_COUNT": "10",
    })
    assert settings.pumpfun.enable_in_monitor is True
    assert settings.pumpfun.min_reply_count == 10
