"""Tests for the daily operating routine (Spec Part 11)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.collectors.market_data import MajorsSnapshot
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import MarketRegime, WatchlistTier
from meme_intelligence.core.errors import TransientCollectorError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.database.storage import Storage
from meme_intelligence.workflow.daily_routine import DailyRoutine, assess_market_environment

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def make_pair(address="TokenA", symbol="MEMA", liquidity=60000.0) -> DexPair:
    token = TokenIdentity(chain="solana", address=address, symbol=symbol)
    return DexPair(
        chain="solana", pair_address=f"Pool{address}", base_token=token,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=liquidity,
        volume_24h=90_000.0, buys_24h=400, sells_24h=250,
        buyers_24h=300, sellers_24h=180, price_change_24h=5.0,
        pair_created_at=NOW - timedelta(hours=2),
    )


def clean_profile(token: TokenIdentity, honeypot=False) -> SecurityProfile:
    return SecurityProfile(
        token=token, source="goplus",
        is_honeypot=honeypot, cannot_buy=False, cannot_sell_all=False,
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
    def __init__(self, pools):
        self.pools = pools

    async def get_new_pools(self, network, page=1):
        return self.pools if page == 1 else []


class FakeGoPlus:
    def __init__(self, profiles):
        self.profiles = profiles  # address -> SecurityProfile | None

    async def get_token_security(self, chain, address):
        return self.profiles.get(address)


class FakeCoinGecko:
    def __init__(self, snapshot=None, fail=False):
        self.snapshot = snapshot
        self.fail = fail

    async def get_majors(self):
        if self.fail:
            raise TransientCollectorError("coingecko down")
        return self.snapshot


class FakeDexScreener:
    def __init__(self, pairs_by_address=None):
        self.pairs_by_address = pairs_by_address or {}

    async def get_token_pairs(self, address, chain=None):
        return self.pairs_by_address.get(address, [])


@pytest.fixture
def storage():
    with Storage(":memory:", now_func=lambda: NOW) as s:
        yield s


def make_routine(storage, pools, profiles, majors=None, coingecko_fail=False,
                 dexscreener=None) -> DailyRoutine:
    return DailyRoutine(
        Settings.from_env(env={}), storage,
        gecko_client=FakeGecko(pools),
        goplus_client=FakeGoPlus(profiles),
        coingecko_client=FakeCoinGecko(majors, fail=coingecko_fail),
        dexscreener_client=dexscreener or FakeDexScreener(),
        now_func=lambda: NOW,
    )


BULL_MAJORS = MajorsSnapshot(btc_price_usd=100_000.0, btc_change_24h_percent=4.0,
                             eth_change_24h_percent=3.0, sol_change_24h_percent=6.0)


async def test_full_day_healthy_candidate(storage):
    pair = make_pair()
    profile = clean_profile(pair.base_token)
    routine = make_routine(storage, [pair], {pair.base_token.address: profile},
                           majors=BULL_MAJORS)
    report = await routine.run()

    assert report.environment.regime is MarketRegime.BULL
    assert report.candidates_analyzed == 1
    assert len(report.opportunities) == 1
    assert report.opportunities[0].symbol == "MEMA"

    watchlist = storage.get_watchlist()
    assert len(watchlist) == 1
    assert watchlist[0].tier in (WatchlistTier.TIER_1_HIGH_PRIORITY,
                                 WatchlistTier.TIER_2_DEVELOPING,
                                 WatchlistTier.TIER_3_RESEARCH_ONLY)
    assert storage.score_history(pair.base_token)  # snapshot persisted
    assert "DAILY INTELLIGENCE REPORT" in report.render()


async def test_honeypot_rejected_not_watchlisted(storage):
    pair = make_pair(address="TokenBad", symbol="RUG")
    profile = clean_profile(pair.base_token, honeypot=True)
    routine = make_routine(storage, [pair], {pair.base_token.address: profile},
                           majors=BULL_MAJORS)
    report = await routine.run()

    assert storage.get_watchlist() == []
    assert any("honeypot" in r for r in report.biggest_risks)
    journal = storage.journal_entries(pair.base_token)
    assert any("rejected at intake" in e["content"] for e in journal)


async def test_market_provider_failure_degrades_gracefully(storage):
    pair = make_pair()
    routine = make_routine(storage, [pair],
                           {pair.base_token.address: clean_profile(pair.base_token)},
                           coingecko_fail=True)
    report = await routine.run()
    assert report.environment.regime is MarketRegime.UNKNOWN
    assert report.candidates_analyzed == 1  # the day continues without regime data


async def test_unindexed_token_skipped_quietly(storage):
    pair = make_pair()
    routine = make_routine(storage, [pair], {}, majors=BULL_MAJORS)  # goplus knows nothing
    report = await routine.run()
    assert report.candidates_analyzed == 0
    assert storage.get_watchlist() == []


async def test_watchlist_review_archives_dead_token(storage):
    dead_token = TokenIdentity(chain="solana", address="TokenDead", symbol="DEAD")
    storage.update_watchlist(dead_token, WatchlistTier.TIER_2_DEVELOPING, score=70.0)

    routine = make_routine(storage, [], {}, majors=BULL_MAJORS,
                           dexscreener=FakeDexScreener({}))  # no pairs remain
    report = await routine.run()

    archived = [c for c in report.watchlist_changes if c.change == "archived"]
    assert archived and archived[0].token.address == "TokenDead"
    assert storage.get_watchlist() == []


def test_market_environment_mapping():
    bull = assess_market_environment(BULL_MAJORS, risk_on_change=2.0, risk_off_drop=3.0)
    assert bull.regime is MarketRegime.BULL

    bear = assess_market_environment(
        MajorsSnapshot(btc_price_usd=90_000.0, btc_change_24h_percent=-5.0),
        risk_on_change=2.0, risk_off_drop=3.0,
    )
    assert bear.regime is MarketRegime.BEAR

    neutral = assess_market_environment(
        MajorsSnapshot(btc_price_usd=95_000.0, btc_change_24h_percent=0.5),
        risk_on_change=2.0, risk_off_drop=3.0,
    )
    assert neutral.regime is MarketRegime.NEUTRAL

    unknown = assess_market_environment(None, risk_on_change=2.0, risk_off_drop=3.0)
    assert unknown.regime is MarketRegime.UNKNOWN
    assert "unknown" in unknown.summary().lower()
