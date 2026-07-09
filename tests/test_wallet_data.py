"""Tests for the wallet-data collectors (Spec Part 17; Helius + Birdeye)."""

import pytest

from meme_intelligence.collectors.wallet_data import (
    BirdeyeClient,
    HeliusClient,
    WalletDataService,
)
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError, TransientCollectorError
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.core.rate_limiter import RateLimiter

MINT = "MintAddr1111111111111111111111111111111111"
TOKEN = TokenIdentity(chain="solana", address=MINT, symbol="MEME")


def make_helius() -> HeliusClient:
    return HeliusClient("test-key", rate_limiter=RateLimiter(100.0, burst=20), cache=TTLCache())


def make_birdeye() -> BirdeyeClient:
    return BirdeyeClient("test-key", rate_limiter=RateLimiter(100.0, burst=20), cache=TTLCache())


HELIUS_RESPONSES = {
    "getTokenSupply": {"value": {"uiAmount": 1_000_000.0}},
    "getTokenLargestAccounts": {"value": [
        {"address": "TokAcc1", "uiAmount": 150_000.0},
        {"address": "TokAcc2", "uiAmount": 50_000.0},
        {"address": "TokAcc3", "uiAmount": 10_000.0},
    ]},
    "getMultipleAccounts": {"value": [
        {"data": {"parsed": {"info": {"owner": "OwnerWhale1"}}}},
        {"data": {"parsed": {"info": {"owner": "OwnerWhale2"}}}},
        {"data": {"parsed": {"info": {"owner": "1nc1nerator11111111111111111111111111111111"}}}},
    ]},
}

ENHANCED_TXS = [
    {
        "timestamp": 1783490000,
        "tokenTransfers": [
            {"mint": MINT, "fromUserAccount": "WalletA", "toUserAccount": "WalletB",
             "tokenAmount": 1000.0},
            {"mint": "OtherMint", "fromUserAccount": "X", "toUserAccount": "Y",
             "tokenAmount": 5.0},
        ],
    },
    {"timestamp": 1783490100, "tokenTransfers": []},
]


def patch_helius(monkeypatch, client):
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        if json_body is not None:  # RPC call
            method = json_body["method"]
            return {"jsonrpc": "2.0", "id": 1, "result": HELIUS_RESPONSES[method]}
        return ENHANCED_TXS  # enhanced API call

    monkeypatch.setattr(client, "_get_json", fake_get_json)


async def test_helius_top_holders_resolved_to_owners(monkeypatch):
    client = make_helius()
    patch_helius(monkeypatch, client)
    holders = await client.get_top_holders(MINT)
    assert len(holders) == 2  # burn address excluded
    assert holders[0].owner == "OwnerWhale1"
    assert holders[0].percent == pytest.approx(15.0)  # 150k / 1M
    assert holders[1].percent == pytest.approx(5.0)


async def test_helius_transfers_filtered_to_mint(monkeypatch):
    client = make_helius()
    patch_helius(monkeypatch, client)
    transfers = await client.get_recent_transfers(MINT)
    assert len(transfers) == 1
    assert transfers[0].from_owner == "WalletA"
    assert transfers[0].ui_amount == 1000.0
    assert transfers[0].timestamp.year == 2026


async def test_helius_rpc_error_raises(monkeypatch):
    client = make_helius()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    with pytest.raises(CollectorError, match="RPC error"):
        await client.get_top_holders(MINT)


BIRDEYE_TRADES = {
    "success": True,
    "data": {"items": [
        {
            "owner": "TraderA", "side": "buy", "blockUnixTime": 1783490000,
            "base": {"address": "So1111", "uiAmount": 0.5, "price": 80.0},
            "quote": {"address": MINT, "uiAmount": 250.0, "price": 0.16},
        },
        {
            "owner": "TraderB", "side": "sell", "blockUnixTime": 1783490100,
            "base": {"address": "So1111", "uiAmount": 0.25, "price": 80.0},
            "quote": {"address": MINT, "uiAmount": 125.0, "price": 0.16},
        },
        {"owner": None, "side": "buy"},  # malformed: skipped
    ]},
}

BIRDEYE_OVERVIEW = {"success": True, "data": {"holder": 12345, "uniqueWallet24h": 678}}


async def test_birdeye_trades_usd_computed_from_legs(monkeypatch):
    client = make_birdeye()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        return BIRDEYE_TRADES if "txs" in path else BIRDEYE_OVERVIEW

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    trades = await client.get_recent_trades(MINT)
    assert len(trades) == 2
    assert trades[0].volume_usd == pytest.approx(40.0)  # 0.5 SOL x $80
    assert trades[0].price_usd == pytest.approx(0.16)   # our mint's leg price
    assert trades[1].side == "sell"


async def test_birdeye_overview_parsed(monkeypatch):
    client = make_birdeye()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        return BIRDEYE_OVERVIEW

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    overview = await client.get_token_overview(MINT)
    assert overview == {"holder_count": 12345, "unique_wallets_24h": 678}


async def test_birdeye_overview_accepts_decimal_string_holder_counts(monkeypatch):
    """Bug-hunt: int("1234.0") raises ValueError; providers sometimes send
    counts as decimal strings."""
    client = make_birdeye()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        return {"success": True, "data": {"holder": "1234.0", "uniqueWallet24h": "56.0"}}

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    overview = await client.get_token_overview(MINT)
    assert overview == {"holder_count": 1234, "unique_wallets_24h": 56}


async def test_helius_key_redacted_from_error_messages():
    """Bug-hunt: the RPC path embeds the key directly (no header auth
    option); a timeout/429/5xx must not leak it in plaintext (Rule 16)."""
    client = HeliusClient("SECRET_HELIUS_KEY_123", rate_limiter=RateLimiter(100.0, burst=10))
    assert client._scrub(
        "https://mainnet.helius-rpc.com/?api-key=SECRET_HELIUS_KEY_123 timed out"
    ) == "https://mainnet.helius-rpc.com/?api-key=***REDACTED*** timed out"


async def test_birdeye_unsuccessful_response_raises(monkeypatch):
    client = make_birdeye()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        return {"success": False, "message": "bad key"}

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    with pytest.raises(CollectorError, match="unsuccessful"):
        await client.get_token_overview(MINT)


async def test_helius_partial_failure_still_attributes_the_source():
    """Bug-hunt: get_top_holders and get_recent_transfers shared one try
    block, so a transfers failure discarded the "helius" source tag even
    though get_top_holders had already succeeded — real Helius holder
    data came back with no attribution of where it came from."""
    helius = make_helius()

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        if json_body is not None:  # RPC call (get_top_holders) succeeds
            method = json_body["method"]
            return {"jsonrpc": "2.0", "id": 1, "result": HELIUS_RESPONSES[method]}
        raise TransientCollectorError("enhanced API down")  # get_recent_transfers fails

    import unittest.mock as _mock
    with _mock.patch.object(helius, "_get_json", fake_get_json):
        service = WalletDataService(helius, None)
        data = await service.gather(TOKEN)
    assert data.sources == ("helius",)  # attributed despite the partial failure
    assert len(data.top_holders) == 2
    assert data.recent_transfers == ()


async def test_service_degrades_when_one_source_fails(monkeypatch):
    helius, birdeye = make_helius(), make_birdeye()
    patch_helius(monkeypatch, helius)

    async def birdeye_down(path, params=None, *, cache_key=None, cache_ttl=None,
                           headers=None, json_body=None):
        raise TransientCollectorError("birdeye down")

    monkeypatch.setattr(birdeye, "_get_json", birdeye_down)
    service = WalletDataService(helius, birdeye)
    data = await service.gather(TOKEN)
    assert data.sources == ("helius",)     # partial, not empty
    assert len(data.top_holders) == 2
    assert data.recent_trades == ()         # honestly missing, not zeroed


def test_missing_api_key_rejected():
    with pytest.raises(ValueError):
        HeliusClient("", rate_limiter=RateLimiter(10.0))
    with pytest.raises(ValueError):
        WalletDataService(None, None)
