"""Tests for the DexScreener boost radar (Project 5)."""

import asyncio

from meme_intelligence.collectors.market_data import TokenBoost
from meme_intelligence.config.settings import BoostWatcherSettings
from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.workflow.boost_watcher import BoostWatcher


def make_boost(addr, total, chain="solana") -> TokenBoost:
    return TokenBoost(chain=chain, token_address=addr, total_amount=float(total))


class FakeDex:
    """Serves a scripted sequence of boost batches (one per poll)."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    async def get_boosts(self):
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        return batch


class FakeNotifier:
    def __init__(self):
        self.dispatched = []

    async def dispatch(self, events):
        self.dispatched.extend(events)


def make_watcher(dex, notifier, **overrides) -> BoostWatcher:
    settings = BoostWatcherSettings(enabled=True, **overrides)
    return BoostWatcher(dex, notifier, settings)


async def test_prime_pass_records_baseline_without_alerting():
    """Enabling the watcher must not dump the whole current boosted set onto
    the phone — the first poll only records the baseline."""
    dex = FakeDex([[make_boost("AAA", 500)]])   # already boosted at enable time
    notifier = FakeNotifier()
    fresh = await make_watcher(dex, notifier).poll_once()
    assert fresh == []
    assert notifier.dispatched == []


async def test_new_crossing_alerts_once():
    dex = FakeDex([[], [make_boost("BBB", 150)], [make_boost("BBB", 150)]])
    notifier = FakeNotifier()
    watcher = make_watcher(dex, notifier)

    await watcher.poll_once()                       # prime (empty baseline)
    fresh = await watcher.poll_once()               # BBB crosses -> alert
    assert [b.token_address for b in fresh] == ["BBB"]
    assert len(notifier.dispatched) == 1
    event = notifier.dispatched[0]
    assert event.alert_type == "boost"
    assert event.priority is AlertPriority.MEDIUM
    assert "150" in event.title
    assert "endorsement" in event.why_it_matters.lower()   # honest caveat carried

    await watcher.poll_once()                       # BBB still boosted -> NO re-alert
    assert len(notifier.dispatched) == 1


async def test_below_threshold_never_alerts():
    dex = FakeDex([[], [make_boost("CCC", 50)], [make_boost("CCC", 99)]])
    notifier = FakeNotifier()
    watcher = make_watcher(dex, notifier)
    for _ in range(3):
        await watcher.poll_once()
    assert notifier.dispatched == []


async def test_chain_filter_excludes_other_chains():
    dex = FakeDex([[], [make_boost("ETHTOK", 500, chain="ethereum"),
                        make_boost("SOLTOK", 500, chain="solana")]])
    notifier = FakeNotifier()
    watcher = make_watcher(dex, notifier, chain_filter="solana")
    await watcher.poll_once()                       # prime
    fresh = await watcher.poll_once()
    assert [b.token_address for b in fresh] == ["SOLTOK"]
    assert len(notifier.dispatched) == 1


async def test_empty_chain_filter_allows_all_chains():
    dex = FakeDex([[], [make_boost("ETHTOK", 500, chain="ethereum")]])
    notifier = FakeNotifier()
    watcher = make_watcher(dex, notifier, chain_filter="")
    await watcher.poll_once()
    fresh = await watcher.poll_once()
    assert [b.token_address for b in fresh] == ["ETHTOK"]


async def test_poll_loop_survives_fetch_errors():
    """A DexScreener hiccup must never propagate out of the loop — it backs
    off and keeps running (Rule 7)."""
    class BoomDex:
        async def get_boosts(self):
            raise RuntimeError("dexscreener down")

    sleeps: list[float] = []
    notifier = FakeNotifier()
    watcher = BoostWatcher(BoomDex(), notifier, BoostWatcherSettings(enabled=True))

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            watcher._stopping = True   # let the loop exit after a couple of backoffs

    watcher._sleep = fake_sleep
    await watcher._poll_forever()      # must return without raising
    assert sleeps and sleeps[1] > sleeps[0]   # exponential backoff
    assert notifier.dispatched == []


async def test_start_and_close_lifecycle():
    dex = FakeDex([[]])                 # always empty
    notifier = FakeNotifier()
    watcher = make_watcher(dex, notifier, poll_interval_seconds=0.01)
    await watcher.start()
    await asyncio.sleep(0)              # let the task schedule
    await watcher.close()
    assert watcher._task is None
