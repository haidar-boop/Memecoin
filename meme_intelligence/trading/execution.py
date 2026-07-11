"""Operator-initiated buy/dump execution (Project 6).

Two executors behind one interface (``execute_buy`` / ``execute_sell_all``):

* :class:`DryRunExecutor` — signs nothing; journals the intent and says so.
  Used whenever live trading is off or no trading key is configured.
* :class:`LiveExecutor` — builds a Jupiter swap, signs it with the dedicated
  trading wallet's key, submits it via the operator's Solana RPC, and waits
  for on-chain confirmation. Buys and dumps (sell-100%) only; NEVER auto —
  every trade is a button the operator tapped.

SAFETY MODEL (see DECISIONS_LOG 2026-07-10, Project 6):

* The system never auto-trades. There is no code path that initiates a trade
  without an explicit operator action.
* The signing key belongs to a DEDICATED, low-balance wallet — never the
  operator's main wallet — and is read only from an environment variable,
  never logged (Rule 16). The real hard cap is how little it is funded with.
* Every buy is capped per-trade (``max_buy_sol``) and re-checked against the
  live wallet balance; the wallet can never spend SOL it does not hold.
* ``LiveExecutor`` is only constructed when ``execution.live_enabled`` is on
  AND a key is present; otherwise the dry-run executor is used.
"""

from __future__ import annotations

import asyncio
import base64
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from meme_intelligence.collectors.jupiter_data import SOL_MINT
from meme_intelligence.core.errors import CollectorError, MemeIntelError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity

_LAMPORTS_PER_SOL = 1_000_000_000
# Leave headroom for the transaction fee + priority fee + temporary wSOL rent
# so a "buy all my SOL" never fails for lack of fee money.
_FEE_BUFFER_LAMPORTS = 7_000_000  # ~0.007 SOL
_SOLSCAN_TX = "https://solscan.io/tx/"


class TradeError(MemeIntelError):
    """A live trade could not be completed (surfaced to the operator, not raised on)."""


@dataclass(frozen=True)
class TradeIntent:
    """One operator-initiated buy intent (never an order)."""

    token_address: str
    chain: str
    sol_amount: float
    requested_at: datetime
    source: str = "telegram"


class DryRunExecutor:
    """Records buy/dump intents and answers honestly that nothing executed.

    Any storage failure is logged and swallowed — a journaling hiccup must not
    turn a no-op dry run into an error in the operator's face (Rule 7).
    """

    live = False

    def __init__(self, storage) -> None:
        self._storage = storage
        self._logger = get_logger("trading.execution")

    async def execute_buy(self, intent: TradeIntent) -> str:
        message = (
            f"DRY RUN — no real trade executed. Would buy {intent.sol_amount:g} SOL "
            f"of {intent.token_address} on {intent.chain}. Live execution is off "
            "(MEMEINTEL_EXECUTION_LIVE_ENABLED=false or no trading key set)."
        )
        self._journal(intent.token_address, intent.chain, "trade_intent",
                      f"dry-run buy intent via {intent.source}: {intent.sol_amount:g} SOL "
                      f"at {intent.requested_at.isoformat()}")
        return message

    async def execute_sell_all(self, mint: str, chain: str = "solana") -> str:
        self._journal(mint, chain, "trade_intent", f"dry-run dump intent for {mint}")
        return (f"DRY RUN — no real trade executed. Would dump the full {mint} "
                "position. Live execution is off.")

    def _journal(self, address: str, chain: str, kind: str, content: str) -> None:
        try:
            self._storage.add_journal(TokenIdentity(chain=chain, address=address), kind, content)
            self._logger.info("dry-run %s recorded for %s", kind, address)
        except Exception as exc:  # noqa: BLE001 — journaling must not break the reply
            self._logger.warning("failed to journal %s for %s: %s", kind, address, exc)


class LiveExecutor:
    """Signs and submits real Solana swaps for operator buy/dump (Project 6)."""

    live = True

    def __init__(
        self,
        storage,
        *,
        jupiter_client,
        rpc_client,
        private_key_base58: str,
        max_buy_sol: float,
        slippage_bps: int = 500,
        priority_fee_max_lamports: int = 1_000_000,
        confirm_timeout_seconds: float = 45.0,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if not private_key_base58:
            raise ValueError("LiveExecutor requires the trading wallet private key")
        # Lazy import so the rest of the system does not depend on solders.
        from solders.keypair import Keypair

        try:
            self._keypair = Keypair.from_base58_string(private_key_base58)
        except Exception as exc:  # noqa: BLE001 — bad key -> clear config error, no secret leak
            raise ValueError("MEMEINTEL_EXECUTION_PRIVATE_KEY is not a valid "
                             "base58 Solana private key") from exc
        self._pubkey = str(self._keypair.pubkey())
        self._storage = storage
        self._jupiter = jupiter_client
        self._rpc = rpc_client
        self._max_buy_sol = max_buy_sol
        self._slippage_bps = slippage_bps
        self._priority_fee_max = priority_fee_max_lamports
        self._confirm_timeout = confirm_timeout_seconds
        self._sleep = sleep_func
        self._time = time_func
        self._lock = asyncio.Lock()  # one trade at a time per wallet
        self._logger = get_logger("trading.execution")

    @property
    def wallet_address(self) -> str:
        return self._pubkey

    async def execute_buy(self, intent: TradeIntent) -> str:
        if intent.chain not in ("solana", "sol"):
            return "Live trading is Solana-only."
        # Reject nan/inf explicitly: `nan <= 0` and `nan > cap` are BOTH
        # False, so a non-finite amount would slip past both guards into
        # int(nan * ...) and raise (bug-hunt finding, 2026-07-11). This is the
        # money gate — it must not rely on the caller having validated.
        if not math.isfinite(intent.sol_amount) or intent.sol_amount <= 0:
            return "Buy amount must be positive."
        if intent.sol_amount > self._max_buy_sol:
            return (f"Refused: {intent.sol_amount:g} SOL exceeds the per-trade cap of "
                    f"{self._max_buy_sol:g} SOL (MEMEINTEL_EXECUTION_MAX_BUY_SOL).")
        lamports = int(intent.sol_amount * _LAMPORTS_PER_SOL)
        async with self._lock:
            # These run BEFORE any transaction is broadcast, so a failure here
            # means nothing was spent and it is safe to say so.
            try:
                balance = await self._rpc.get_sol_balance_lamports(self._pubkey)
            except CollectorError as exc:
                return ("Buy aborted — could not read the wallet balance "
                        f"({self._safe(str(exc))}). Nothing was spent.")
            if balance < lamports + _FEE_BUFFER_LAMPORTS:
                return (f"Refused: wallet holds {balance / _LAMPORTS_PER_SOL:.4f} SOL, "
                        f"not enough for {intent.sol_amount:g} SOL + fees. Fund the "
                        "trading wallet or lower the amount.")
            try:
                quote = await self._jupiter.get_quote(
                    SOL_MINT, intent.token_address, lamports, self._slippage_bps,
                    use_cache=False)
            except CollectorError as exc:
                return ("Buy aborted — could not get a fresh quote "
                        f"({self._safe(str(exc))}). Nothing was spent.")
            if quote is None:
                return ("No route to buy this token right now (Jupiter found no "
                        "swap path). Nothing was spent.")
            return await self._execute_swap(
                quote, mint=intent.token_address, kind="trade_buy", action="BUY",
                detail=f"{intent.sol_amount:g} SOL via {intent.source}")

    async def execute_sell_all(self, mint: str, chain: str = "solana") -> str:
        if chain not in ("solana", "sol"):
            return "Live trading is Solana-only."
        async with self._lock:
            try:
                raw = await self._rpc.get_token_balance_raw(self._pubkey, mint)
            except CollectorError as exc:
                return ("Dump aborted — could not read the token balance "
                        f"({self._safe(str(exc))}). Nothing was sold.")
            if raw <= 0:
                return "Nothing to dump — the trading wallet holds none of this token."
            try:
                quote = await self._jupiter.get_quote(
                    mint, SOL_MINT, raw, self._slippage_bps, use_cache=False)
            except CollectorError as exc:
                return ("Dump aborted — could not get a fresh quote "
                        f"({self._safe(str(exc))}). Nothing was sold.")
            if quote is None:
                return ("No route to sell this token right now (Jupiter found no "
                        "swap path). Nothing was sold — try again shortly.")
            return await self._execute_swap(
                quote, mint=mint, kind="trade_sell", action="DUMP",
                detail="100% of position")

    # ---- internals ----

    async def _execute_swap(self, quote: dict, *, mint: str, kind: str,
                            action: str, detail: str) -> str:
        """Build -> sign -> send -> confirm with money-safe staged reporting.

        The single rule: once a transaction has been BROADCAST (send returned a
        signature), never report an outright failure that hides the signature —
        the operator must verify on-chain, not blindly re-tap and double-spend.
        Only pre-broadcast failures (build/sign, and a clean submission
        rejection) are reported as 'nothing spent'.
        """
        # Pre-broadcast: build + sign. A failure here spent nothing.
        try:
            swap_b64 = await self._jupiter.build_swap_transaction(
                quote, self._pubkey,
                priority_fee_max_lamports=self._priority_fee_max,
                max_slippage_bps=self._slippage_bps)
            signed = self._sign(swap_b64)
        except (CollectorError, TradeError) as exc:
            return f"{action} failed before sending — nothing was spent: {self._safe(str(exc))}"

        # Submission. If this raises we cannot be certain the tx did NOT reach
        # the network, so we must NOT invite a blind retry.
        try:
            signature = await self._rpc.send_raw_transaction(signed)
        except CollectorError as exc:
            self._logger.error("%s submission error for %s: %s", action, mint,
                               self._safe(str(exc)))
            return (f"{action} may not have gone through (submission error). Do NOT retry "
                    f"blindly — check your wallet / Solscan first: {self._safe(str(exc))}")

        # Broadcast: the signature is now the source of truth. Journal it
        # immediately so a confirmation hiccup can never lose the record.
        link = f"{_SOLSCAN_TX}{signature}"
        self._journal(mint, kind, f"live {kind} {detail}: {signature}")
        try:
            landed = await self._confirm(signature)
        except asyncio.CancelledError:
            # Shutdown/cancellation during the confirm poll. The tx is already
            # broadcast and journaled above; log the signature at ERROR so it
            # survives in the journalctl record even though the operator's
            # reply can't be delivered, then propagate. The operator verifies
            # on Solscan and must NOT blindly re-tap (bug-hunt finding).
            self._logger.error(
                "%s for %s was broadcast but confirmation was cancelled "
                "(shutdown) — verify on-chain, do NOT re-tap: %s",
                action, mint, link)
            raise
        except TradeError as exc:
            # Confirmed on-chain FAILURE: the tx reverted, so no funds moved
            # beyond the network fee — safe to retry.
            return (f"{action} did NOT go through on-chain — only the network fee was spent, "
                    f"safe to retry. ({self._safe(str(exc))})\n{link}")
        except CollectorError as exc:
            # The RPC could not tell us the outcome. UNKNOWN — do not retry.
            self._logger.warning("%s confirmation unknown for %s: %s", action, mint,
                                 self._safe(str(exc)))
            return (f"{action} was submitted but could not be confirmed (RPC error). Do NOT "
                    f"retry — check Solscan first.\n{link}")
        if landed:
            return f"{action} confirmed.\n{link}"
        return (f"{action} submitted — confirmation still pending. Do NOT retry; "
                f"check Solscan.\n{link}")

    def _sign(self, swap_tx_base64: str) -> str:
        """Sign the Jupiter VersionedTransaction with the trading key."""
        from solders.transaction import VersionedTransaction

        try:
            raw = base64.b64decode(swap_tx_base64)
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [self._keypair])
            return base64.b64encode(bytes(signed)).decode("ascii")
        except Exception as exc:  # noqa: BLE001 — malformed tx must not leak internals
            raise TradeError("could not sign the swap transaction") from exc

    async def _confirm(self, signature: str) -> bool:
        """Poll until the signature confirms/finalizes; True if it landed.

        A timeout returns False (the tx may still land — reported as pending),
        an on-chain error raises TradeError so the operator sees the failure.
        """
        deadline = self._time() + self._confirm_timeout
        while self._time() < deadline:
            status = await self._rpc.signature_status(signature)
            if status is not None:
                if status.get("err") is not None:
                    raise TradeError(f"transaction failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await self._sleep(2.0)
        return False

    def _safe(self, text: str) -> str:
        """Never let the private key or wallet internals surface in a reply."""
        return text.replace(self._pubkey, self._pubkey[:4] + "…")

    def _journal(self, address: str, kind: str, content: str) -> None:
        try:
            self._storage.add_journal(
                TokenIdentity(chain="solana", address=address), kind, content)
        except Exception as exc:  # noqa: BLE001 — journaling must not break the reply
            self._logger.warning("failed to journal %s for %s: %s", kind, address, exc)
