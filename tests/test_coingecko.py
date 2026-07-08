"""Tests for the CoinGecko majors client (Spec Part 11, Section 2)."""

import pytest

from meme_intelligence.collectors.market_data import CoinGeckoClient
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

FIXTURE = {
    "bitcoin": {"usd": 101234.5, "usd_24h_change": 2.34},
    "ethereum": {"usd": 3456.7, "usd_24h_change": -1.2},
    "solana": {"usd": 210.9, "usd_24h_change": 5.6},
}


def make_client() -> CoinGeckoClient:
    return CoinGeckoClient(rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache())


async def test_majors_normalized(monkeypatch):
    client = make_client()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None):
        client.requested_params = params
        return FIXTURE

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    snapshot = await client.get_majors()
    assert snapshot.btc_price_usd == pytest.approx(101234.5)
    assert snapshot.btc_change_24h_percent == pytest.approx(2.34)
    assert snapshot.eth_change_24h_percent == pytest.approx(-1.2)
    assert snapshot.sol_change_24h_percent == pytest.approx(5.6)
    assert "bitcoin" in client.requested_params["ids"]


async def test_missing_coins_become_none(monkeypatch):
    client = make_client()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None):
        return {"bitcoin": {"usd": 100000.0}}  # no change field, no eth/sol

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    snapshot = await client.get_majors()
    assert snapshot.btc_price_usd == 100000.0
    assert snapshot.btc_change_24h_percent is None
    assert snapshot.eth_change_24h_percent is None


async def test_bad_payload_raises(monkeypatch):
    client = make_client()

    async def bad(path, params=None, *, cache_key=None, cache_ttl=None):
        return ["nope"]

    monkeypatch.setattr(client, "_get_json", bad)
    with pytest.raises(CollectorError):
        await client.get_majors()
