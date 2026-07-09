"""Tests for DexScreener response normalization (Spec Part 2, Part 32 Rule 3)."""

from datetime import timezone

import pytest

from meme_intelligence.collectors.market_data import DexScreenerClient
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

# Shape mirrors the real DexScreener /latest/dex API response.
FIXTURE = {
    "schemaVersion": "1.0.0",
    "pairs": [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "url": "https://dexscreener.com/solana/pairaddr1",
            "pairAddress": "PairAddr1",
            "baseToken": {"address": "BaseAddr1", "name": "Test Meme", "symbol": "MEME"},
            "quoteToken": {"address": "QuoteAddr", "name": "Wrapped SOL", "symbol": "SOL"},
            "priceUsd": "0.00012345",
            "txns": {"h1": {"buys": 12, "sells": 5}, "h24": {"buys": 150, "sells": 90}},
            "volume": {"h1": 9000.5, "h6": 60000.0, "h24": 250000.5},
            "priceChange": {"h1": 3.2, "h6": 15.0, "h24": 42.7},
            "liquidity": {"usd": 85000.25, "base": 1000, "quote": 500},
            "fdv": 1200000,
            "marketCap": 1100000,
            "pairCreatedAt": 1751500800000,
        },
        {
            # Young pair with many missing fields -- must normalize to None, not crash.
            "chainId": "base",
            "pairAddress": "PairAddr2",
            "baseToken": {"address": "BaseAddr2", "symbol": "NEW"},
            "quoteToken": {},
        },
        {
            # Malformed entry (no pairAddress) -- must be skipped, not abort the batch.
            "chainId": "solana",
            "baseToken": {"address": "BaseAddr3"},
        },
    ],
}


def make_client() -> DexScreenerClient:
    return DexScreenerClient(
        rate_limiter=RateLimiter(100.0, burst=10),
        cache=TTLCache(),
    )


@pytest.fixture
def client(monkeypatch):
    client = make_client()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None):
        client.requested_path = path
        client.requested_params = params
        return FIXTURE

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    return client


async def test_full_pair_is_normalized(client):
    pairs = await client.get_token_pairs("BaseAddr1")
    pair = pairs[0]
    assert pair.chain == "solana"
    assert pair.pair_address == "PairAddr1"
    assert pair.base_token.symbol == "MEME"
    assert pair.quote_symbol == "SOL"
    assert pair.price_usd == pytest.approx(0.00012345)  # string -> float
    assert pair.liquidity_usd == pytest.approx(85000.25)
    assert pair.market_cap == pytest.approx(1100000)
    assert pair.buys_24h == 150 and pair.sells_24h == 90
    assert pair.buys_1h == 12 and pair.sells_1h == 5
    assert pair.price_change_1h == pytest.approx(3.2)
    assert pair.price_change_6h == pytest.approx(15.0)
    assert pair.volume_1h == pytest.approx(9000.5)
    assert pair.volume_6h == pytest.approx(60000.0)
    assert pair.pair_created_at.tzinfo == timezone.utc
    assert pair.pair_created_at.year == 2025


async def test_missing_fields_become_none(client):
    pairs = await client.get_token_pairs("BaseAddr2")
    young = next(p for p in pairs if p.pair_address == "PairAddr2")
    assert young.price_usd is None
    assert young.liquidity_usd is None
    assert young.volume_24h is None
    assert young.buys_24h is None
    assert young.pair_created_at is None


async def test_huge_pair_created_at_does_not_crash_the_batch(monkeypatch):
    """Bug-hunt: OverflowError from an out-of-range pairCreatedAt escaped
    _from_ms_timestamp and killed the whole normalization batch."""
    client = make_client()
    bad_fixture = {"pairs": [
        {**FIXTURE["pairs"][0], "pairAddress": "PairBad", "pairCreatedAt": 1e30},
    ]}

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None):
        return bad_fixture

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    pairs = await client.get_token_pairs("BaseAddr1")  # must not raise
    assert pairs[0].pair_created_at is None


async def test_malformed_entry_skipped_not_fatal(client):
    pairs = await client.get_token_pairs("anything")
    assert len(pairs) == 2  # third fixture entry silently skipped (and logged)


async def test_chain_filter(client):
    pairs = await client.get_token_pairs("anything", chain="base")
    assert [p.chain for p in pairs] == ["base"]


async def test_search_uses_query_param(client):
    await client.search_pairs("MEME")
    assert client.requested_path == "latest/dex/search"
    assert client.requested_params == {"q": "MEME"}


async def test_non_object_payload_raises(monkeypatch):
    client = make_client()

    async def bad_payload(path, params=None, *, cache_key=None, cache_ttl=None):
        return ["not", "an", "object"]

    monkeypatch.setattr(client, "_get_json", bad_payload)
    with pytest.raises(CollectorError, match="expected JSON object"):
        await client.get_token_pairs("x")


async def test_empty_arguments_rejected():
    client = make_client()
    with pytest.raises(ValueError):
        await client.get_token_pairs("")
    with pytest.raises(ValueError):
        await client.search_pairs("")
