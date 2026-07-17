"""Tests for GoPlus security-data normalization (Spec Parts 4/18/33)."""

import pytest

from meme_intelligence.collectors.security_data import GoPlusClient
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

EVM_ADDRESS = "0xAbCdEf0000000000000000000000000000000001"

EVM_FIXTURE = {
    "code": 1,
    "message": "OK",
    "result": {
        EVM_ADDRESS.lower(): {
            "token_name": "Test Meme",
            "token_symbol": "MEME",
            "is_honeypot": "0",
            "cannot_buy": "0",
            "cannot_sell_all": "0",
            "is_open_source": "1",
            "is_proxy": "0",
            "is_mintable": "1",
            "owner_address": "0x1111111111111111111111111111111111111111",
            "hidden_owner": "0",
            "can_take_back_ownership": "0",
            "is_blacklisted": "0",
            "transfer_pausable": "1",
            "selfdestruct": "0",
            "buy_tax": "0.02",
            "sell_tax": "0.30",
            "slippage_modifiable": "1",
            "fake_token": "0",
            "is_airdrop_scam": "",
            "anti_whale_modifiable": "0",
            "personal_slippage_modifiable": "0",
            "trading_cooldown": "0",
            "honeypot_with_same_creator": "2",
            "holder_count": "1523",
            "creator_percent": "0.08",
            "creator_address": "0xdeployer1",
            "owner_percent": "0.01",
            "holders": [
                {"address": "0xdead000000000000000000000000000000000000", "percent": "0.5", "is_locked": 0},
                {"address": "0xaaa1", "percent": "0.12", "is_locked": 0},
                {"address": "0xaaa2", "percent": "0.06", "is_locked": 0},
                {"address": "0xaaa3", "percent": "0.04", "is_locked": 1},
            ],
            "lp_holders": [
                {"address": "0xlock1", "percent": "0.70", "is_locked": 1},
                {"address": "0xfree1", "percent": "0.30", "is_locked": 0},
            ],
        }
    },
}

SOL_ADDRESS = "SoLTokenAddr111111111111111111111111111111"

SOLANA_FIXTURE = {
    "code": 1,
    "message": "OK",
    "result": {
        SOL_ADDRESS: {
            "metadata": {"name": "Sol Meme", "symbol": "SMEME"},
            "mintable": {"status": "0"},
            "freezable": {"status": "1"},
            "closable": {"status": "0"},
            "balance_mutable_authority": {"status": "0"},
            "non_transferable": "0",
            "transfer_fee_upgradable": {"status": "0"},
            "holder_count": "890",
            "holders": [
                {"address": "So1Holder1", "percent": "0.15", "is_locked": 0},
                {"address": "So1Holder2", "percent": "0.05", "is_locked": 0},
            ],
            "creators": [{"address": "So1Creator", "percent": "0.03"}],
            "lp_holders": [
                {"address": "So1Burn", "percent": "1.0", "is_locked": 1},
            ],
        }
    },
}


def make_client() -> GoPlusClient:
    return GoPlusClient(rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache())


def patch_payload(monkeypatch, client, payload):
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None):
        client.requested_path = path
        client.requested_params = params
        return payload

    monkeypatch.setattr(client, "_get_json", fake_get_json)


async def test_evm_profile_normalized(monkeypatch):
    client = make_client()
    patch_payload(monkeypatch, client, EVM_FIXTURE)
    profile = await client.get_token_security("ethereum", EVM_ADDRESS)

    assert client.requested_path == "api/v1/token_security/1"
    assert profile.token.symbol == "MEME"
    assert profile.is_honeypot is False
    assert profile.is_mintable is True
    assert profile.ownership_renounced is False  # real owner address set
    assert profile.trading_pausable is True
    assert profile.buy_tax_percent == pytest.approx(2.0)    # fraction -> percent
    assert profile.sell_tax_percent == pytest.approx(30.0)
    assert profile.is_airdrop_scam is None                   # empty string -> unknown
    assert profile.honeypot_same_creator_count == 2
    assert profile.holder_count == 1523
    # burn address and locked holder excluded from concentration:
    assert profile.top_holder_percent == pytest.approx(12.0)
    assert profile.top10_holder_percent == pytest.approx(18.0)
    assert profile.creator_percent == pytest.approx(8.0)
    assert profile.creator_address == "0xdeployer1"
    assert profile.lp_locked_percent == pytest.approx(70.0)


async def test_evm_renounced_owner(monkeypatch):
    fixture = {
        "code": 1,
        "result": {
            EVM_ADDRESS.lower(): {
                "owner_address": "0x0000000000000000000000000000000000000000",
            }
        },
    }
    client = make_client()
    patch_payload(monkeypatch, client, fixture)
    profile = await client.get_token_security("base", EVM_ADDRESS)
    assert profile.ownership_renounced is True
    assert profile.is_honeypot is None  # unreported facts stay unknown


async def test_solana_profile_normalized(monkeypatch):
    client = make_client()
    patch_payload(monkeypatch, client, SOLANA_FIXTURE)
    profile = await client.get_token_security("solana", SOL_ADDRESS)

    assert client.requested_path == "api/v1/solana/token_security"
    assert profile.token.symbol == "SMEME"
    assert profile.is_mintable is False
    assert profile.is_freezable is True
    assert profile.selfdestruct is False
    assert profile.cannot_sell_all is False
    assert profile.top_holder_percent == pytest.approx(15.0)
    assert profile.creator_percent == pytest.approx(3.0)
    assert profile.creator_address == "So1Creator"
    assert profile.lp_locked_percent == pytest.approx(100.0)


async def test_unknown_chain_rejected():
    client = make_client()
    with pytest.raises(CollectorError, match="unsupported chain"):
        await client.get_token_security("dogechain", EVM_ADDRESS)


async def test_api_error_code_raises(monkeypatch):
    client = make_client()
    patch_payload(monkeypatch, client, {"code": 4029, "message": "rate limit"})
    with pytest.raises(CollectorError, match="4029"):
        await client.get_token_security("ethereum", EVM_ADDRESS)


async def test_missing_result_returns_none(monkeypatch):
    client = make_client()
    patch_payload(monkeypatch, client, {"code": 1, "message": "OK", "result": {}})
    assert await client.get_token_security("ethereum", EVM_ADDRESS) is None
