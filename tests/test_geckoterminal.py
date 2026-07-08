"""Tests for GeckoTerminal new-pool normalization (Spec Part 2, Part 3)."""

from datetime import timezone

import pytest

from meme_intelligence.collectors.market_data import GeckoTerminalClient
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

# Shape mirrors the real GeckoTerminal JSON:API v2 response.
FIXTURE = {
    "data": [
        {
            "id": "solana_PoolAddr1",
            "type": "pool",
            "attributes": {
                "name": "MOON / SOL",
                "address": "PoolAddr1",
                "base_token_price_usd": "0.0005",
                "reserve_in_usd": "42000.50",
                "fdv_usd": "500000",
                "market_cap_usd": None,
                "pool_created_at": "2026-07-07T22:00:00Z",
                "volume_usd": {"h1": "12000.5", "h6": "48000.0", "h24": "150000.75"},
                "price_change_percentage": {"h1": "4.1", "h6": "18.0", "h24": "35.5"},
                "transactions": {
                    "h1": {"buys": 25, "sells": 10, "buyers": 20, "sellers": 8},
                    "h24": {"buys": 320, "sells": 180, "buyers": 210, "sellers": 140},
                },
            },
            "relationships": {
                "base_token": {"data": {"id": "solana_BaseTokenAddr1", "type": "token"}},
                "dex": {"data": {"id": "raydium", "type": "dex"}},
            },
        },
        {
            # Malformed: missing attributes entirely -- skipped, not fatal.
            "id": "solana_PoolAddr2",
            "type": "pool",
        },
    ]
}


def make_client() -> GeckoTerminalClient:
    return GeckoTerminalClient(rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache())


@pytest.fixture
def client(monkeypatch):
    client = make_client()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None):
        client.requested_path = path
        return FIXTURE

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    return client


async def test_new_pool_normalized(client):
    pools = await client.get_new_pools("solana")
    assert len(pools) == 1  # malformed second entry skipped
    pool = pools[0]
    assert pool.chain == "solana"
    assert pool.pair_address == "PoolAddr1"
    assert pool.base_token.address == "BaseTokenAddr1"
    assert pool.base_token.symbol == "MOON"
    assert pool.quote_symbol == "SOL"
    assert pool.dex_id == "raydium"
    assert pool.price_usd == pytest.approx(0.0005)
    assert pool.liquidity_usd == pytest.approx(42000.50)
    assert pool.volume_24h == pytest.approx(150000.75)
    assert pool.buys_24h == 320 and pool.sells_24h == 180
    assert pool.buyers_24h == 210 and pool.sellers_24h == 140
    assert pool.buys_1h == 25 and pool.sells_1h == 10
    assert pool.price_change_1h == pytest.approx(4.1)
    assert pool.volume_1h == pytest.approx(12000.5)
    assert pool.market_cap is None
    assert pool.pair_created_at.tzinfo == timezone.utc
    assert pool.pair_created_at.hour == 22


async def test_endpoint_paths(client):
    await client.get_new_pools("solana")
    assert client.requested_path == "api/v2/networks/solana/new_pools"
    await client.get_trending_pools("base")
    assert client.requested_path == "api/v2/networks/base/trending_pools"


async def test_token_pairs_endpoint_and_chain_alias(client):
    """DexScreener-style chain ids map to GeckoTerminal network ids (Part 15)."""
    await client.get_token_pairs("TokenX", chain="ethereum")
    assert client.requested_path == "api/v2/networks/eth/tokens/TokenX/pools"
    await client.get_token_pairs("TokenY", chain="solana")
    assert client.requested_path == "api/v2/networks/solana/tokens/TokenY/pools"


async def test_token_pairs_without_chain_is_collector_error():
    """Missing chain must be a CollectorError so a provider pool fails over."""
    with pytest.raises(CollectorError, match="chain is required"):
        await make_client().get_token_pairs("TokenX")


async def test_bad_payload_raises(monkeypatch):
    client = make_client()

    async def bad(path, params=None, *, cache_key=None, cache_ttl=None):
        return "nope"

    monkeypatch.setattr(client, "_get_json", bad)
    with pytest.raises(CollectorError, match="expected JSON object"):
        await client.get_new_pools("solana")


async def test_empty_network_rejected():
    with pytest.raises(ValueError):
        await make_client().get_new_pools("")
