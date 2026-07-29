"""An unreadable wallet balance must never read as an empty wallet (Rule 8)."""

import pytest

from meme_intelligence.core.errors import CollectorError


async def test_missing_getbalance_value_raises_instead_of_reporting_zero():
    """Bug hunt 2026-07-29: returning 0 told the operator "wallet holds 0.0000
    SOL" for a funded wallet whenever the RPC hiccuped, and made
    get_spendable_balance_sol return 0.0 instead of the None its own docstring
    promises."""
    from meme_intelligence.trading.solana_rpc import SolanaRpcClient

    client = SolanaRpcClient.__new__(SolanaRpcClient)
    client.name = "solana_rpc"

    async def no_value(method, params):
        return {}          # a 200 with no "value" key = the read failed

    client._rpc = no_value
    with pytest.raises(CollectorError):
        await client.get_sol_balance_lamports("SomeOwnerPubkey")


async def test_a_real_zero_balance_is_still_reported_as_zero():
    """An actually-empty wallet must keep working — this guard is about
    UNREADABLE, not empty."""
    from meme_intelligence.trading.solana_rpc import SolanaRpcClient

    client = SolanaRpcClient.__new__(SolanaRpcClient)
    client.name = "solana_rpc"

    async def zero(method, params):
        return {"value": 0}

    client._rpc = zero
    assert await client.get_sol_balance_lamports("SomeOwnerPubkey") == 0
