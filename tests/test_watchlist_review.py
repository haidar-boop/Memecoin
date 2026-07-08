"""Tests for the shared watchlist reviewer (Spec Parts 11/16/28)."""

from datetime import datetime, timedelta, timezone

from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import WatchlistTier
from meme_intelligence.core.errors import TransientCollectorError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.database.storage import Storage
from meme_intelligence.workflow.pipeline import ResearchPipeline
from meme_intelligence.workflow.watchlist_review import review_entries

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings.from_env(env={})


def token(symbol: str) -> TokenIdentity:
    return TokenIdentity(chain="solana", address=f"Token{symbol}", symbol=symbol)


def make_pair(tok: TokenIdentity, liquidity=90_000.0) -> DexPair:
    return DexPair(
        chain="solana", pair_address=f"Pool{tok.symbol}", base_token=tok,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=liquidity,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )


def clean_profile(tok: TokenIdentity, honeypot=False) -> SecurityProfile:
    return SecurityProfile(
        token=tok, source="goplus",
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


class FakeMarket:
    def __init__(self, pairs_by_address, fail_for=frozenset()):
        self.pairs_by_address = pairs_by_address
        self.fail_for = fail_for

    async def get_token_pairs(self, address, chain=None):
        if address in self.fail_for:
            raise TransientCollectorError("market data down")
        return self.pairs_by_address.get(address, [])


class MappedGoPlus:
    def __init__(self, profiles):
        self.profiles = profiles

    async def get_token_security(self, chain, address):
        return self.profiles.get(address)


def make_pipeline(profiles) -> ResearchPipeline:
    return ResearchPipeline(SETTINGS, MappedGoPlus(profiles), now_func=lambda: NOW)


async def test_healthy_entry_retiered_and_snapshotted():
    tok = token("GOOD")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tok, WatchlistTier.TIER_3_RESEARCH_ONLY, score=60.0)
        changes = await review_entries(
            storage,
            FakeMarket({tok.address: [make_pair(tok)]}),
            make_pipeline({tok.address: clean_profile(tok)}),
            limit=5,
        )
        assert changes
        # healthy re-assessment promotes out of research-only
        entry = storage.get_watchlist()[0]
        assert entry.tier in (WatchlistTier.TIER_1_HIGH_PRIORITY,
                              WatchlistTier.TIER_2_DEVELOPING)
        assert storage.score_history(tok)  # snapshot persisted


async def test_dead_market_archives():
    tok = token("DEAD")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tok, WatchlistTier.TIER_1_HIGH_PRIORITY, score=85.0)
        changes = await review_entries(
            storage, FakeMarket({tok.address: []}), make_pipeline({}), limit=5,
        )
        assert any(c.change == "archived" for c in changes)
        assert storage.get_watchlist() == []


async def test_turned_honeypot_archived():
    tok = token("TRAP")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tok, WatchlistTier.TIER_1_HIGH_PRIORITY, score=85.0)
        changes = await review_entries(
            storage,
            FakeMarket({tok.address: [make_pair(tok)]}),
            make_pipeline({tok.address: clean_profile(tok, honeypot=True)}),
            limit=5,
        )
        archived = [c for c in changes if c.change == "archived"]
        assert archived and "Avoid" in archived[0].detail


async def test_skip_set_and_limit_respected():
    tok_a, tok_b = token("AAAA"), token("BBBB")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tok_a, WatchlistTier.TIER_1_HIGH_PRIORITY, score=85.0)
        storage.update_watchlist(tok_b, WatchlistTier.TIER_1_HIGH_PRIORITY, score=84.0)
        market = FakeMarket({
            tok_a.address: [make_pair(tok_a)],
            tok_b.address: [make_pair(tok_b)],
        })
        pipeline = make_pipeline({
            tok_a.address: clean_profile(tok_a),
            tok_b.address: clean_profile(tok_b),
        })
        changes = await review_entries(
            storage, market, pipeline, limit=5, skip={tok_a.address.lower()},
        )
        touched = {c.token.address for c in changes}
        assert tok_a.address not in touched
        assert tok_b.address in touched

        # limit=0 -> nothing reviewed at all
        changes = await review_entries(storage, market, pipeline, limit=0)
        assert changes == []


async def test_market_failure_skips_gracefully():
    tok = token("FAIL")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tok, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        changes = await review_entries(
            storage, FakeMarket({}, fail_for={tok.address}), make_pipeline({}), limit=5,
        )
        assert changes == []  # not archived, not modified — just skipped this round
        assert storage.get_watchlist()  # still tracked


async def test_on_result_callback_invoked():
    tok = token("GOOD")
    seen = []

    async def callback(result):
        seen.append(result.pair.base_token.symbol)

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.update_watchlist(tok, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        await review_entries(
            storage,
            FakeMarket({tok.address: [make_pair(tok)]}),
            make_pipeline({tok.address: clean_profile(tok)}),
            limit=5, on_result=callback,
        )
        assert seen == ["GOOD"]
