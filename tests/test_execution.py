"""Tests for live trade execution (Project 6) — buy/dump signing + guards.

Uses a throwaway solders keypair and fully mocked Jupiter + RPC; no network,
no real funds. Verifies the caps, balance checks, route/failure handling, and
that the sign->send->confirm path runs end to end.
"""

import base64

import pytest

from meme_intelligence.database.storage import Storage
from meme_intelligence.trading.execution import (
    DryRunExecutor,
    LiveExecutor,
    TradeIntent,
)

from datetime import datetime, timezone

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
MINT = "MemeMint1111111111111111111111111111111111"
LAMPORTS = 1_000_000_000


def new_keypair():
    from solders.keypair import Keypair
    return Keypair()


def keypair_b58(kp) -> str:
    return str(kp)  # solders Keypair __str__ is the base58 secret


def swap_tx_b64(kp) -> str:
    """A real (minimal) VersionedTransaction the executor can deserialize+sign."""
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction

    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    tx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(tx)).decode()


class FakeJupiter:
    def __init__(self, quote, swap_b64):
        self.quote = quote
        self.swap_b64 = swap_b64
        self.calls = []

    async def get_quote(self, input_mint, output_mint, amount, slippage_bps):
        self.calls.append(("quote", input_mint, output_mint, amount))
        return self.quote

    async def build_swap_transaction(self, quote, user_public_key, *,
                                     priority_fee_max_lamports=0):
        self.calls.append(("build", user_public_key))
        return self.swap_b64


class FakeRpc:
    def __init__(self, *, sol=0, token=0, sig="SIG123", status="confirmed"):
        self.sol = sol
        self.token = token
        self.sig = sig
        self.status = status
        self.sent = []

    async def get_sol_balance_lamports(self, owner):
        return self.sol

    async def get_token_balance_raw(self, owner, mint):
        return self.token

    async def send_raw_transaction(self, signed_base64):
        self.sent.append(signed_base64)
        return self.sig

    async def signature_status(self, signature):
        if self.status is None:
            return None
        if self.status == "err":
            return {"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": None}
        return {"err": None, "confirmationStatus": self.status}


def make_live(storage, kp, *, jupiter, rpc, max_buy_sol=0.15):
    async def no_sleep(_):
        return None

    return LiveExecutor(
        storage, jupiter_client=jupiter, rpc_client=rpc,
        private_key_base58=keypair_b58(kp),
        max_buy_sol=max_buy_sol, slippage_bps=500,
        confirm_timeout_seconds=45.0,
        sleep_func=no_sleep, time_func=iter([0.0, 1.0, 2.0, 100.0]).__next__,
    )


def intent(sol):
    return TradeIntent(token_address=MINT, chain="solana", sol_amount=sol,
                       requested_at=NOW, source="telegram")


async def test_buy_happy_path_signs_sends_and_confirms():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000", "routePlan": []}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, sig="BUYSIG", status="confirmed")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "BUY confirmed" in msg and "BUYSIG" in msg
        assert "solscan.io/tx/BUYSIG" in msg
        assert len(rpc.sent) == 1                       # exactly one tx submitted
        journal = storage.journal_entries(limit=5)
        assert journal and journal[0]["kind"] == "trade_buy"


async def test_buy_refused_over_per_trade_cap():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "1"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=100 * LAMPORTS)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc, max_buy_sol=0.15)
        msg = await ex.execute_buy(intent(0.5))
        assert "exceeds the per-trade cap" in msg
        assert rpc.sent == [] and jup.calls == []      # nothing quoted or sent


async def test_buy_refused_insufficient_balance():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "1"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=1_000_000)  # 0.001 SOL, far below a 0.1 buy + fees
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "not enough" in msg
        assert rpc.sent == []


async def test_buy_no_route_spends_nothing():
    kp = new_keypair()
    jup = FakeJupiter(quote=None, swap_b64=swap_tx_b64(kp))  # Jupiter: no route
    rpc = FakeRpc(sol=5 * LAMPORTS)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "No route to buy" in msg
        assert rpc.sent == []                            # never built or sent a tx


async def test_buy_onchain_failure_is_reported_not_raised():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, status="err")        # tx lands with an error
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "Buy failed" in msg


async def test_buy_confirmation_timeout_reports_pending():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, status=None)         # never confirms
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "confirmation pending" in msg
        assert len(rpc.sent) == 1                        # it WAS submitted


async def test_dump_happy_path():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": str(2 * LAMPORTS)}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(token=750_000, sig="DUMPSIG", status="finalized")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_sell_all(MINT)
        assert "DUMP confirmed" in msg and "DUMPSIG" in msg
        # It sold the FULL token balance.
        assert ("quote", MINT, __import__("meme_intelligence.collectors.jupiter_data",
                fromlist=["SOL_MINT"]).SOL_MINT, 750_000) in jup.calls


async def test_dump_nothing_to_sell():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "1"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(token=0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_sell_all(MINT)
        assert "Nothing to dump" in msg
        assert rpc.sent == []


def test_bad_private_key_rejected():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        with pytest.raises(ValueError, match="valid base58"):
            LiveExecutor(storage, jupiter_client=None, rpc_client=None,
                         private_key_base58="not-a-real-key", max_buy_sol=0.1)


async def test_dry_run_executor_signs_nothing():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = DryRunExecutor(storage)
        buy = await ex.execute_buy(intent(0.1))
        dump = await ex.execute_sell_all(MINT)
        assert "DRY RUN" in buy and "DRY RUN" in dump
        assert ex.live is False
        assert len(storage.journal_entries(limit=5)) == 2   # both intents journaled


# ---- SolanaRpcClient parsing ----

def make_rpc_client(monkeypatch, responses):
    from meme_intelligence.core.rate_limiter import RateLimiter
    from meme_intelligence.trading.solana_rpc import SolanaRpcClient

    client = SolanaRpcClient("helius-key", rate_limiter=RateLimiter(100.0, burst=10))
    seq = list(responses)

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None, error_status_as_json=frozenset()):
        return {"jsonrpc": "2.0", "id": 1, "result": seq.pop(0)}

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    return client


async def test_rpc_reads_sol_and_token_balances(monkeypatch):
    client = make_rpc_client(monkeypatch, [
        {"value": 2_500_000_000},
        {"value": [
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "400"}}}}}},
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "350"}}}}}},
        ]},
    ])
    assert await client.get_sol_balance_lamports("owner") == 2_500_000_000
    assert await client.get_token_balance_raw("owner", MINT) == 750  # summed across accounts


async def test_rpc_send_returns_signature_and_status(monkeypatch):
    client = make_rpc_client(monkeypatch, [
        "SIGXYZ",
        {"value": [{"err": None, "confirmationStatus": "confirmed"}]},
    ])
    assert await client.send_raw_transaction("b64tx") == "SIGXYZ"
    status = await client.signature_status("SIGXYZ")
    assert status["confirmationStatus"] == "confirmed"


async def test_rpc_send_with_no_signature_raises(monkeypatch):
    from meme_intelligence.core.errors import CollectorError

    client = make_rpc_client(monkeypatch, [None])
    with pytest.raises(CollectorError):
        await client.send_raw_transaction("b64tx")
