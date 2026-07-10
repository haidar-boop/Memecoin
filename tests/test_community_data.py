"""Tests for the CoinGecko community-data collector and its pipeline wiring
(Spec Part 5 data feed; "cheap aggregator" decision in handoff/DECISIONS_LOG.md)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.collectors.market_data import CoinGeckoClient
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.enums import CommunityRating, MarketRegime
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.core.rate_limiter import RateLimiter
from meme_intelligence.workflow.pipeline import ResearchPipeline

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")
SETTINGS = Settings.from_env(env={})

COIN_PAYLOAD = {
    "id": "meme-coin",
    "sentiment_votes_up_percentage": 80.0,
    "community_data": {
        "facebook_likes": None,
        "reddit_average_posts_48h": 6.0,
        "reddit_average_comments_48h": 30.0,
        "reddit_subscribers": 12_000,
        "reddit_accounts_active_48h": 800,
        "telegram_channel_user_count": 94_142,
    },
}


def make_client(api_key="") -> CoinGeckoClient:
    return CoinGeckoClient(api_key=api_key,
                           rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache())


def patch_json(monkeypatch, client, payload=COIN_PAYLOAD, error=None):
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        client.requested = {"path": path, "headers": headers}
        if error is not None:
            raise error
        return payload

    monkeypatch.setattr(client, "_get_json", fake_get_json)


# ---- Collector normalization ----

async def test_community_profile_normalized(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client)
    profile = await client.get_community_profile(TOKEN)
    assert profile is not None
    assert profile.source == "coingecko"
    assert profile.telegram_members == 94_142
    assert profile.positive_sentiment_percent == pytest.approx(80.0)
    assert profile.reddit_subscribers == 12_000
    assert profile.reddit_posts_per_day == pytest.approx(3.0)
    assert profile.user_content_per_day == pytest.approx(18.0)
    # Fields the source does not track stay unknown (Rule 8).
    assert profile.twitter_followers is None
    assert profile.bot_follower_percent is None
    assert "solana/contract/TokenAddr1" in client.requested["path"]


async def test_reddit_zeros_mean_untracked_not_zero(monkeypatch):
    payload = {
        "sentiment_votes_up_percentage": 62.0,
        "community_data": {
            "reddit_average_posts_48h": 0.0,
            "reddit_average_comments_48h": 0.0,
            "reddit_subscribers": 0,
            "telegram_channel_user_count": 5_000,
        },
    }
    client = make_client()
    patch_json(monkeypatch, client, payload=payload)
    profile = await client.get_community_profile(TOKEN)
    assert profile.reddit_subscribers is None
    assert profile.reddit_posts_per_day is None
    assert profile.user_content_per_day is None
    assert profile.telegram_members == 5_000


async def test_unsupported_chain_returns_none():
    client = make_client()
    token = TokenIdentity(chain="tron", address="T123")
    assert await client.get_community_profile(token) is None


async def test_unlisted_token_404_returns_none(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client,
               error=CollectorError("coingecko: unexpected status 404 on url: not found",
                                    status_code=404))
    assert await client.get_community_profile(TOKEN) is None


async def test_other_errors_propagate(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client,
               error=CollectorError("coingecko: unexpected status 403 on url", status_code=403))
    with pytest.raises(CollectorError):
        await client.get_community_profile(TOKEN)


async def test_demo_api_key_sent_as_header(monkeypatch):
    client = make_client(api_key="CG-demo-key")
    patch_json(monkeypatch, client)
    await client.get_community_profile(TOKEN)
    assert client.requested["headers"] == {"x-cg-demo-api-key": "CG-demo-key"}


async def test_bnb_chain_maps_to_binance_smart_chain(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client)
    token = TokenIdentity(chain="bnb", address="0xabc")
    await client.get_community_profile(token)
    assert "binance-smart-chain/contract/0xabc" in client.requested["path"]


# ---- Pipeline wiring (community category goes live) ----

def make_pair() -> DexPair:
    return DexPair(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=90_000.0,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )


def make_profile() -> SecurityProfile:
    return SecurityProfile(
        token=TOKEN, source="goplus",
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


class OneShotGoPlus:
    async def get_token_security(self, chain, address):
        return make_profile()


class FakeCommunityClient:
    def __init__(self, payload=COIN_PAYLOAD, listed=True):
        self.inner = make_client()
        self.payload = payload
        self.listed = listed

    async def get_community_profile(self, token):
        if not self.listed:
            return None
        client = make_client()

        async def fake(path, params=None, *, cache_key=None, cache_ttl=None,
                       headers=None, json_body=None):
            return self.payload

        client._get_json = fake
        return await client.get_community_profile(token)


async def test_pipeline_fills_community_category():
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(),
                                community_client=FakeCommunityClient(),
                                now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result.community is not None
    assert result.community_profile.telegram_members == 94_142
    assert result.community.rating is not CommunityRating.ARTIFICIAL
    assert result.master.category_scores.community == pytest.approx(
        result.community.overall_score)
    # Q3 of the decision tree now sees a real community.
    q3 = [s for s in result.master.decision_trace if "community real" in s.question]
    assert q3 and q3[0].answer == "yes"


async def test_pipeline_unlisted_token_keeps_community_unknown():
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(),
                                community_client=FakeCommunityClient(listed=False),
                                now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result.community is None
    assert result.master.category_scores.community is None


async def test_pipeline_without_community_client_unchanged():
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(), now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result.community is None and result.community_profile is None


async def test_ai_snapshot_carries_community_facts():
    from meme_intelligence.ai.reasoning import build_intelligence_snapshot

    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(),
                                community_client=FakeCommunityClient(),
                                now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    snapshot = build_intelligence_snapshot(result)
    assert snapshot["community"]["assessed"] is True
    assert snapshot["community"]["telegram_members"] == 94_142
    assert snapshot["community"]["positive_sentiment_percent"] == pytest.approx(80.0)
    assert "score" in snapshot["community"]
