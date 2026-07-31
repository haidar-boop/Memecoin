"""Tests for the live holdings rug guard (operator request 2026-07-29).

This is the only component that moves real money with no human tap, so most of
these tests are about it NOT trading: on unknown data, on a single bad tick,
when disarmed, or twice for the same position.
"""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.analyzers.rug_watch import EXIT, HOLD, WARN
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.errors import AllProvidersFailedError, CollectorError
from meme_intelligence.core.models import DexPair, TokenIdentity
from meme_intelligence.workflow.holdings_guard import HoldingsGuard

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
MINT = "So1MemeToken111111111111111111111111111111"
TOKEN = TokenIdentity(chain="solana", address=MINT, symbol="MEME")


def make_settings(**overrides):
    env = {"MEMEINTEL_RUG_WATCH_ENABLED": "true"}
    env.update(overrides)
    return Settings.from_env(env=env)


class FakeStorage:
    def __init__(self, holdings=None):
        self._holdings = holdings if holdings is not None else [
            {"address": MINT, "chain": "solana", "symbol": "MEME"}]

    def get_holdings(self, active_only=True):
        return list(self._holdings)


class FakeMarket:
    """Serves a scripted liquidity sequence; `None` raises (unreadable)."""

    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.calls = 0

    async def get_token_pairs_confirmed(self, address, chain=None):
        value = self._sequence[min(self.calls, len(self._sequence) - 1)]
        self.calls += 1
        if value == "fail":
            raise AllProvidersFailedError("get_token_pairs", {"dexscreener": OSError("down")})
        if value == "empty":
            return []
        return [DexPair(chain="solana", pair_address="Pool1", base_token=TOKEN,
                        price_usd=1.0, liquidity_usd=value)]


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def dispatch(self, events):
        self.events.extend(events)
        return list(events)

    @property
    def types(self):
        return [e.alert_type for e in self.events]


class FakeExecutor:
    live = True  # the executors' real capability flag (2026-07-31 port fix:
    # the original fake defined a nonexistent ``enabled``, masking a guard
    # bug that would have prevented every real auto-sell)

    def __init__(self, result="DUMP confirmed."):
        self.sells = []
        self._result = result

    async def execute_sell_all(self, mint, chain="solana"):
        self.sells.append((mint, chain))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def make_guard(sequence, *, settings=None, executor=None, holdings=None,
               notifier=None):
    settings = settings or make_settings()
    return HoldingsGuard(
        settings, FakeStorage(holdings), notifier or FakeNotifier(),
        FakeMarket(sequence), executor=executor,
        now_func=lambda: NOW)


async def run_polls(guard, count):
    verdicts = []
    for _ in range(count):
        verdicts.extend(v for _t, v in await guard.poll_once())
    return verdicts


# ---- It sells when it should ----


async def test_a_confirmed_drain_is_auto_sold_when_armed():
    """The scenario that cost the operator $20: liquidity leaves the pool."""
    executor = FakeExecutor()
    notifier = FakeNotifier()
    guard = make_guard(
        [50_000.0, 48_000.0, 9_000.0, 8_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor, notifier=notifier)
    verdicts = await run_polls(guard, 4)
    assert verdicts[-1].action == EXIT
    assert executor.sells == [(MINT, "solana")]
    assert "rug_watch_exit" in notifier.types
    assert any("DUMP confirmed" in r for e in notifier.events for r in e.reasons)


async def test_a_position_is_only_ever_sold_once():
    """Recorded before the trade, so a crash mid-sell cannot double-sell."""
    executor = FakeExecutor()
    guard = make_guard(
        [50_000.0, 48_000.0, 9_000.0, 8_000.0, 7_000.0, 6_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor)
    await run_polls(guard, 6)
    assert len(executor.sells) == 1


async def test_a_failed_sell_still_tells_the_operator_to_act():
    executor = FakeExecutor(result=CollectorError("jupiter down"))
    notifier = FakeNotifier()
    guard = make_guard(
        [50_000.0, 48_000.0, 9_000.0, 8_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor, notifier=notifier)
    await run_polls(guard, 4)
    assert any("/dump manually NOW" in r for e in notifier.events for r in e.reasons)


# ---- It does NOT sell when it shouldn't ----


async def test_it_never_sells_while_auto_sell_is_off():
    """enabled without auto_sell = watch and warn only."""
    executor = FakeExecutor()
    notifier = FakeNotifier()
    guard = make_guard([50_000.0, 48_000.0, 9_000.0, 8_000.0],
                       executor=executor, notifier=notifier)
    await run_polls(guard, 4)
    assert executor.sells == []
    assert "rug_watch_exit" in notifier.types
    assert any("Auto-sell is OFF" in r for e in notifier.events for r in e.reasons)


async def test_an_unreadable_provider_never_sells():
    """Rule 8 — the failure mode that once fabricated -100% 'deaths'. Here it
    would liquidate a healthy position."""
    executor = FakeExecutor()
    guard = make_guard(
        [50_000.0, 48_000.0, "fail", "fail", "fail", "fail"],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor)
    verdicts = await run_polls(guard, 6)
    assert executor.sells == []
    assert verdicts[-1].action == HOLD


async def test_a_single_bad_tick_never_sells():
    executor = FakeExecutor()
    guard = make_guard(
        [50_000.0, 49_000.0, 4_000.0, 48_000.0, 47_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor)
    await run_polls(guard, 5)
    assert executor.sells == []


async def test_ordinary_volatility_never_sells():
    executor = FakeExecutor()
    guard = make_guard(
        [50_000.0, 44_000.0, 41_000.0, 43_000.0, 40_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor)
    await run_polls(guard, 5)
    assert executor.sells == []


async def test_an_empty_pool_from_every_provider_is_a_measured_zero():
    """"Every provider agrees there is no pair" IS evidence, unlike a failed
    read — the distinction the confirmed-empty lookup exists to make."""
    executor = FakeExecutor()
    guard = make_guard(
        [50_000.0, 48_000.0, "empty", "empty"],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor)
    await run_polls(guard, 4)
    assert executor.sells == [(MINT, "solana")]


async def test_the_kill_switch_stops_selling_but_keeps_watching():
    executor = FakeExecutor()
    notifier = FakeNotifier()
    guard = make_guard(
        [50_000.0, 48_000.0, 9_000.0, 8_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor, notifier=notifier)
    assert guard.armed
    guard.disarm("operator sent /rugwatch off")
    assert not guard.armed
    await run_polls(guard, 4)
    assert executor.sells == []
    assert "rug_watch_exit" in notifier.types      # still told


async def test_no_executor_configured_alerts_instead_of_crashing():
    notifier = FakeNotifier()
    guard = make_guard(
        [50_000.0, 48_000.0, 9_000.0, 8_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=None, notifier=notifier)
    await run_polls(guard, 4)
    assert any("Trading is not configured" in r
               for e in notifier.events for r in e.reasons)


# ---- Robustness ----


async def test_a_storage_failure_is_survived():
    class BrokenStorage:
        def get_holdings(self, active_only=True):
            raise RuntimeError("database is locked")

    guard = HoldingsGuard(make_settings(), BrokenStorage(), FakeNotifier(),
                          FakeMarket([50_000.0]), now_func=lambda: NOW)
    assert await guard.poll_once() == []


async def test_no_holdings_is_a_quiet_no_op():
    notifier = FakeNotifier()
    guard = make_guard([50_000.0], holdings=[], notifier=notifier)
    assert await guard.poll_once() == []
    assert notifier.events == []


async def test_more_positions_than_the_cap_are_reported_not_silently_dropped(caplog):
    import logging

    holdings = [{"address": f"Mint{i}", "chain": "solana", "symbol": f"M{i}"}
                for i in range(25)]
    guard = make_guard([50_000.0], holdings=holdings)
    with caplog.at_level(logging.WARNING,
                         logger="meme_intelligence.workflow.holdings_guard"):
        await guard.poll_once()
    assert "not watched this pass" in caplog.text


async def test_the_guard_does_not_start_when_disabled():
    guard = HoldingsGuard(Settings.from_env(env={}), FakeStorage(), FakeNotifier(),
                          FakeMarket([50_000.0]), now_func=lambda: NOW)
    await guard.start()
    assert guard._task is None
    await guard.stop()


async def test_status_reports_what_it_is_doing():
    guard = make_guard([50_000.0, 48_000.0],
                       settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"))
    await run_polls(guard, 2)
    status = guard.status()
    assert status["enabled"] is True and status["auto_sell"] is True
    assert status["watching"] == 1


async def test_status_snapshot_surfaces_the_guard():
    """/status must show whether the thing that can sell your coins is armed."""
    from meme_intelligence.workflow.controller import ContinuousScanner

    scanner = ContinuousScanner.__new__(ContinuousScanner)
    scanner._holdings_guard = None
    assert scanner._holdings_guard is None

    guard = make_guard([50_000.0],
                       settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"))
    scanner.set_holdings_guard(guard)
    assert scanner._holdings_guard.status()["auto_sell"] is True


# ---- The live-flag port fix (2026-07-31): the original guard checked a
# nonexistent ``executor.enabled`` and would NEVER have auto-sold. These two
# tests pin the fix against the REAL executor classes, not fakes. ----


async def test_a_real_dry_run_executor_is_reported_as_not_configured():
    """An armed guard holding a DryRunExecutor must say trading is not
    configured (a simulated trade must never be reported as an AUTO-SOLD),
    and must not crash."""
    from meme_intelligence.database.storage import Storage
    from meme_intelligence.trading.execution import DryRunExecutor

    notifier = FakeNotifier()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        guard = make_guard(
            [50_000.0, 48_000.0, 9_000.0, 8_000.0],
            settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
            executor=DryRunExecutor(storage), notifier=notifier)
        await run_polls(guard, 4)
    assert "rug_watch_exit" in notifier.types
    exit_event = next(e for e in notifier.events if e.alert_type == "rug_watch_exit")
    assert any("Trading is not configured" in r for r in exit_event.reasons)
    assert not any("AUTO-SOLD" in e.title for e in notifier.events)


async def test_a_live_flagged_executor_actually_sells():
    """The mirror: an object exposing ONLY the real ``live`` capability flag
    (no ``enabled``) is trusted with the sell — proving the guard keys off
    the flag the executors actually define."""
    class MinimalLive:
        live = True

        def __init__(self):
            self.sells = []

        async def execute_sell_all(self, mint, chain="solana"):
            self.sells.append((mint, chain))
            return "DUMP confirmed."

    executor = MinimalLive()
    guard = make_guard(
        [50_000.0, 48_000.0, 9_000.0, 8_000.0],
        settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        executor=executor)
    await run_polls(guard, 4)
    assert executor.sells == [(TOKEN.address, "solana")]


# ---- 2026-07-31 adversarial-review fixes (guard level) ----


class MultiPoolMarket:
    """Serves scripted per-poll pair LISTS (each item: list of (addr, liq))."""

    def __init__(self, polls):
        self._polls = list(polls)
        self.calls = 0

    async def get_token_pairs_confirmed(self, address, chain=None):
        pools = self._polls[min(self.calls, len(self._polls) - 1)]
        self.calls += 1
        return [DexPair(chain="solana", pair_address=addr, base_token=TOKEN,
                        price_usd=1.0, liquidity_usd=liq)
                for addr, liq in pools]


def make_guard_with_market(market, *, settings=None, executor=None,
                           notifier=None):
    return HoldingsGuard(
        settings or make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
        FakeStorage(None), notifier or FakeNotifier(), market,
        executor=executor, now_func=lambda: NOW)


async def test_a_vanishing_main_pool_field_never_fabricates_a_collapse():
    """CONFIRMED review finding: main pool $80k + side pool $2k, then one
    response where the main pool's liquidity is null — max() used to pick the
    $2k side pool as a MEASURED reading and sell on a fabricated 97% drop.
    The tracked-pool read must degrade to unknown instead."""
    executor = FakeExecutor()
    market = MultiPoolMarket([
        [("Main", 80_000.0), ("Side", 2_000.0)],
        [("Main", 80_000.0), ("Side", 2_000.0)],
        [("Main", None), ("Side", 2_000.0)],     # the glitch
        [("Main", None), ("Side", 2_000.0)],
        [("Main", 80_000.0), ("Side", 2_000.0)], # provider recovers
    ])
    guard = make_guard_with_market(market, executor=executor)
    for _ in range(5):
        await guard.poll_once()
    assert executor.sells == []                   # unknown, never a $2k "measurement"


async def test_provider_failover_to_a_smaller_pool_universe_never_sells():
    """CONFIRMED review finding: failover to a provider that does not carry
    the deepest pool used to look like a >55% drain. A response without the
    tracked pool is unknown."""
    executor = FakeExecutor()
    market = MultiPoolMarket([
        [("Main", 80_000.0)],
        [("Main", 78_000.0)],
        [("OtherVenue", 20_000.0)],               # failover: different pool
        [("OtherVenue", 20_000.0)],
        [("OtherVenue", 20_000.0)],
    ])
    guard = make_guard_with_market(market, executor=executor)
    for _ in range(5):
        await guard.poll_once()
    assert executor.sells == []


async def test_the_tracked_pool_actually_draining_still_sells():
    """The pool-identity fix must not blind the guard to a real rug on the
    tracked pool itself."""
    executor = FakeExecutor()
    market = MultiPoolMarket([
        [("Main", 80_000.0), ("Side", 2_000.0)],
        [("Main", 78_000.0), ("Side", 2_000.0)],
        [("Main", 8_000.0), ("Side", 2_000.0)],
        [("Main", 7_000.0), ("Side", 2_000.0)],
    ])
    guard = make_guard_with_market(market, executor=executor)
    for _ in range(4):
        await guard.poll_once()
    assert executor.sells == [(MINT, "solana")]


async def test_poll_cadence_is_clamped_to_the_cache_ttl():
    """CONFIRMED review finding: polling below the 30s HTTP cache TTL made two
    'confirmations' out of one cached measurement. The guard clamps."""
    settings = make_settings(MEMEINTEL_RUG_WATCH_POLL_SECONDS="10")
    guard = HoldingsGuard(settings, FakeStorage(None), FakeNotifier(),
                          FakeMarket([50_000.0]), now_func=lambda: NOW)
    assert guard._poll_seconds == settings.http.cache_ttl_seconds  # 30, not 10


async def test_confirmed_drain_with_route_gone_escalates_to_critical():
    """CONFIRMED review finding: an armed guard blocked by a vanished sell
    route used to send a HIGH warning promising it 'will auto-sell' — a false
    promise. That state must be a CRITICAL rug_watch_exit telling the
    operator to /dump manually, and must NOT mark the coin exited (if the
    route returns while the drain holds, the real auto-sell still fires)."""
    class RouteGoneMarket(FakeMarket):
        pass

    executor = FakeExecutor()
    notifier = FakeNotifier()
    guard = make_guard([50_000.0, 48_000.0, 9_000.0, 8_000.0],
                       settings=make_settings(MEMEINTEL_RUG_WATCH_AUTO_SELL="true"),
                       executor=executor, notifier=notifier)
    # Force route=False onto every reading via the jupiter probe seam:
    class NoRouteJupiter:
        async def check_round_trip_liquidity(self, mint, probe_sol_amount,
                                             slippage_bps):
            from types import SimpleNamespace
            return SimpleNamespace(live_buy_route_found=True,
                                   live_sell_route_found=False)

    guard._jupiter = NoRouteJupiter()
    await run_polls(guard, 4)
    assert executor.sells == []                       # nothing to execute
    criticals = [e for e in notifier.events
                 if e.alert_type == "rug_watch_exit"]
    assert criticals and any("route" in r for e in criticals for r in e.reasons)
    assert not any("will auto-sell" in r for e in criticals for r in e.reasons)
    assert MINT not in guard._exited                  # route back => can still sell
