"""Tests for live trade execution (Project 6) — buy/dump signing + guards.

Uses a throwaway solders keypair and fully mocked Jupiter + RPC; no network,
no real funds. Verifies the caps, balance checks, route/failure handling, and
that the sign->send->confirm path runs end to end.
"""

import asyncio
import base64
import math

import pytest

from meme_intelligence.collectors.jupiter_data import SOL_MINT
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

    async def get_quote(self, input_mint, output_mint, amount, slippage_bps, *, use_cache=True):
        self.calls.append(("quote", input_mint, output_mint, amount, use_cache))
        return self.quote

    async def build_swap_transaction(self, quote, user_public_key, *,
                                     priority_fee_max_lamports=0, max_slippage_bps=500):
        self.calls.append(("build", user_public_key, max_slippage_bps))
        return self.swap_b64


class FakeRpc:
    def __init__(self, *, sol=0, token=0, sig="SIG123", status="confirmed",
                 send_raises=False, confirm_raises=False):
        self.sol = sol
        self.token = token
        self.sig = sig
        self.status = status
        self.send_raises = send_raises
        self.confirm_raises = confirm_raises
        self.sent = []

    async def get_sol_balance_lamports(self, owner):
        from meme_intelligence.core.errors import CollectorError
        if self.sol == "raise":
            raise CollectorError("balance rpc down")
        return self.sol

    async def get_token_balance_raw(self, owner, mint):
        return self.token

    async def send_raw_transaction(self, signed_base64):
        from meme_intelligence.core.errors import CollectorError
        if self.send_raises:
            raise CollectorError("send rpc error")
        self.sent.append(signed_base64)
        return self.sig

    async def signature_status(self, signature):
        from meme_intelligence.core.errors import CollectorError
        if self.confirm_raises:
            raise CollectorError("confirm rpc error")
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


async def test_buy_onchain_revert_is_reported_as_safe_to_retry():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, status="err")        # tx lands but reverts
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "did NOT go through on-chain" in msg and "safe to retry" in msg


async def test_buy_confirmation_timeout_reports_pending_with_signature():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, sig="PENDSIG", status=None)  # never confirms
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "pending" in msg and "Do NOT retry" in msg
        assert "PENDSIG" in msg                          # signature never hidden
        assert len(rpc.sent) == 1                        # it WAS submitted


async def test_confirm_phase_rpc_error_never_hides_the_signature():
    """The double-spend fix: a broadcast tx whose confirmation RPC errors must
    be reported as 'submitted, unknown — do NOT retry' WITH its signature, not
    as an outright failure that invites a second buy."""
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, sig="LANDEDSIG", confirm_raises=True)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "could not be confirmed" in msg and "Do NOT" in msg
        assert "LANDEDSIG" in msg                        # signature surfaced, not hidden
        assert "failed" not in msg.lower()               # never labelled an outright failure
        assert len(rpc.sent) == 1
        # The broadcast tx is journaled even though confirmation failed.
        journal = storage.journal_entries(limit=5)
        assert journal and journal[0]["kind"] == "trade_buy"


async def test_submission_error_warns_it_may_have_gone_through():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS, send_raises=True)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "may not have gone through" in msg and "Do NOT retry blindly" in msg


async def test_live_trade_bounds_slippage_to_configured_cap():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=5 * LAMPORTS)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        await ex.execute_buy(intent(0.1))
        # The quote is fetched FRESH (use_cache=False) and the swap is capped at
        # the operator's slippage (500 bps here) — no unbounded dynamic slippage.
        assert ("quote", SOL_MINT, MINT, 100_000_000, False) in jup.calls
        build_calls = [c for c in jup.calls if c[0] == "build"]
        assert build_calls and build_calls[0][2] == 500  # max_slippage_bps passed


async def test_dump_happy_path():
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": str(2 * LAMPORTS)}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(token=750_000, sig="DUMPSIG", status="finalized")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_sell_all(MINT)
        assert "DUMP confirmed" in msg and "DUMPSIG" in msg
        # It sold the FULL token balance, from a fresh (uncached) quote.
        assert ("quote", MINT, SOL_MINT, 750_000, False) in jup.calls


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


# ---- Dedicated trading Helius key (build_executor wiring) ----

def _executor_env(kp, **extra):
    env = {
        "MEMEINTEL_EXECUTION_LIVE_ENABLED": "true",
        "MEMEINTEL_EXECUTION_PRIVATE_KEY": keypair_b58(kp),
        "MEMEINTEL_HELIUS_API_KEY": "scanner-key",
    }
    env.update(extra)
    return env


def test_build_executor_uses_dedicated_trading_helius_key():
    """MEMEINTEL_EXECUTION_HELIUS_API_KEY gives trading its OWN account +
    its OWN rate limiter, so the scanner's exhausted budget can't 429 a
    trade's balance read (observed live 2026-07-11)."""
    from meme_intelligence.__main__ import build_executor
    from meme_intelligence.config.settings import Settings
    from meme_intelligence.core.rate_limiter import RateLimiter

    kp = new_keypair()
    settings = Settings.from_env(env=_executor_env(
        kp, MEMEINTEL_EXECUTION_HELIUS_API_KEY="trading-key"))
    assert settings.trading_helius_api_key == "trading-key"
    shared = RateLimiter.per_minute(120.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        executor, rpc = build_executor(
            settings, storage, jupiter_client=object(), helius_rate_limiter=shared)
        assert executor.live is True
        assert rpc is not None
        assert rpc._api_key == "trading-key"          # the second account's key
        assert rpc._rate_limiter is not shared        # and its own budget


def test_build_executor_without_dedicated_key_shares_the_scanner_limiter():
    """No dedicated key -> same account as the scanner -> one shared bucket
    (the earlier fix for two independent limiters exceeding one budget)."""
    from meme_intelligence.__main__ import build_executor
    from meme_intelligence.config.settings import Settings
    from meme_intelligence.core.rate_limiter import RateLimiter

    kp = new_keypair()
    settings = Settings.from_env(env=_executor_env(kp))
    shared = RateLimiter.per_minute(120.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        executor, rpc = build_executor(
            settings, storage, jupiter_client=object(), helius_rate_limiter=shared)
        assert executor.live is True
        assert rpc._api_key == "scanner-key"
        assert rpc._rate_limiter is shared


def test_build_executor_dedicated_key_alone_is_enough():
    """A trading-only Helius key with NO scanner key still arms live trading."""
    from meme_intelligence.__main__ import build_executor
    from meme_intelligence.config.settings import Settings

    kp = new_keypair()
    env = _executor_env(kp, MEMEINTEL_EXECUTION_HELIUS_API_KEY="trading-key")
    env["MEMEINTEL_HELIUS_API_KEY"] = ""
    settings = Settings.from_env(env=env)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        executor, rpc = build_executor(settings, storage, jupiter_client=object())
        assert executor.live is True
        assert rpc._api_key == "trading-key"


# ---- Bug-hunt fixes (2026-07-11) ----

async def test_buy_refused_non_finite_amount_at_the_money_gate():
    """nan/inf must be refused by the executor itself (the money gate), not
    just the command layer: `nan <= 0` and `nan > cap` are both False, so an
    unguarded non-finite amount would crash in int(nan * ...)."""
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "1"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol=100 * LAMPORTS)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        for bad in (float("nan"), float("inf"), float("-inf")):
            msg = await ex.execute_buy(intent(bad))
            assert "must be positive" in msg
        assert rpc.sent == [] and jup.calls == []   # never quoted or broadcast


async def test_getbalance_non_numeric_value_raises_collector_error(monkeypatch):
    """A malformed getBalance value must fail as CollectorError (routing to the
    clean 'Nothing was spent' abort), not raise a raw TypeError."""
    from meme_intelligence.core.errors import CollectorError

    client = make_rpc_client(monkeypatch, [{"value": {"unexpected": "shape"}}])
    with pytest.raises(CollectorError):
        await client.get_sol_balance_lamports("owner")


async def test_buy_aborts_cleanly_when_balance_unreadable():
    """End-to-end: an unreadable balance aborts pre-broadcast with the safe
    'Nothing was spent' message and never quotes or sends."""
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "1"}, swap_b64=swap_tx_b64(kp))
    rpc = FakeRpc(sol="raise")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        msg = await ex.execute_buy(intent(0.1))
        assert "could not read the wallet balance" in msg
        assert "Nothing was spent" in msg
        assert rpc.sent == [] and jup.calls == []


async def test_confirmation_cancellation_journals_signature_and_reraises():
    """If confirmation is cancelled (shutdown) after broadcast, the signature
    must already be journaled (never lost) and CancelledError propagates."""
    kp = new_keypair()
    jup = FakeJupiter(quote={"outAmount": "500000", "routePlan": []},
                      swap_b64=swap_tx_b64(kp))

    class CancelDuringConfirmRpc(FakeRpc):
        async def signature_status(self, signature):
            raise asyncio.CancelledError()

    rpc = CancelDuringConfirmRpc(sol=5 * LAMPORTS, sig="CANCELSIG")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        ex = make_live(storage, kp, jupiter=jup, rpc=rpc)
        with pytest.raises(asyncio.CancelledError):
            await ex.execute_buy(intent(0.1))
        # The tx was broadcast and journaled BEFORE the confirm poll, so the
        # signature survives even though no reply could be delivered.
        journal = storage.journal_entries(limit=5)
        assert journal and "CANCELSIG" in journal[0]["content"]
        assert len(rpc.sent) == 1   # broadcast happened exactly once
