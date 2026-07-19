"""Tests for the LunarCrush social-data collector (Roadmap item 5)."""

import pytest

from meme_intelligence.collectors.social_data import LunarCrushClient
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError, TransientCollectorError
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.core.rate_limiter import RateLimiter

TOKEN = TokenIdentity(chain="solana", address="MintAddr1111", symbol="MEME")


def make_client() -> LunarCrushClient:
    return LunarCrushClient("test-key", rate_limiter=RateLimiter(100.0, burst=20),
                            cache=TTLCache())


COINS_PAYLOAD = {
    "data": [
        {
            "id": 1, "symbol": "MEME", "name": "Meme Coin", "topic": "meme-coin",
            "sentiment": 70.0, "social_dominance": 2.5, "social_volume_24h": 5000,
            "galaxy_score": 62.0, "alt_rank": 150,
            "blockchains": [
                {"type": "coin", "network": "solana", "address": "MintAddr1111", "decimals": 9},
                # Same symbol, DIFFERENT chain, DIFFERENT address — proves the
                # directory keys on (chain, address), not symbol/name alone.
                {"type": "coin", "network": "ethereum", "address": "0xOtherMeme", "decimals": 18},
            ],
        },
        {
            "id": 2, "symbol": "OTHER", "name": "Other Coin", "topic": "other-coin",
            "sentiment": 40.0,
            "blockchains": [
                # Unmapped network string: must be skipped, not raise.
                "not-a-dict-skip-me",
                {"type": "coin", "network": "tron", "address": "TAbc123"},
            ],
        },
        "not-a-dict-skip-me",
    ]
}

TOPIC_PAYLOAD = {
    "data": {
        "types_sentiment": {"twitter": 82.0, "reddit": 55.0},
        "types_count": {"twitter": 47, "reddit": 3},
        "types_interactions": {"twitter": 9000},
        "trend": "up",
        "interactions_24h": 12000,
        "num_contributors": 300,
        "num_posts": 55,
    }
}


def patch_json(monkeypatch, client, *, coins=COINS_PAYLOAD, topic=TOPIC_PAYLOAD,
               coins_error=None, topic_error=None):
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        client_calls = getattr(client, "_calls", None)
        if client_calls is None:
            client_calls = client._calls = []
        client_calls.append(path)
        if "coins/list" in path:
            if coins_error is not None:
                raise coins_error
            return coins
        if topic_error is not None:
            raise topic_error
        return topic

    monkeypatch.setattr(client, "_get_json", fake_get_json)


# ---- Directory matching (contract-address, chain-enforced) ----

async def test_matches_by_contract_address_on_the_right_chain(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client)
    profile = await client.get_community_profile(TOKEN)
    assert profile is not None
    assert profile.source == "lunarcrush"


async def test_same_address_under_wrong_chain_does_not_match(monkeypatch):
    """The Ethereum entry's address must never satisfy a Solana lookup for
    the same coin — chain-matching, not just address-matching."""
    client = make_client()
    patch_json(monkeypatch, client)
    wrong_chain_token = TokenIdentity(chain="ethereum", address="MintAddr1111", symbol="MEME")
    assert await client.get_community_profile(wrong_chain_token) is None

    right_chain_token = TokenIdentity(chain="ethereum", address="0xOtherMeme", symbol="MEME")
    profile = await client.get_community_profile(right_chain_token)
    assert profile is not None


async def test_no_match_returns_none(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client)
    unknown = TokenIdentity(chain="solana", address="NeverListed", symbol="XXX")
    assert await client.get_community_profile(unknown) is None


async def test_unmapped_network_skipped_without_raising(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client)
    tron_token = TokenIdentity(chain="tron", address="TAbc123", symbol="OTHER")
    assert await client.get_community_profile(tron_token) is None


async def test_malformed_blockchains_and_data_never_raise(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client, coins={"data": "not-a-list"})
    assert await client.get_community_profile(TOKEN) is None

    client2 = make_client()
    patch_json(monkeypatch, client2, coins={"data": [{"blockchains": "not-a-list"}]})
    assert await client2.get_community_profile(TOKEN) is None

    client3 = make_client()
    patch_json(monkeypatch, client3, coins={})
    assert await client3.get_community_profile(TOKEN) is None

    client4 = make_client()
    patch_json(monkeypatch, client4, coins=None)
    assert await client4.get_community_profile(TOKEN) is None


# ---- Sentiment/content mapping ----

async def test_twitter_specific_fields_preferred_over_coin_level(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client)
    profile = await client.get_community_profile(TOKEN)
    assert profile.positive_sentiment_percent == pytest.approx(82.0)  # twitter, not coin-level 70
    assert profile.user_content_per_day == pytest.approx(47.0)
    assert profile.social_trend == "up"
    assert profile.social_volume_24h == 5000
    assert profile.social_dominance_percent == pytest.approx(2.5)
    assert profile.galaxy_score == pytest.approx(62.0)
    assert profile.alt_rank == 150


async def test_coin_level_sentiment_fallback_when_topic_lacks_platform_data(monkeypatch):
    client = make_client()
    topic_without_twitter = {"data": {"types_sentiment": {}, "types_count": {}, "trend": None}}
    patch_json(monkeypatch, client, topic=topic_without_twitter)
    profile = await client.get_community_profile(TOKEN)
    assert profile.positive_sentiment_percent == pytest.approx(70.0)  # coin-level fallback
    assert profile.user_content_per_day is None
    assert profile.social_trend is None


async def test_topic_detail_failure_degrades_gracefully(monkeypatch):
    """A topic-detail failure must not erase the coin-level data already
    fetched (Rule 9)."""
    client = make_client()
    patch_json(monkeypatch, client, topic_error=TransientCollectorError("topic API down"))
    profile = await client.get_community_profile(TOKEN)
    assert profile is not None
    assert profile.positive_sentiment_percent == pytest.approx(70.0)  # coin-level survives
    assert profile.social_volume_24h == 5000                          # coin-level survives
    assert profile.user_content_per_day is None                       # topic data honestly missing


async def test_directory_failure_returns_none_not_raise(monkeypatch):
    client = make_client()
    patch_json(monkeypatch, client, coins_error=TransientCollectorError("coins list down"))
    assert await client.get_community_profile(TOKEN) is None


# ---- Rule 8: never fabricate per-account fields ----

async def test_never_sets_per_account_fields(monkeypatch):
    """The single most important test in this module: LunarCrush's public
    API has no per-account follower/engagement/bot-detection data, so this
    client must NEVER populate those fields — regression guard for the
    Rule-8 non-fabrication decision."""
    client = make_client()
    patch_json(monkeypatch, client)
    profile = await client.get_community_profile(TOKEN)
    assert profile is not None
    assert profile.twitter_followers is None
    assert profile.twitter_engagement_rate_percent is None
    assert profile.twitter_growth_rate_7d_percent is None
    assert profile.bot_follower_percent is None


def test_missing_api_key_rejected():
    with pytest.raises(ValueError):
        LunarCrushClient("", rate_limiter=RateLimiter(10.0))


async def test_key_redacted_from_error_messages():
    client = LunarCrushClient("SECRET_LC_KEY", rate_limiter=RateLimiter(100.0, burst=10))
    assert client._scrub(
        "https://lunarcrush.com/api4/public/coins/list/v1 timed out with SECRET_LC_KEY"
    ) == "https://lunarcrush.com/api4/public/coins/list/v1 timed out with ***REDACTED***"


async def test_other_directory_errors_propagate_from_directory(monkeypatch):
    """A non-transient CollectorError building the directory should still be
    caught by get_community_profile and reported as no-match (honest gap),
    matching the CoinGecko client's "unlisted" behavior for the caller."""
    client = make_client()
    patch_json(monkeypatch, client, coins_error=CollectorError("lunarcrush: bad request",
                                                               status_code=400))
    assert await client.get_community_profile(TOKEN) is None
