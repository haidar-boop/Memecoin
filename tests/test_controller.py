"""Tests for the continuous scanning controller (Spec Part 13)."""

from datetime import datetime, timedelta, timezone

from meme_intelligence.alerts.notification_engine import NotificationEngine
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
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=90_000.0,
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


async def test_failed_cycle_backs_off_and_recovers():
    pair = make_pair()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner, sleeps = make_scanner(
            storage, [pair],
            {pair.base_token.address: clean_profile(pair.base_token)},
            fail_on_call=1,  # first discovery call raises
        )
        history = await scanner.run(max_cycles=2)

        # cycle 1 failed (no stats), cycle 2 succeeded after backoff
        assert len(history) == 1
        assert history[0].analyzed == 1
        assert sleeps and sleeps[0] == 5.0  # error backoff, not the normal interval


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

import dataclasses as _dc

from meme_intelligence.core.enums import AlertPriority, WatchlistTier
from meme_intelligence.core.models import TokenIdentity


class FakeMarketService:
    """Market service double: serves best pairs and a fixed verification verdict."""

    def __init__(self, pairs_by_address=None, verdict=(None, "no second source")):
        self.pairs_by_address = pairs_by_address or {}
        self.verdict = verdict
        self.recheck_calls = 0
        self.verify_calls = 0

    async def get_best_pair(self, address, chain=None):
        self.recheck_calls += 1
        return self.pairs_by_address.get(address)

    async def cross_check_liquidity(self, pair):
        self.verify_calls += 1
        return self.verdict


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
        opportunity = [e for e in sink.sent if "opportunity" in e.alert_type]
        assert opportunity
        assert opportunity[0].priority is AlertPriority.LOW  # MEDIUM downgraded
        assert any("DOWNGRADED" in r for r in opportunity[0].reasons)


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
        opportunity = [e for e in sink.sent if "opportunity" in e.alert_type]
        assert opportunity
        assert any("confirmed" in r for r in opportunity[0].reasons)


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


async def test_dead_watchlist_token_archived_with_postmortem():
    """Part 29 S1: a tracked token whose pool collapsed gets ONE MEDIUM
    post-mortem and is archived — not re-tiered on its pump-window score
    and re-warned at HIGH every recheck."""
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
    the settings flags are authoritative over what the caller wired in."""
    pair = make_pair()
    wallet, ai = RecordingWalletService(), RecordingAIService()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_scanner_with_metered(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            wallet=wallet, ai=ai,  # wired, but default settings say off
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1
        assert wallet.gather_calls == []
        assert ai.judge_calls == 0


async def test_metered_services_run_when_opted_in():
    pair = make_pair()
    wallet, ai = RecordingWalletService(), RecordingAIService()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        scanner = make_scanner_with_metered(
            storage, [pair], {pair.base_token.address: clean_profile(pair.base_token)},
            wallet=wallet, ai=ai, settings=metered_on_settings(),
        )
        history = await scanner.run(max_cycles=1)
        assert history[0].analyzed == 1  # a no-data wallet answer never blocks analysis
        assert wallet.gather_calls == [pair.base_token.address]
        assert ai.judge_calls == 1


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
