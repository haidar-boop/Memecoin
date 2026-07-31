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


def _profile(token, creator_address=None):
    return SecurityProfile(
        token=token, source="goplus", is_honeypot=False, cannot_sell_all=False,
        is_mintable=False, ownership_renounced=True, is_freezable=False,
        buy_tax_percent=0.0, sell_tax_percent=0.0, honeypot_same_creator_count=0,
        holder_count=2500, top_holder_percent=3.0, top10_holder_percent=22.0,
        creator_percent=1.5, lp_locked_percent=95.0,
        creator_address=creator_address)


class _FakeGecko:
    def __init__(self, pools):
        self.pools = pools

    async def get_new_pools(self, network, page=1):
        return self.pools if page == 1 else []


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
    # A verdict was stored too, so resolution can grade it (Sections 6/8):
    # without this the report-card metrics and adaptive ensemble weights
    # never updated in monitor-only operation.
    assert learning.store.get_prediction(coin_id) is not None
    learning.resolve_outcome(pair.base_token.address, "solana", 24.0, 10.0)
    assert learning._ensemble.final_samples == 1  # graded on resolution


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


# ---- Creator wallet + dev outflow (upgrade #2) ----

class _FlowAssessment:
    """Duck-typed stand-in exposing exactly what _creator_outflow_usd reads."""

    def __init__(self, net_flows):
        self.net_flows = net_flows


def test_creator_outflow_from_net_flows():
    from meme_intelligence.workflow.controller import _creator_outflow_usd

    flows = _FlowAssessment([("WalletA", 500.0), ("DevWallet", -3_000.0)])
    # Net seller -> positive outflow magnitude.
    assert _creator_outflow_usd(flows, "DevWallet") == 3_000.0
    # Net buyer -> observed zero outflow (a real observation, not unknown).
    assert _creator_outflow_usd(flows, "WalletA") == 0.0
    # Creator not seen trading in the window -> observed zero.
    assert _creator_outflow_usd(flows, "SomeoneElse") == 0.0
    # Unknown creator or no wallet data -> None (Rule 8).
    assert _creator_outflow_usd(flows, None) is None
    assert _creator_outflow_usd(None, "DevWallet") is None
    # EVM addresses compare case-insensitively; base58 stays exact.
    evm = _FlowAssessment([("0xDeAdBeEf", -100.0)])
    assert _creator_outflow_usd(evm, "0xdeadbeef") == 100.0
    assert _creator_outflow_usd(flows, "devwallet") == 0.0  # base58: no fold


async def test_scanner_persists_creator_and_blacklist_fires():
    """Creator flows from the security profile into the learning store, and a
    confirmed rug makes the NEXT coin by the same deployer fire the
    reputation signal (Section 5a end to end)."""
    pair = _pair()
    settings, learning = _learning(enable_in_monitor=True)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = _scanner(settings, storage, learning, [pair],
                           {pair.base_token.address: _profile(pair.base_token,
                                                              creator_address="devX")})
        await scanner.run(max_cycles=1)

    # Creator persisted on the coin record.
    coin_id = learning.store.coin_id(pair.base_token)
    record = learning.store.get_record(coin_id)
    assert record.creator == "devX"

    # The coin rugs -> deployer blacklisted -> next devX coin is flagged.
    learning.resolve_outcome(pair.base_token.address, "solana", 24.0, -95.0, is_rug=True)
    assert learning.store.deployer_rug_count("devX", "solana") == 1
    verdict = learning.evaluate_coin("NextCoin", "solana",
                                     [{"age_seconds": 0, "price_usd": 1.0}],
                                     creator="devX")
    assert "deployer_blacklisted" in verdict["rug_signals_fired"]


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


# ---- Final-hunt regression tests (parsing / pool / grading / AI guards) ----

async def test_get_token_pairs_drops_quote_side_pairs():
    """Bug-hunt: pairs where the queried token is the QUOTE side describe the
    counterparty token — keeping them let get_best_pair analyze/alert under
    the wrong token entirely."""
    from meme_intelligence.collectors.market_data import DexScreenerClient

    client = DexScreenerClient.__new__(DexScreenerClient)

    async def fake_get_json(*a, **k):
        def p(base_addr, quote_addr, liq):
            return {"chainId": "solana", "pairAddress": f"pool{base_addr}",
                    "baseToken": {"address": base_addr, "symbol": "B"},
                    "quoteToken": {"address": quote_addr, "symbol": "Q"},
                    "liquidity": {"usd": liq}}
        return {"pairs": [p("TOK", "SOL", 50_000),      # queried token is base
                          p("OTHER", "TOK", 80_000)]}   # queried token is QUOTE
    client._get_json = fake_get_json
    import logging
    client._logger = logging.getLogger("t")
    client.name = "dexscreener"

    pairs = await client.get_token_pairs("TOK")
    assert [p.base_token.address for p in pairs] == ["TOK"]


def test_geckoterminal_chain_canonicalized():
    """Bug-hunt: GT network ids (eth, polygon_pos) leaked into DexPair.chain,
    breaking cross-provider verification and CoinGecko lookups off-Solana."""
    from meme_intelligence.collectors.market_data import GeckoTerminalClient

    item = {"attributes": {"address": "PoolX", "name": "X / WETH",
                           "reserve_in_usd": "1000"},
            "relationships": {"base_token": {"data": {"id": "eth_0xabc"}}}}
    pair = GeckoTerminalClient._parse_pool(item)
    assert pair.chain == "ethereum"
    assert pair.base_token.chain == "ethereum"


async def test_permanent_404_does_not_poison_provider_health():
    """Bug-hunt: 3 unindexed-token 404s in a row put a HEALTHY provider on
    cooldown, blacking out the whole pool for every token."""
    from meme_intelligence.core.errors import CollectorError, TransientCollectorError
    from meme_intelligence.core.provider_pool import ProviderPool

    class NotIndexed:
        name = "gecko"
        async def get_token_pairs(self, addr, chain=None):
            raise CollectorError("404 not indexed", status_code=404)

    class Down:
        name = "dex"
        async def get_token_pairs(self, addr, chain=None):
            raise TransientCollectorError("boom")

    pool = ProviderPool([NotIndexed(), Down()], failure_threshold=3)
    for _ in range(5):
        try:
            await pool.call("get_token_pairs", "X")
        except Exception:
            pass
    health = {h.name: h for h in pool.health()}
    assert health["gecko"].healthy is True      # 404s never cool it down
    assert health["dex"].healthy is False        # real outage still does


def test_helius_token_amount_falls_back_when_uiamount_null():
    from meme_intelligence.collectors.wallet_data import _token_amount

    assert _token_amount({"uiAmount": None, "amount": "1000000000",
                          "decimals": 6}) == 1000.0
    assert _token_amount({"uiAmount": None, "uiAmountString": "42.5"}) == 42.5
    assert _token_amount({"uiAmount": 7.0}) == 7.0
    assert _token_amount("junk") is None
