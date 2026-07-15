"""Tests for the continuous scanning controller (Spec Part 13)."""

import dataclasses as _dc
from datetime import datetime, timedelta, timezone

from meme_intelligence.alerts.notification_engine import (
    _BUY_SIDE_ALERT_TYPES,
    NotificationEngine,
)
from meme_intelligence.config.settings import AlertEngineSettings, Settings
from meme_intelligence.core.errors import TransientCollectorError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.database.storage import Storage
from meme_intelligence.workflow.controller import ContinuousScanner

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings.from_env(env={})


def make_pair(address="TokenA", symbol="MEMA") -> DexPair:
    token = TokenIdentity(chain="solana", address=address, symbol=symbol)
    return DexPair(
        chain="solana", pair_address=f"Pool{address}", base_token=token,
        # Under the (now default-ON) buy-side ceilings: mcap <= $100k,
        # liquidity <= $50k — the profile the operator wants pitched.
        market_cap=80_000.0, fdv=84_000.0, liquidity_usd=45_000.0,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )


def clean_profile(token: TokenIdentity) -> SecurityProfile:
    return SecurityProfile(
        token=token, source="goplus",
        is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
        is_open_source=True, is_proxy=False, is_mintable=False,
        ownership_renounced=True, hidden_owner=False, can_take_back_ownership=False,
        has_blacklist=False, trading_pausable=False, is_freezable=False,
        balance_mutable=False, selfdestruct=False,
        buy_tax_percent=0.0, sell_tax_percent=0.0, tax_modifiable=False,
        fake_token=False, is_airdrop_scam=False, anti_whale_modifiable=False,
        slippage_modifiable=False, personal_slippage_modifiable=False,
        trading_cooldown=False, honeypot_same_creator_count=0,
        holder_count=2500, top_holder_percent=3.0, top10_holder_percent=22.0,
        creator_percent=1.5, owner_percent=0.0, lp_locked_percent=95.0,
    )


class FakeGecko:
    def __init__(self, pools, fail_on_call: int | None = None):
        self.pools = pools
        self.calls = 0
        self.fail_on_call = fail_on_call

    async def get_new_pools(self, network):
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise TransientCollectorError("provider hiccup")
        return self.pools


class FakeGoPlus:
    def __init__(self, profiles):
        self.profiles = profiles

    async def get_token_security(self, chain, address):
        return self.profiles.get(address)


class RecordingSink:
    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(event)


def make_scanner(storage, pools, profiles, fail_on_call=None, sink=None):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    notifier = NotificationEngine([sink or RecordingSink()], AlertEngineSettings(),
                                  time_func=lambda: 0.0)
    scanner = ContinuousScanner(
        SETTINGS, storage, notifier,
        gecko_client=FakeGecko(pools, fail_on_call=fail_on_call),
        goplus_client=FakeGoPlus(profiles),
        now_func=lambda: NOW,
        sleep_func=fake_sleep,
    )
    return scanner, sleeps


async def test_bounded_run_analyzes_and_persists():
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair],
                                  {pair.base_token.address: clean_profile(pair.base_token)})
        history = await scanner.run(max_cycles=2)

        assert len(history) == 2
        assert history[0].analyzed == 1
        assert history[1].analyzed == 0  # seen-set prevents re-analysis
        assert storage.score_history(pair.base_token)
        assert storage.get_watchlist()  # tiered in


async def test_alerts_dispatched_and_journaled():
    pair = make_pair()
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair],
                                  {pair.base_token.address: clean_profile(pair.base_token)},
                                  sink=sink)
        history = await scanner.run(max_cycles=1)

        assert history[0].alerts  # provisional opportunity alert fired
        assert sink.sent
        journal = storage.journal_entries(pair.base_token)
        assert any(e["kind"] == "alert" for e in journal)


async def test_discovery_outage_isolated_cycle_still_completes():
    """Bug-hunt: a GeckoTerminal outage used to abort the WHOLE cycle into
    the outer backoff, stalling the launch funnel, watchlist rechecks, and
    security-change detection whose providers were fine (Rule 9). Discovery
    failure now yields an empty candidate list and the cycle completes."""
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, sleeps = make_scanner(
            storage, [pair],
            {pair.base_token.address: clean_profile(pair.base_token)},
            fail_on_call=1,  # first discovery call raises
        )
        history = await scanner.run(max_cycles=2)

        # Both cycles completed; the outage cycle simply saw no pools.
        assert len(history) == 2
        assert history[0].pools_seen == 0 and history[0].analyzed == 0
        assert history[1].analyzed == 1  # discovery recovered next cycle
        assert 5.0 not in sleeps  # no error backoff was needed


async def test_failed_cycle_backs_off_and_recovers():
    """The outer backoff still guards non-provider cycle failures (Rule 7)."""
    from meme_intelligence.core.errors import MemeIntelError

    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, sleeps = make_scanner(
            storage, [pair],
            {pair.base_token.address: clean_profile(pair.base_token)})
        real_run_cycle = scanner._run_cycle
        calls = {"n": 0}

        async def flaky_cycle(cycle):
            calls["n"] += 1
            if calls["n"] == 1:
                raise MemeIntelError("simulated storage failure")
            return await real_run_cycle(cycle)

        scanner._run_cycle = flaky_cycle
        history = await scanner.run(max_cycles=2)

        # cycle 1 failed (no stats), cycle 2 succeeded after backoff
        assert len(history) == 1
        assert history[0].analyzed == 1
        assert sleeps and sleeps[0] == 5.0  # error backoff, not the normal interval


async def test_raw_non_project_exception_also_backs_off_not_crashes():
    """Bug-hunt 2026-07-12: the backstop used to catch ONLY MemeIntelError, so
    a RAW exception a lower layer forgot to wrap (e.g. sqlite3.OperationalError
    on a full disk, or any RuntimeError) escaped and killed the 24/7 loop --
    stranding the operator with no way to /dump. The catch is now `Exception`."""
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, sleeps = make_scanner(
            storage, [pair],
            {pair.base_token.address: clean_profile(pair.base_token)})
        real_run_cycle = scanner._run_cycle
        calls = {"n": 0}

        async def flaky_cycle(cycle):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("unwrapped sqlite failure")  # NOT a MemeIntelError
            return await real_run_cycle(cycle)

        scanner._run_cycle = flaky_cycle
        history = await scanner.run(max_cycles=2)     # must not raise
        assert len(history) == 1                       # cycle 1 swallowed, cycle 2 ran
        assert history[0].analyzed == 1
        assert sleeps and sleeps[0] == 5.0             # backed off, did not crash


async def test_seen_cache_is_bounded():
    """Bug-hunt: _seen / _ai_verified grew one entry per token forever. They
    are now capacity-bounded (FIFO eviction) so weeks of scanning can't leak
    memory on the 1GB droplet."""
    from meme_intelligence.workflow.controller import _BoundedKeySet

    s = _BoundedKeySet(capacity=3)
    for i in range(10):
        s.add((f"c{i}", "solana"))
    assert len(s) == 3
    assert ("c9", "solana") in s          # newest kept
    assert ("c0", "solana") not in s      # oldest evicted
    # Value-carrying use (the AI-verified cache) round-trips within capacity.
    s.add(("v", "solana"), "inconclusive")
    assert s.get(("v", "solana")) == "inconclusive"
    assert s.get(("missing", "solana"), False) is False


async def test_request_stop_ends_loop():
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair],
                                  {pair.base_token.address: clean_profile(pair.base_token)})
        scanner.request_stop()
        history = await scanner.run(max_cycles=10)
        assert history == []  # stopped before the first cycle


async def test_unindexed_tokens_skipped():
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair], {})  # goplus knows nothing
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 0
        assert storage.get_watchlist() == []


# ---- Part 15: watchlist recheck cadence + multi-source verification ----

from meme_intelligence.core.enums import AlertPriority, WatchlistTier
from meme_intelligence.core.models import TokenIdentity


class FakeMarketService:
    """Market service double: serves best pairs and a fixed verification verdict."""

    def __init__(self, pairs_by_address=None, verdict=(None, "no second source"),
                 all_providers_down=False):
        self.pairs_by_address = pairs_by_address or {}
        self.verdict = verdict
        self.recheck_calls = 0
        self.verify_calls = 0
        self._all_providers_down = all_providers_down

    async def get_best_pair(self, address, chain=None):
        self.recheck_calls += 1
        return self.pairs_by_address.get(address)

    async def get_token_pairs(self, address, chain=None):
        # Mirrors the real service: raises when every provider is down,
        # returns [] when the token genuinely has no pairs.
        from meme_intelligence.core.errors import AllProvidersFailedError
        self.recheck_calls += 1
        if self._all_providers_down:
            raise AllProvidersFailedError("get_token_pairs", {"dexscreener": Exception("down")})
        pair = self.pairs_by_address.get(address)
        return [pair] if pair is not None else []

    async def cross_check_liquidity(self, pair):
        self.verify_calls += 1
        return self.verdict

    def health(self):
        from meme_intelligence.core.provider_pool import ProviderHealth
        if self._all_providers_down:
            return [ProviderHealth(name="dexscreener", healthy=False,
                                   consecutive_failures=5, total_failures=5,
                                   total_successes=0, cooldown_remaining=45.0)]
        return [ProviderHealth(name="dexscreener", healthy=True,
                               consecutive_failures=0, total_failures=0,
                               total_successes=10)]


def fast_recheck_settings() -> Settings:
    return Settings.from_env(env={"MEMEINTEL_WORKFLOW_WATCHLIST_RECHECK_CYCLES": "1"})


def make_scanner_with_market(storage, pools, profiles, market, settings=None, sink=None):
    async def fake_sleep(seconds):
        pass

    notifier = NotificationEngine([sink or RecordingSink()], AlertEngineSettings(),
                                  time_func=lambda: 0.0)
    return ContinuousScanner(
        settings or SETTINGS, storage, notifier,
        gecko_client=FakeGecko(pools),
        goplus_client=FakeGoPlus(profiles),
        market_service=market,
        now_func=lambda: NOW,
        sleep_func=fake_sleep,
    )


async def test_watchlist_recheck_reanalyzes_tracked_tokens():
    tracked = TokenIdentity(chain="solana", address="TokenTracked", symbol="TRK")
    tracked_pair = make_pair(address="TokenTracked", symbol="TRK")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tracked, WatchlistTier.TIER_1_HIGH_PRIORITY, score=80.0)
        market = FakeMarketService({"TokenTracked": tracked_pair})
        scanner = make_scanner_with_market(
            storage, [], {"TokenTracked": clean_profile(tracked)},
            market, settings=fast_recheck_settings(),
        )
        history = await scanner.run(max_cycles=1)
        assert market.recheck_calls == 1
        assert history[0].analyzed == 1  # the tracked token was re-analyzed
        assert len(storage.score_history(tracked)) == 1


async def test_recheck_archives_token_with_no_pairs():
    dead = TokenIdentity(chain="solana", address="TokenDead", symbol="DEAD")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(dead, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        scanner = make_scanner_with_market(
            storage, [], {}, FakeMarketService({}), settings=fast_recheck_settings(),
        )
        await scanner.run(max_cycles=1)
        assert storage.get_watchlist() == []
        archived = storage.get_watchlist(include_archived=True)
        assert archived and archived[0].tier is WatchlistTier.ARCHIVED


async def test_transient_provider_outage_does_not_archive_healthy_token():
    """Bug-hunt: get_best_pair returning None during a total provider
    outage (AllProvidersFailedError) was indistinguishable from a token
    genuinely having no pairs left, so a transient outage silently
    archived healthy watchlist tokens."""
    alive = TokenIdentity(chain="solana", address="TokenAlive", symbol="OK")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(alive, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        market = FakeMarketService({}, all_providers_down=True)  # outage, not death
        scanner = make_scanner_with_market(
            storage, [], {}, market, settings=fast_recheck_settings(),
        )
        await scanner.run(max_cycles=1)
        assert storage.get_watchlist() != []  # still tracked, not archived
        archived = storage.get_watchlist(include_archived=True)
        assert not any(e.tier is WatchlistTier.ARCHIVED for e in archived)


# The healthy make_pair() fixture scores ~93 overall with only community
# unverified, so its opportunity alert is a HIGH strong_candidate.
_CANDIDATE_TYPES = {"high_priority_opportunity", "strong_candidate", "early_opportunity"}


async def test_source_disagreement_downgrades_opportunity_alert():
    """Part 15 Section 10: important events need multi-source confirmation."""
    pair = make_pair()
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService(verdict=(False, "sources disagree on liquidity"))
        scanner = make_scanner_with_market(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            market, sink=sink,
        )
        await scanner.run(max_cycles=1)
        assert market.verify_calls >= 1
        candidate = [e for e in sink.sent if e.alert_type in _CANDIDATE_TYPES]
        assert candidate
        # a HIGH strong_candidate downgrades one step to MEDIUM on disagreement
        assert candidate[0].priority is AlertPriority.MEDIUM
        assert any("DOWNGRADED" in r for r in candidate[0].reasons)


async def test_source_agreement_annotates_alert():
    pair = make_pair()
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService(verdict=(True, "liquidity confirmed by verifier"))
        scanner = make_scanner_with_market(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            market, sink=sink,
        )
        await scanner.run(max_cycles=1)
        candidate = [e for e in sink.sent if e.alert_type in _CANDIDATE_TYPES]
        assert candidate
        assert any("confirmed" in r for r in candidate[0].reasons)


async def test_strong_fresh_token_reaches_sink_at_high_priority():
    """The whole point of the strong_candidate tier: a fresh launch with
    NO community data but strong measurable gates produces a HIGH alert,
    so it survives an external_min_priority=high delivery filter that was
    hiding every MEDIUM opportunity signal."""
    pair = make_pair()  # ~93 overall, no community client -> community unverified
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService(verdict=(True, "confirmed"))
        scanner = make_scanner_with_market(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            market, sink=sink,
        )
        await scanner.run(max_cycles=1)
        strong = [e for e in sink.sent if e.alert_type == "strong_candidate"]
        assert strong
        assert strong[0].priority is AlertPriority.HIGH


# ---- Part 32.5 S8: AI verification of gate-passing opportunities ----

from meme_intelligence.ai.reasoning import AIJudgment
from meme_intelligence.analyzers.foundation_analyzer import FoundationInputs
from meme_intelligence.analyzers.narrative_analyzer import NarrativeInputs
from meme_intelligence.core.enums import ResearchMode
from meme_intelligence.core.models import CommunityProfile


class FakeCommunity:
    async def get_community_profile(self, token):
        # Field values mirror tests/test_community_analyzer.healthy_profile
        # (scores >= 85, not artificial) so the community gate passes with data.
        return CommunityProfile(
            token=token, source="test",
            twitter_followers=25000, twitter_engagement_rate_percent=6.0,
            twitter_growth_rate_7d_percent=40.0, bot_follower_percent=5.0,
            telegram_members=8000, telegram_active_members=1600,
            telegram_admin_only_talk=False, duplicate_message_percent=2.0,
            discord_members=3000, discord_active_percent=18.0,
            member_retention_30d_percent=85.0, positive_sentiment_percent=75.0,
            user_content_per_day=30.0, dev_updates_per_week=4.0,
            dev_responds_to_community=True,
        )


class VerifierAI:
    def __init__(self, judgment=None):
        self.judgment = judgment
        self.judge_calls = 0

    async def judge(self, result, *, mode):
        self.judge_calls += 1
        return self.judgment


def weak_narrative_judgment() -> AIJudgment:
    return AIJudgment(
        foundation_inputs=FoundationInputs(),
        narrative_inputs=NarrativeInputs(
            memorability=5.0, shareability=5.0, emotional_impact=5.0,
            cultural_timing=5.0, community_participation=5.0,
            meme_strength=5.0, community_creativity=5.0, long_term_strength=5.0,
        ),
        bull_case=("some structural room remains",),
        bear_case=("narrative is a re-run with no differentiation",),
        confidence=80.0, confidence_reason="clear negative narrative evidence",
        mode=ResearchMode.STANDARD, model="test-model",
    )


def gate_passing_scanner(storage, ai, sink, settings=None):
    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}

    async def fake_sleep(seconds):
        pass

    notifier = NotificationEngine([sink], AlertEngineSettings(), time_func=lambda: 0.0)
    scanner = ContinuousScanner(
        settings or SETTINGS, storage, notifier,
        gecko_client=FakeGecko([pair]),
        goplus_client=FakeGoPlus(profiles),
        market_service=FakeMarketService(verdict=(True, "liquidity confirmed")),
        community_client=FakeCommunity(),
        ai_service=ai,
        now_func=lambda: NOW, sleep_func=fake_sleep,
    )
    return scanner, pair


async def test_gate_passing_token_triggers_one_ai_verification():
    """verify_opportunities (default on): the AI judges ONLY the gate passer,
    and a discarded/absent judgment never blocks the deterministic alert."""
    ai = VerifierAI(judgment=None)
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, pair = gate_passing_scanner(storage, ai, sink)
        await scanner.run(max_cycles=1)

        assert ai.judge_calls == 1  # exactly the gate passer, nothing else
        high = [e for e in sink.sent if e.alert_type == "high_priority_opportunity"]
        assert high  # judgment unavailable -> deterministic evidence stands (Rule 9)


async def test_ai_spend_vetoed_for_blacklisted_deployer():
    """Credit conservation: a gate-passing token whose deployer sits on the
    mind layer's blacklist never triggers a paid verification call — the
    free rug engine is consulted BEFORE the API (a paid opinion is the last
    check, never the first)."""
    import dataclasses as _dc
    from meme_intelligence.learning.service import LearningService
    from meme_intelligence.learning.store import LearningStore

    settings = Settings.from_env(env={
        "MEMEINTEL_LEARNING_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_LEARNING_STATE_DIR": ":memory:",
    })
    learning = LearningService(
        settings, store=LearningStore(":memory:", now_func=lambda: NOW),
        now_func=lambda: NOW)
    learning.store.blacklist_deployer("devBad", "solana")

    ai = VerifierAI(judgment=weak_narrative_judgment())
    sink = RecordingSink()
    pair = make_pair()
    profile = _dc.replace(clean_profile(pair.base_token), creator_address="devBad")

    async def fake_sleep(seconds):
        pass

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        notifier = NotificationEngine([sink], AlertEngineSettings(), time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            settings, storage, notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address: profile}),
            market_service=FakeMarketService(verdict=(True, "liquidity confirmed")),
            community_client=FakeCommunity(),
            ai_service=ai,
            learning_service=learning,
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        await scanner.run(max_cycles=1)

    assert ai.judge_calls == 0  # credits saved: rug engine vetoed the call


async def test_rug_screen_vetoes_high_alert_even_without_ai():
    """Live finding (2026-07-10): the rug-engine screen was reachable only
    through the AI-verification gate, so turning the API key off silently
    removed it — a blacklisted deployer's token fired HIGH, unscreened.
    The free screen now runs whether or not AI is configured."""
    import dataclasses as _dc
    from meme_intelligence.learning.service import LearningService
    from meme_intelligence.learning.store import LearningStore

    settings = Settings.from_env(env={
        "MEMEINTEL_LEARNING_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_LEARNING_STATE_DIR": ":memory:",
    })
    learning = LearningService(
        settings, store=LearningStore(":memory:", now_func=lambda: NOW),
        now_func=lambda: NOW)
    learning.store.blacklist_deployer("devBad", "solana")

    sink = RecordingSink()
    pair = make_pair()
    profile = _dc.replace(clean_profile(pair.base_token), creator_address="devBad")

    async def fake_sleep(seconds):
        pass

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        notifier = NotificationEngine([sink], AlertEngineSettings(), time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            settings, storage, notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address: profile}),
            market_service=FakeMarketService(verdict=(True, "liquidity confirmed")),
            community_client=FakeCommunity(),
            ai_service=None,  # the operator turned the API key off
            learning_service=learning,
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        await scanner.run(max_cycles=1)

    # The rug-engine veto now SUPPRESSES the buy-side alert entirely — it used
    # to downgrade to a MEDIUM early_opportunity that a MEDIUM-threshold phone
    # still buzzed for (a demoted rug reaching the operator). No opportunity /
    # momentum alert of any tier survives the veto.
    assert not any(e.alert_type in _BUY_SIDE_ALERT_TYPES for e in sink.sent)


class SearchingMarketService(FakeMarketService):
    """FakeMarketService + provider search, for the copycat screen."""

    def __init__(self, search_results, **kwargs):
        super().__init__(**kwargs)
        self.search_results = search_results
        self.search_calls = 0

    async def search_pairs(self, query):
        self.search_calls += 1
        return self.search_results


async def test_copycat_of_established_token_never_fires_high():
    """Live finding (2026-07-10): fresh knock-offs wearing the symbol of an
    established coin reached the operator as HIGH opportunities. The free
    copycat screen downgrades them with the duplicate named — no AI needed."""
    pair = make_pair()  # symbol MEMA, liquidity 90k
    original_token = TokenIdentity(chain="solana", address="TheRealMema", symbol="MEMA")
    original = _dc.replace(make_pair(address="TheRealMema"), liquidity_usd=2_000_000.0,
                           base_token=original_token,
                           pair_address="PoolTheRealMema")
    market = SearchingMarketService([original], verdict=(True, "liquidity confirmed"))
    sink = RecordingSink()

    async def fake_sleep(seconds):
        pass

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        notifier = NotificationEngine([sink], AlertEngineSettings(), time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            SETTINGS, storage, notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address: clean_profile(pair.base_token)}),
            market_service=market,
            community_client=FakeCommunity(),
            ai_service=None,
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        await scanner.run(max_cycles=1)

    assert market.search_calls == 1
    # The copycat veto now suppresses the buy-side alert entirely (was: a
    # MEDIUM early_opportunity naming the duplicate).
    assert not any(e.alert_type in _BUY_SIDE_ALERT_TYPES for e in sink.sent)


def test_copycat_rule_requires_a_real_size_gap():
    """Unit tests for the pure copycat rule: only an established (deep,
    much larger) pool wearing the same symbol/name is evidence."""
    from meme_intelligence.workflow.controller import _find_established_duplicate

    candidate = make_pair()  # MEMA, 45k liquidity
    kwargs = dict(liquidity_ratio=10.0, min_liquidity_usd=100_000.0)

    def rival(address="OtherAddr", symbol="MEMA", name=None, liquidity=2_000_000.0):
        token = TokenIdentity(chain="solana", address=address, symbol=symbol, name=name)
        return _dc.replace(make_pair(address=address), base_token=token,
                           pair_address=f"Pool{address}", liquidity_usd=liquidity)

    # Same symbol + 20x the liquidity -> veto, with the original named.
    veto = _find_established_duplicate(candidate, [rival()], **kwargs)
    assert veto is not None and "MEMA" in veto

    # A small same-symbol coin is a coincidence, not an original (450k floor
    # here = 10x the candidate's 45k) — and same symbol below the absolute
    # floor never fires either.
    assert _find_established_duplicate(candidate, [rival(liquidity=300_000.0)],
                                       **kwargs) is None

    # The candidate token itself listed on another venue is not a duplicate.
    same = rival(address=candidate.base_token.address)
    assert _find_established_duplicate(candidate, [same], **kwargs) is None

    # Different symbol and name: no match, regardless of size.
    assert _find_established_duplicate(candidate, [rival(symbol="OTHER")],
                                       **kwargs) is None

    # Name-level match fires even when tickers differ (renamed knock-off).
    named_candidate = _dc.replace(
        candidate, base_token=_dc.replace(candidate.base_token, name="Meme Coin"))
    named_rival = rival(symbol="MEMA2", name="  meme   COIN ")  # normalized match
    assert _find_established_duplicate(named_candidate, [named_rival],
                                       **kwargs) is not None

    # Unknown liquidity on the match is not evidence (Rule 8).
    assert _find_established_duplicate(candidate, [rival(liquidity=None)],
                                       **kwargs) is None


async def test_ai_confirmation_annotates_the_alert():
    judgment = weak_narrative_judgment()
    # strong narrative instead: same judgment but high slots
    import dataclasses as _dc3
    strong = _dc3.replace(
        judgment,
        narrative_inputs=NarrativeInputs(
            memorability=90.0, shareability=90.0, emotional_impact=85.0,
            cultural_timing=85.0, community_participation=90.0,
            meme_strength=90.0, community_creativity=85.0, long_term_strength=80.0,
        ),
    )
    ai = VerifierAI(judgment=strong)
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, pair = gate_passing_scanner(storage, ai, sink)
        await scanner.run(max_cycles=1)

        assert ai.judge_calls == 1
        high = [e for e in sink.sent if e.alert_type == "high_priority_opportunity"]
        assert high
        assert any("AI verification" in r for r in high[0].reasons)
        assert any("AI caution" in r for r in high[0].reasons)  # bear case rides along


async def test_ai_dissent_withholds_the_high_priority_alert():
    """If the judgment drops the re-scored master below the gate, the
    high-priority alert simply never fires — same gates, better evidence."""
    ai = VerifierAI(judgment=weak_narrative_judgment())
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, pair = gate_passing_scanner(storage, ai, sink)
        await scanner.run(max_cycles=1)

        assert ai.judge_calls == 1
        assert not any(e.alert_type == "high_priority_opportunity" for e in sink.sent)
        # the snapshot records the AI-enriched (honest, lower) score
        history = storage.score_history(pair.base_token)
        assert history and history[0]["final_score"] < 85.0


async def test_verification_off_means_no_ai_call():
    settings = Settings.from_env(env={"MEMEINTEL_AI_VERIFY_OPPORTUNITIES": "false"})
    ai = VerifierAI(judgment=None)
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = gate_passing_scanner(storage, ai, sink, settings=settings)
        await scanner.run(max_cycles=1)
        assert ai.judge_calls == 0
        assert any(e.alert_type == "high_priority_opportunity" for e in sink.sent)


async def test_verify_opportunities_false_respected_even_with_enable_in_monitor_true():
    """Bug-hunt: an `or` in the constructor meant verify_opportunities=false
    was ignored whenever enable_in_monitor was true, so a per-token
    judgment discarded for low confidence got silently re-attempted a
    second time on a gate-passing token — a real extra paid call the
    user explicitly disabled."""
    settings = Settings.from_env(env={
        "MEMEINTEL_AI_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_AI_VERIFY_OPPORTUNITIES": "false",
    })
    ai = VerifierAI(judgment=None)  # simulates a discarded/low-confidence judgment
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = gate_passing_scanner(storage, ai, sink, settings=settings)
        assert scanner._ai_verifier is None
        await scanner.run(max_cycles=1)
        # exactly one call: the per-token enable_in_monitor pass, NOT a
        # second verification call on top of it
        assert ai.judge_calls == 1


async def test_persistent_gate_passer_verified_only_once():
    """Bug-hunt: a token still passing every gate on its Nth watchlist
    recheck got re-judged with a fresh paid Claude call every single
    time, even while its alert sat cooldown-suppressed and unseen."""
    judgment = weak_narrative_judgment()
    import dataclasses as _dc4
    strong = _dc4.replace(
        judgment,
        narrative_inputs=NarrativeInputs(
            memorability=90.0, shareability=90.0, emotional_impact=85.0,
            cultural_timing=85.0, community_participation=90.0,
            meme_strength=90.0, community_creativity=85.0, long_term_strength=80.0,
        ),
    )
    ai = VerifierAI(judgment=strong)
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, pair = gate_passing_scanner(
            storage, ai, sink, settings=fast_recheck_settings())
        # cycle 1: discovers + verifies + tiers the token
        await scanner.run(max_cycles=1)
        assert ai.judge_calls == 1
        # cycle 2: the SAME token comes up again via watchlist recheck
        # (fast_recheck_settings reruns every cycle) and still passes
        # every gate — must NOT trigger a second AI call
        await scanner.run(max_cycles=1)
        assert ai.judge_calls == 1


async def test_dead_watchlist_token_archived_with_postmortem():
    """Part 29 S1: a tracked token whose pool collapsed gets ONE post-mortem
    and is archived — not re-tiered on its pump-window score and re-warned
    at HIGH every recheck. This token never earned an opportunity alert, so
    the interest gate delivers the post-mortem at LOW (recorded for grading,
    silent on the phone)."""
    dying = TokenIdentity(chain="solana", address="TokenDying", symbol="DIE")
    dead_pair = _dc.replace(make_pair(address="TokenDying", symbol="DIE"),
                            liquidity_usd=25.0)
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(dying, WatchlistTier.TIER_1_HIGH_PRIORITY, score=85.0)
        market = FakeMarketService({"TokenDying": dead_pair})
        scanner = make_scanner_with_market(
            storage, [], {"TokenDying": clean_profile(dying)},
            market, settings=fast_recheck_settings(), sink=sink,
        )
        await scanner.run(max_cycles=1)

        assert storage.get_watchlist() == []  # archived despite a high score
        archived = storage.get_watchlist(include_archived=True)
        assert archived and archived[0].tier is WatchlistTier.ARCHIVED

        types = [e.alert_type for e in sink.sent]
        assert "token_death" in types
        assert "risk_warning" not in types
        assert "score_drop_review" not in types
        death = next(e for e in sink.sent if e.alert_type == "token_death")
        assert death.priority is AlertPriority.LOW  # interest gate: never recommended


async def test_recommended_token_keeps_full_priority_postmortem():
    """The interest gate must NOT silence tokens the operator was pointed
    at: with a prior HIGH opportunity alert on record, the death post-mortem
    arrives at its full MEDIUM priority."""
    from meme_intelligence.alerts.notification_engine import AlertEvent
    from meme_intelligence.core.enums import AlertPriority as _AP

    dying = TokenIdentity(chain="solana", address="TokenDying", symbol="DIE")
    dead_pair = _dc.replace(make_pair(address="TokenDying", symbol="DIE"),
                            liquidity_usd=25.0)
    sink = RecordingSink()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(dying, WatchlistTier.TIER_1_HIGH_PRIORITY, score=85.0)
        storage.record_alert(  # the scanner recommended this token earlier
            AlertEvent(_AP.HIGH, "strong_candidate", dying, "was strong", ()),
            source="test")
        market = FakeMarketService({"TokenDying": dead_pair})
        scanner = make_scanner_with_market(
            storage, [], {"TokenDying": clean_profile(dying)},
            market, settings=fast_recheck_settings(), sink=sink,
        )
        await scanner.run(max_cycles=1)

        death = next(e for e in sink.sent if e.alert_type == "token_death")
        assert death.priority is AlertPriority.MEDIUM


# ---- Parts 17/23 in the monitor: metered layers behind enable_in_monitor ----


class RecordingWalletService:
    """Wallet-service double: records gather() calls, reports no data."""

    def __init__(self):
        self.gather_calls: list[str] = []
        self.closed = False

    async def gather(self, token):
        from meme_intelligence.core.errors import InsufficientDataError
        self.gather_calls.append(token.address)
        raise InsufficientDataError("no wallet data in this test double")

    async def close(self):
        self.closed = True


class RecordingAIService:
    """AI-service double: records judge() calls, returns no judgment."""

    def __init__(self):
        self.judge_calls = 0

    async def judge(self, result, *, mode):
        self.judge_calls += 1
        return None


def metered_on_settings() -> Settings:
    return Settings.from_env(env={
        "MEMEINTEL_WALLET_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_AI_ENABLE_IN_MONITOR": "true",
    })


def make_scanner_with_metered(storage, pools, profiles, *, wallet=None, ai=None,
                              settings=None):
    async def fake_sleep(seconds):
        pass

    notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                  time_func=lambda: 0.0)
    return ContinuousScanner(
        settings or SETTINGS, storage, notifier,
        gecko_client=FakeGecko(pools),
        goplus_client=FakeGoPlus(profiles),
        wallet_service=wallet,
        ai_service=ai,
        now_func=lambda: NOW,
        sleep_func=fake_sleep,
    )


async def test_metered_services_off_by_default_even_when_wired():
    """Rule 10/11: wallet/AI never run in the loop without explicit opt-in —
    the settings flags are authoritative over what the caller wired in.
    verify_opportunities is disabled here so this isolates the
    enable_in_monitor gating from the separate gate-passer AI pass."""
    pair = make_pair()
    wallet, ai = RecordingWalletService(), RecordingAIService()
    settings = Settings.from_env(env={"MEMEINTEL_AI_VERIFY_OPPORTUNITIES": "false"})
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_scanner_with_metered(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            wallet=wallet, ai=ai, settings=settings,  # wired, but flags say off
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1
        assert wallet.gather_calls == []
        assert ai.judge_calls == 0


async def test_metered_services_run_when_opted_in():
    pair = make_pair()
    wallet, ai = RecordingWalletService(), RecordingAIService()
    # enable_in_monitor on, verify_opportunities off: isolates the
    # per-token pipeline judgment (one call) from the gate-passer pass.
    settings = Settings.from_env(env={
        "MEMEINTEL_WALLET_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_AI_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_AI_VERIFY_OPPORTUNITIES": "false",
    })
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_scanner_with_metered(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            wallet=wallet, ai=ai, settings=settings,
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1  # a no-data wallet answer never blocks analysis
        assert wallet.gather_calls == [pair.base_token.address]
        assert ai.judge_calls == 1


async def test_credit_gate_skips_wallet_lookup_on_oversized_candidate():
    """End-to-end through the real scanner (not just the pipeline unit
    tests): a coin already past the buy-side ceiling can never earn an
    opportunity alert, so the wallet credit gate skips it even with
    enable_in_monitor on."""
    oversized = _dc.replace(make_pair(), liquidity_usd=2_000_000.0, market_cap=5_000_000.0)
    wallet = RecordingWalletService()
    settings = Settings.from_env(env={
        "MEMEINTEL_WALLET_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_AI_VERIFY_OPPORTUNITIES": "false",
    })
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_scanner_with_metered(
            storage, [oversized], {oversized.base_token.address: clean_profile(oversized.base_token)},
            wallet=wallet, settings=settings,
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1        # analysis still ran
        assert wallet.gather_calls == []        # just no wallet credit spent


async def test_credit_gate_still_checks_wallets_for_a_held_oversized_coin():
    """A coin the operator holds always gets a wallet check regardless of
    the ceiling — whale-exit visibility matters most for money he already
    put in, whatever the coin's current size (2026-07-15 credit gate)."""
    oversized = _dc.replace(make_pair(), liquidity_usd=2_000_000.0, market_cap=5_000_000.0)
    wallet = RecordingWalletService()
    settings = Settings.from_env(env={
        "MEMEINTEL_WALLET_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_AI_VERIFY_OPPORTUNITIES": "false",
    })
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.set_holding(oversized.base_token)
        scanner = make_scanner_with_metered(
            storage, [oversized], {oversized.base_token.address: clean_profile(oversized.base_token)},
            wallet=wallet, settings=settings,
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1
        assert wallet.gather_calls == [oversized.base_token.address]


async def test_security_change_triggers_critical_alert_on_recheck():
    """Part 18 Section 10 end-to-end: a token turning honeypot between
    analyses produces a CRITICAL security_change alert."""
    tracked = TokenIdentity(chain="solana", address="TokenTurn", symbol="TURN")
    tracked_pair = make_pair(address="TokenTurn", symbol="TURN")
    sink = RecordingSink()

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tracked, WatchlistTier.TIER_1_HIGH_PRIORITY, score=80.0)

        # First recheck: clean profile establishes the facts baseline.
        goplus_profiles = {"TokenTurn": clean_profile(tracked)}
        market = FakeMarketService({"TokenTurn": tracked_pair})
        scanner = make_scanner_with_market(storage, [], goplus_profiles, market,
                                           settings=fast_recheck_settings(), sink=sink)
        await scanner.run(max_cycles=1)
        assert not any(e.alert_type == "security_change" for e in sink.sent)

        # Second recheck: the token became a honeypot.
        import dataclasses as _dc2
        goplus_profiles["TokenTurn"] = _dc2.replace(clean_profile(tracked), is_honeypot=True)
        scanner2 = make_scanner_with_market(storage, [], goplus_profiles, market,
                                            settings=fast_recheck_settings(), sink=sink)
        await scanner2.run(max_cycles=1)

        changes = [e for e in sink.sent if e.alert_type == "security_change"]
        assert changes and changes[0].priority is AlertPriority.CRITICAL
        assert any("honeypot" in r for r in changes[0].reasons)


# ---- Project 2: holdings-first interest, mute filtering, rug-seam wiring ----

async def test_held_token_is_operator_interest_without_alert_history():
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [], {})
        assert scanner._operator_interest(pair.base_token) is False
        storage.set_holding(pair.base_token)
        assert scanner._operator_interest(pair.base_token) is True


async def test_muted_token_delivery_suppressed_but_analysis_recorded():
    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.mute_token(pair.base_token)
        sink = RecordingSink()
        scanner, _ = make_scanner(storage, [pair], profiles, sink=sink)

        from meme_intelligence.alerts.notification_engine import AlertEvent
        from meme_intelligence.core.enums import AlertPriority

        class AlwaysAlert:
            def evaluate(self, result, **kwargs):
                return [AlertEvent(priority=AlertPriority.HIGH,
                                   alert_type="high_priority_opportunity",
                                   token=result.pair.base_token,
                                   title="forced", reasons=("forced",))]

        scanner._rules = AlwaysAlert()
        await scanner.run(max_cycles=1)

        assert sink.sent == []                                     # delivery muted
        assert storage.latest_security_facts(pair.base_token)      # analysis ran
        assert storage.score_history(pair.base_token, limit=1)     # snapshot recorded


async def test_unmuted_token_still_delivers():
    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        sink = RecordingSink()
        scanner, _ = make_scanner(storage, [pair], profiles, sink=sink)

        from meme_intelligence.alerts.notification_engine import AlertEvent
        from meme_intelligence.core.enums import AlertPriority

        class AlwaysAlert:
            def evaluate(self, result, **kwargs):
                return [AlertEvent(priority=AlertPriority.HIGH,
                                   alert_type="high_priority_opportunity",
                                   token=result.pair.base_token,
                                   title="forced", reasons=("forced",))]

        scanner._rules = AlwaysAlert()
        await scanner.run(max_cycles=1)
        assert len(sink.sent) >= 1


async def test_rug_seam_receives_unsellable_override(monkeypatch):
    """Project 1's live probe feeds the rug engine: True only for a confirmed
    buy-route-without-sell-route; NEVER False (a good probe must not erase
    GoPlus honeypot flags); None when the probe did not run."""
    import dataclasses as _dc
    from types import SimpleNamespace

    from meme_intelligence.learning import rug_engine as rug_module

    captured = []
    original_assess = rug_module.RugEngine.assess

    def spy_assess(self, **kwargs):
        captured.append(kwargs.get("unsellable_override"))
        return original_assess(self, **kwargs)

    monkeypatch.setattr(rug_module.RugEngine, "assess", spy_assess)

    pair = make_pair()
    base = clean_profile(pair.base_token)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [], {})

        def fake_result(profile):
            return SimpleNamespace(security_profile=profile, pair=pair, wallet=None)

        # Confirmed cannot-sell -> override True
        cannot_sell = _dc.replace(base, live_buy_route_found=True,
                                  live_sell_route_found=False)
        scanner._deterministic_risk_veto(fake_result(cannot_sell), [], None)
        # Healthy round trip -> None (NOT False)
        healthy = _dc.replace(base, live_buy_route_found=True,
                              live_sell_route_found=True,
                              live_round_trip_loss_percent=1.0)
        scanner._deterministic_risk_veto(fake_result(healthy), [], None)
        # Probe never ran -> None
        scanner._deterministic_risk_veto(fake_result(base), [], None)

    assert captured == [True, None, None]


async def test_status_snapshot_reports_layers_and_counts():
    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair], profiles)
        await scanner.run(max_cycles=1)
        snap = scanner.status_snapshot()
    assert snap["cycles"] == 1
    assert snap["last_cycle"]["analyzed"] == 1
    assert snap["layers"]["buy_button"] is False
    assert snap["db"]["tokens"] >= 1
    assert snap["uptime_seconds"] is not None


async def test_check_token_uses_market_service_and_pipeline():
    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}

    class FakeMarket:
        async def get_best_pair(self, address, chain=None):
            return pair if address == pair.base_token.address else None

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                      time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            SETTINGS, storage, notifier,
            gecko_client=FakeGecko([]), goplus_client=FakeGoPlus(profiles),
            market_service=FakeMarket(),
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        result = await scanner.check_token(pair.base_token.address)
        assert result is not None and result.master is not None
        assert await scanner.check_token("Unknown11111111111111111111111111111111111") is None


async def test_scanner_starts_and_stops_attached_telegram_listener():
    class FakeListener:
        def __init__(self):
            self.started = 0
            self.stopped = 0

        async def start(self):
            self.started += 1

        async def stop(self):
            self.stopped += 1

    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair], profiles)
        listener = FakeListener()
        scanner.set_telegram_listener(listener)
        await scanner.run(max_cycles=1)
    assert listener.started == 1 and listener.stopped == 1


async def test_broken_telegram_listener_never_blocks_the_scan():
    class ExplodingListener:
        async def start(self):
            raise RuntimeError("cannot start")

        async def stop(self):
            raise RuntimeError("cannot stop")

    pair = make_pair()
    profiles = {pair.base_token.address: clean_profile(pair.base_token)}
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair], profiles)
        scanner.set_telegram_listener(ExplodingListener())
        history = await scanner.run(max_cycles=1)   # must not raise
    assert len(history) == 1 and history[0].analyzed == 1


# ---- Project 3: mind-layer P(rug) veto (earned authority only) ----

def make_learning_settings_env(**extra):
    env = {"MEMEINTEL_LEARNING_VETO_ENABLED": "true"}
    env.update(extra)
    return Settings.from_env(env=env)


class FakeMind:
    """Learning-service stand-in: configurable metrics + verdict."""

    def __init__(self, p_rug=0.9, precision=0.8, tp=8, fp=2, raise_on_eval=False):
        self.p_rug = p_rug
        self.metrics_calls = 0
        self.eval_calls = 0
        self._raise = raise_on_eval
        self._metrics = {"rug": {"precision": precision,
                                 "true_positives": tp, "false_positives": fp}}

    def get_learning_metrics(self, *, persist=True):
        self.metrics_calls += 1
        return self._metrics

    def evaluate_coin(self, address, chain, snapshots, *, security=None, creator=None):
        self.eval_calls += 1
        if self._raise:
            raise RuntimeError("model exploded")
        return {"final_probabilities": {"rug": self.p_rug}, "model_confidence": 0.9,
                "sample_size": 40}

    # controller._feed_learning compatibility (not used in these tests)
    def record_detection(self, *a, **k): ...
    def capture_snapshot(self, *a, **k): ...


def make_veto_scanner(storage, mind, settings):
    notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                  time_func=lambda: 0.0)

    async def fake_sleep(seconds): ...

    return ContinuousScanner(
        settings, storage, notifier,
        gecko_client=FakeGecko([]), goplus_client=FakeGoPlus({}),
        learning_service=mind,
        now_func=lambda: NOW, sleep_func=fake_sleep,
    )


def fake_veto_input(pair):
    from types import SimpleNamespace
    return SimpleNamespace(security_profile=clean_profile(pair.base_token),
                           pair=pair, wallet=None)


def test_mind_veto_fires_with_earned_authority_and_names_evidence():
    pair = make_pair()
    settings = make_learning_settings_env(MEMEINTEL_LEARNING_ENABLE_IN_MONITOR="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        mind = FakeMind(p_rug=0.9, precision=0.8, tp=8, fp=2)
        scanner = make_veto_scanner(storage, mind, settings)
        reason = scanner._deterministic_risk_veto(fake_veto_input(pair), [], None)
    assert reason is not None
    assert "p(rug) 90%" in reason and "precision 0.80" in reason and "10 graded" in reason


def test_mind_veto_abstains_below_p_rug_threshold():
    pair = make_pair()
    settings = make_learning_settings_env(MEMEINTEL_LEARNING_ENABLE_IN_MONITOR="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        mind = FakeMind(p_rug=0.5, precision=0.8)
        scanner = make_veto_scanner(storage, mind, settings)
        assert scanner._deterministic_risk_veto(fake_veto_input(pair), [], None) is None


def test_mind_veto_abstains_without_earned_authority():
    pair = make_pair()
    settings = make_learning_settings_env(MEMEINTEL_LEARNING_ENABLE_IN_MONITOR="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        # High p_rug but unproven: precision below floor.
        weak = FakeMind(p_rug=0.99, precision=0.4)
        scanner = make_veto_scanner(storage, weak, settings)
        assert scanner._deterministic_risk_veto(fake_veto_input(pair), [], None) is None
        assert weak.eval_calls == 0            # never even evaluated

        # High p_rug but too few graded rug calls.
        cold = FakeMind(p_rug=0.99, precision=1.0, tp=3, fp=0)
        scanner = make_veto_scanner(storage, cold, settings)
        assert scanner._deterministic_risk_veto(fake_veto_input(pair), [], None) is None
        assert cold.eval_calls == 0


def test_mind_veto_off_by_default_even_with_perfect_metrics():
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        mind = FakeMind(p_rug=0.99, precision=1.0, tp=50, fp=0)
        scanner = make_veto_scanner(storage, mind, SETTINGS)  # veto_enabled default False
        assert scanner._deterministic_risk_veto(fake_veto_input(pair), [], None) is None
        assert mind.metrics_calls == 0 and mind.eval_calls == 0


def test_mind_veto_evaluation_error_fails_open():
    pair = make_pair()
    settings = make_learning_settings_env(MEMEINTEL_LEARNING_ENABLE_IN_MONITOR="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        mind = FakeMind(p_rug=0.99, precision=0.9, raise_on_eval=True)
        scanner = make_veto_scanner(storage, mind, settings)
        assert scanner._deterministic_risk_veto(fake_veto_input(pair), [], None) is None


def test_mind_veto_authority_check_is_cached():
    pair = make_pair()
    settings = make_learning_settings_env(MEMEINTEL_LEARNING_ENABLE_IN_MONITOR="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        mind = FakeMind(p_rug=0.2, precision=0.8)   # gate passes; p_rug never vetoes
        scanner = make_veto_scanner(storage, mind, settings)
        scanner._deterministic_risk_veto(fake_veto_input(pair), [], None)
        scanner._deterministic_risk_veto(fake_veto_input(pair), [], None)
        scanner._deterministic_risk_veto(fake_veto_input(pair), [], None)
    assert mind.metrics_calls == 1     # cached within the TTL
    assert mind.eval_calls == 3        # live P(rug) is per-candidate


def test_veto_gate_function_edges():
    from meme_intelligence.learning.metrics import veto_gate

    good = {"rug": {"precision": 0.75, "true_positives": 9, "false_positives": 3}}
    assert veto_gate(good, min_accuracy=0.7, min_samples=10) == (0.75, 12)
    assert veto_gate(good, min_accuracy=0.8, min_samples=10) is None      # below floor
    assert veto_gate(good, min_accuracy=0.7, min_samples=13) is None      # too few
    assert veto_gate({"rug": {}}, min_accuracy=0.7, min_samples=1) is None  # no data
    assert veto_gate({}, min_accuracy=0.7, min_samples=1) is None


# ---- Insufficient-data retry: young tokens get a second look (2026-07-11) ----

from types import SimpleNamespace  # noqa: E402

from meme_intelligence.core.enums import Classification  # noqa: E402


def fake_master(classification, overrides=(), coverage=0.2):
    return SimpleNamespace(
        master=SimpleNamespace(classification=classification, overrides=overrides,
                               coverage=coverage))


def make_bare_scanner(storage, settings=None):
    """A scanner instance for exercising the pure _finalize_or_reschedule
    decision logic directly, without running a full cycle."""
    notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                  time_func=lambda: 0.0)
    return ContinuousScanner(
        settings or SETTINGS, storage, notifier,
        gecko_client=FakeGecko([]), goplus_client=FakeGoPlus({}),
        now_func=lambda: NOW,
    )


def test_young_insufficient_data_avoid_is_rescheduled_not_permanent():
    """The core fix: an AVOID from merely-unverified categories (no red-flag
    overrides) on a fresh pool is NOT permanently blacklisted -- it gets a
    scheduled retry instead."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        pair = make_pair(address="Fresh1")
        pair = _dc.replace(pair, pair_created_at=NOW - timedelta(minutes=1))  # 1 min old
        key = ("solana", "fresh1")
        result = fake_master(Classification.AVOID, overrides=(), coverage=0.2)
        scanner._finalize_or_reschedule(key, result, pair, NOW)
        assert key not in scanner._seen              # NOT permanently excluded
        assert key in scanner._retry_pending          # scheduled for another look
        due_at, saved_address, giveup_at = scanner._retry_pending.get(key)
        assert due_at == NOW + timedelta(minutes=SETTINGS.workflow.insufficient_data_retry_minutes)
        assert saved_address == "Fresh1"   # ORIGINAL case preserved, not the lowercased key
        # give-up deadline is the pool's own max-age horizon (1-min-old pool here)
        assert giveup_at == (NOW - timedelta(minutes=1)) + timedelta(
            minutes=SETTINGS.workflow.insufficient_data_max_age_minutes)


def test_confirmed_red_flag_avoid_is_never_retried():
    """A CONFIRMED red-flag AVOID (overrides non-empty -- destructive
    security / fake community / extreme risk) is real evidence and stays
    permanently excluded, even on a 1-minute-old pool."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        pair = _dc.replace(make_pair(address="Bad1"),
                           pair_created_at=NOW - timedelta(minutes=1))
        key = ("solana", "bad1")
        result = fake_master(Classification.AVOID, overrides=("destructive security",),
                             coverage=0.2)
        scanner._finalize_or_reschedule(key, result, pair, NOW)
        assert key in scanner._seen
        assert key not in scanner._retry_pending


def test_insufficient_data_avoid_past_max_age_gives_up():
    """Once the pool itself has aged past the retry window, further waiting
    is not worth it -- the token is finally excluded like before the fix."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        max_age = SETTINGS.workflow.insufficient_data_max_age_minutes
        pair = _dc.replace(make_pair(address="Old1"),
                           pair_created_at=NOW - timedelta(minutes=max_age + 5))
        key = ("solana", "old1")
        result = fake_master(Classification.AVOID, overrides=(), coverage=0.2)
        scanner._finalize_or_reschedule(key, result, pair, NOW)
        assert key in scanner._seen
        assert key not in scanner._retry_pending


def test_unknown_pool_age_is_not_retried():
    """Rule 8: without a known pool age, the system cannot reason about
    'too young to judge' -- it does not guess, and finalizes as before."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        pair = _dc.replace(make_pair(address="Unk1"), pair_created_at=None)
        key = ("solana", "unk1")
        result = fake_master(Classification.AVOID, overrides=(), coverage=0.2)
        scanner._finalize_or_reschedule(key, result, pair, NOW)
        assert key in scanner._seen
        assert key not in scanner._retry_pending


def test_high_coverage_avoid_is_not_retried():
    """A confidently-scored AVOID (real data, just a low score) is not a
    data gap -- it is a verdict, and stays excluded."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        pair = _dc.replace(make_pair(address="Weak1"),
                           pair_created_at=NOW - timedelta(minutes=1))
        key = ("solana", "weak1")
        result = fake_master(Classification.AVOID, overrides=(), coverage=0.95)
        scanner._finalize_or_reschedule(key, result, pair, NOW)
        assert key in scanner._seen


def test_retry_disabled_by_flag_keeps_old_behavior():
    settings = Settings.from_env(env={"MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_ENABLED": "false"})
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage, settings=settings)
        pair = _dc.replace(make_pair(address="Off1"),
                           pair_created_at=NOW - timedelta(minutes=1))
        key = ("solana", "off1")
        result = fake_master(Classification.AVOID, overrides=(), coverage=0.2)
        scanner._finalize_or_reschedule(key, result, pair, NOW)
        assert key in scanner._seen
        assert key not in scanner._retry_pending


async def test_main_loop_skips_a_key_pending_retry():
    """A token already scheduled for a later retry must not be re-run by the
    plain discovery loop every cycle it's still 'new' -- only the dedicated
    retry pass handles it, on its own paced schedule."""
    pair = make_pair(address="Pend1")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair],
                                  {pair.base_token.address: clean_profile(pair.base_token)})
        key = ("solana", "pend1")
        scanner._retry_pending.add(  # not due yet (3-tuple: due, address, give-up)
            key, (NOW + timedelta(minutes=5), "Pend1", NOW + timedelta(hours=1)))
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 0     # skipped, not (re-)analyzed
        assert key in scanner._retry_pending  # still pending, untouched


async def test_retry_pass_reanalyzes_and_finalizes_a_due_token():
    """When a pending retry's due time arrives, _retry_insufficient_data
    re-fetches the pair and re-runs the pipeline through the normal alert
    path -- exactly like a fresh analysis."""
    pair = make_pair(address="Due1", symbol="DUE")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService({"Due1": pair})
        scanner = make_scanner_with_market(
            storage, [], {"Due1": clean_profile(pair.base_token)}, market)
        key = ("solana", "due1")
        # due now, original-case address, give-up deadline an hour out
        scanner._retry_pending.add(key, (NOW, "Due1", NOW + timedelta(hours=1)))
        history = await scanner.run(max_cycles=1)
        assert market.recheck_calls == 1
        assert history[0].analyzed == 1
        assert key in scanner._seen   # this healthy re-analysis finalized it
        assert storage.score_history(pair.base_token)  # went through _process_result


async def test_retry_pass_gives_up_when_pool_disappears():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService({})  # no pairs for this address
        scanner = make_scanner_with_market(storage, [], {}, market)
        key = ("solana", "gone1")
        scanner._retry_pending.add(key, (NOW, "Gone1", NOW + timedelta(hours=1)))
        history = await scanner.run(max_cycles=1)
        assert key in scanner._seen
        assert history[0].analyzed == 0


async def test_retry_pass_skips_a_stale_already_finalized_entry():
    """A leftover _retry_pending entry for a key already promoted to _seen
    (the bounded set has no remove -- stale is tolerated, matching _seen's
    own FIFO-eviction philosophy) must not be re-fetched pointlessly."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService({"Stale1": make_pair(address="Stale1")})
        scanner = make_scanner_with_market(storage, [], {}, market)
        key = ("solana", "stale1")
        scanner._seen.add(key)                              # already finalized
        scanner._retry_pending.add(  # stale leftover, due
            key, (NOW, "Stale1", NOW + timedelta(hours=1)))
        history = await scanner.run(max_cycles=1)
        assert market.recheck_calls == 0     # never re-fetched
        assert history[0].analyzed == 0


def test_repace_retry_pushes_due_time_forward_before_giveup():
    """Bug-hunt 2026-07-12: a NON-terminal retry outcome (provider outage,
    security data not yet indexed) must re-pace the entry to its NEXT due
    time, not leave it at the old (already-past) due time -- else the entry
    stayed 'due' and re-hit the failing provider EVERY cycle for the whole
    outage."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        key = ("solana", "outage1")
        giveup_at = NOW + timedelta(hours=1)          # deadline still ahead
        scanner._retry_pending.add(key, (NOW, "Outage1", giveup_at))
        scanner._repace_retry(key, "Outage1", giveup_at, NOW)
        assert key not in scanner._seen               # not finalized -- still waiting
        due_at, address, saved_giveup = scanner._retry_pending.get(key)
        # pushed to now + retry_minutes (capped by giveup_at), no longer due now
        assert due_at == NOW + timedelta(
            minutes=SETTINGS.workflow.insufficient_data_retry_minutes)
        assert address == "Outage1"                   # case preserved
        assert saved_giveup == giveup_at


def test_repace_retry_finalizes_once_past_giveup_deadline():
    """When the pool has aged past its give-up deadline mid-outage, re-pacing
    finalizes it into _seen rather than pacing forever."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_bare_scanner(storage)
        key = ("solana", "expired1")
        giveup_at = NOW - timedelta(minutes=1)        # already past the deadline
        scanner._retry_pending.add(key, (NOW, "Expired1", giveup_at))
        scanner._repace_retry(key, "Expired1", giveup_at, NOW)
        assert key in scanner._seen                   # finalized, not re-paced


async def test_retry_pass_repaces_on_provider_outage_not_every_cycle():
    """End-to-end: a due retry that hits a total provider outage is re-paced
    (not left due), so the next cycle does NOT immediately re-fetch it."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        market = FakeMarketService({}, all_providers_down=True)  # outage
        scanner = make_scanner_with_market(storage, [], {}, market)
        key = ("solana", "flaky1")
        scanner._retry_pending.add(key, (NOW, "Flaky1", NOW + timedelta(hours=1)))
        await scanner.run(max_cycles=1)
        assert key not in scanner._seen               # outage != give up
        due_at, _address, _giveup = scanner._retry_pending.get(key)
        assert due_at > NOW                            # re-paced into the future


async def test_rug_screen_now_covers_momentum_and_medium_alerts_too():
    """The bigger 2026-07-11 finding: the free rug-engine screen used to run
    ONLY when a token would fire a HIGH opportunity tier -- so momentum
    (the largest alert category by far) and early_opportunity reached the
    operator completely unscreened by the rug engine, even for a blacklisted
    deployer. A rug classically pumps hard right before it dumps, so exactly
    the coins momentum got excited about were the ones never checked. The
    screen now runs for ANY buy-side alert (it costs zero extra API calls)."""
    from meme_intelligence.learning.service import LearningService
    from meme_intelligence.learning.store import LearningStore

    settings = Settings.from_env(env={
        "MEMEINTEL_LEARNING_ENABLE_IN_MONITOR": "true",
        "MEMEINTEL_LEARNING_STATE_DIR": ":memory:",
    })
    learning = LearningService(
        settings, store=LearningStore(":memory:", now_func=lambda: NOW),
        now_func=lambda: NOW)
    learning.store.blacklist_deployer("devBad", "solana")

    sink = RecordingSink()
    # $6,000 liquidity keeps the master score below the strong-candidate bar
    # (fires momentum + early_opportunity only, confirmed no HIGH tier) while
    # still clearing the momentum gate -- exactly the "exciting pump, unknown
    # safety" shape a rug takes right before it dumps.
    pair = _dc.replace(make_pair(), liquidity_usd=6_000.0)
    profile = _dc.replace(clean_profile(pair.base_token), creator_address="devBad")

    async def fake_sleep(seconds):
        pass

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        notifier = NotificationEngine([sink], AlertEngineSettings(), time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            settings, storage, notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address: profile}),
            ai_service=None,
            learning_service=learning,
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        await scanner.run(max_cycles=1)

    # Confirm the fixture really does fire momentum/early_opportunity with a
    # CLEAN deployer -- proving the suppression below is the veto's doing,
    # not a fixture that never fires anything.
    with Storage(":memory:", now_func=lambda: NOW) as storage2:
        clean_sink = RecordingSink()
        clean_notifier = NotificationEngine([clean_sink], AlertEngineSettings(),
                                            time_func=lambda: 0.0)
        clean_scanner = ContinuousScanner(
            SETTINGS, storage2, clean_notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address: clean_profile(pair.base_token)}),
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        await clean_scanner.run(max_cycles=1)
    clean_types = {e.alert_type for e in clean_sink.sent}
    assert clean_types & {"momentum", "early_opportunity"}   # fixture sanity check
    assert not (clean_types & {"high_priority_opportunity", "strong_candidate"})

    # With the blacklisted deployer, the SAME fixture must now be fully
    # suppressed -- proof the free screen ran even though no HIGH tier fired.
    assert not any(e.alert_type in _BUY_SIDE_ALERT_TYPES for e in sink.sent)


# ---- Smart-wallet data clock wiring (Part 17 groundwork) --------------------
# The recorder is None-gated and never raises, so a broken wire is a SILENT
# no-op — only an end-to-end cycle against real Storage proves the clock runs.

from meme_intelligence.config.settings import SmartWalletSettings  # noqa: E402
from meme_intelligence.core.models import TopHolder  # noqa: E402
from meme_intelligence.workflow.smart_wallets import SmartWalletRecorder  # noqa: E402


async def test_scan_cycle_records_top_holder_sightings():
    """One real cycle: GoPlus profile -> pipeline -> controller -> recorder ->
    Storage. Guards the feature's only production call site (a refactor that
    drops the kwarg or the record() call must fail THIS test, not ship a
    silent no-op that loses weeks of unrecoverable earliest-holder data)."""
    pair = make_pair()
    profile = _dc.replace(
        clean_profile(pair.base_token),
        top_holders=(TopHolder(address="EarlyWhale1", percent=8.0),
                     TopHolder(address="EarlyWhale2", percent=3.5)),
    )
    async def fake_sleep(seconds):
        pass
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        notifier = NotificationEngine([RecordingSink()], AlertEngineSettings(),
                                      time_func=lambda: 0.0)
        scanner = ContinuousScanner(
            SETTINGS, storage, notifier,
            gecko_client=FakeGecko([pair]),
            goplus_client=FakeGoPlus({pair.base_token.address: profile}),
            smart_wallet_recorder=SmartWalletRecorder(
                storage, SmartWalletSettings(enabled=True)),
            now_func=lambda: NOW, sleep_func=fake_sleep,
        )
        await scanner.run(max_cycles=1)

        assert set(storage.wallets_seen_on(pair.base_token)) == {"EarlyWhale1", "EarlyWhale2"}
        history = storage.wallet_history("EarlyWhale1")
        assert history[0]["source"] == "goplus_holders"
        assert history[0]["side"] == "hold_top10"
        assert history[0]["percent"] == 8.0


async def test_scan_cycle_without_recorder_records_nothing():
    """Default wiring (recorder None) must leave the sightings table empty —
    the data clock is strictly opt-in (Rule 18)."""
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, _ = make_scanner(storage, [pair],
                                  {pair.base_token.address: clean_profile(pair.base_token)})
        await scanner.run(max_cycles=1)
        assert storage.wallets_seen_on(pair.base_token) == []


# ---- "Seen before" framing on re-alerts (operator complaint 2026-07-14) ----

async def test_realert_carries_history_note():
    """A token with prior alert history must re-alert WITH the 'seen before'
    line — a recheck alert days later must never read like a brand-new
    discovery. First-ever alerts stay clean (no note)."""
    pair = make_pair()
    profile = clean_profile(pair.base_token)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        # First ever alert: no note.
        sink1 = RecordingSink()
        scanner, _ = make_scanner(storage, [pair],
                                  {pair.base_token.address: profile}, sink=sink1)
        await scanner.run(max_cycles=1)
        assert sink1.sent and all(not e.history_note for e in sink1.sent)

        # A fresh scanner (fresh seen-set + cooldown) re-analyzes the same
        # token: its alerts must now carry the history of round one.
        sink2 = RecordingSink()
        scanner2, _ = make_scanner(storage, [pair],
                                   {pair.base_token.address: profile}, sink=sink2)
        await scanner2.run(max_cycles=1)
        assert sink2.sent
        assert all("prior alert(s) for this coin" in e.history_note
                   for e in sink2.sent)


# ---- Staleness-door backlog drain (2026-07-15 "worked once then stopped") ----

async def test_stale_archive_limit_bounds_one_recheck_pass():
    """The door's first encounter with a pre-door backlog (8,690 entries on
    the droplet) archived thousands in one uninterruptible synchronous sweep,
    freezing the event loop and Telegram for minutes. One pass may now
    archive at most watchlist_stale_archive_limit entries; the next pass
    continues the drain."""
    settings = Settings.from_env(env={
        "MEMEINTEL_WORKFLOW_WATCHLIST_RECHECK_CYCLES": "1",
        "MEMEINTEL_WORKFLOW_WATCHLIST_STALE_ARCHIVE_LIMIT": "3",
    })
    # Storage clock 10 days before the scanner clock -> every entry is stale.
    with Storage(":memory:", now_func=lambda: NOW - timedelta(days=10)) as storage:
        for i in range(8):
            tok = TokenIdentity(chain="solana", address=f"Stale{i}", symbol=f"S{i}")
            storage.update_watchlist(tok, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        scanner = make_scanner_with_market(
            storage, [], {}, FakeMarketService({}), settings=settings)
        await scanner.run(max_cycles=1)
        remaining = storage.get_watchlist()
        assert len(remaining) == 5          # exactly 3 archived this pass
        await scanner.run(max_cycles=1)     # next pass drains 3 more
        assert len(storage.get_watchlist()) == 2


async def test_scanner_staleness_door_now_covers_tier3_zombies():
    """Tier-3 (research-only) is where undead coins accumulate, and the
    scanner used to skip them BEFORE the stale check — thousands of zombies
    only the once-a-day routine could drain. The stale check now runs first:
    the agreed design archives ANY coin past the cap."""
    settings = Settings.from_env(env={"MEMEINTEL_WORKFLOW_WATCHLIST_RECHECK_CYCLES": "1"})
    with Storage(":memory:", now_func=lambda: NOW - timedelta(days=10)) as storage:
        zombie = TokenIdentity(chain="solana", address="Zombie1", symbol="ZMB")
        storage.update_watchlist(zombie, WatchlistTier.TIER_3_RESEARCH_ONLY, score=60.0)
        scanner = make_scanner_with_market(
            storage, [], {}, FakeMarketService({}), settings=settings)
        await scanner.run(max_cycles=1)
        assert storage.get_watchlist() == []                     # archived
        assert storage.get_watchlist(include_archived=True)      # not deleted
