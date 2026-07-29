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
* Every buy is optionally capped per-trade (``max_buy_sol``, 0 = no ceiling —
  operator request, 2026-07-20) and re-checked against the live wallet
  balance; the wallet can never spend SOL it does not hold, cap or no cap.
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
from meme_intelligence.trading.solana_rpc import TransactionRejectedError

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

    async def get_spendable_balance_sol(self) -> float | None:
        """No real wallet exists in dry-run — an honest unknown, never a
        fabricated number (Rule 8). Percentage buy buttons report this as
        'balance unavailable' rather than guessing."""
        return None

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
        preflight_retries: int = 2,
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
        self._preflight_retries = max(0, int(preflight_retries))
        self._sleep = sleep_func
        self._time = time_func
        self._lock = asyncio.Lock()  # one trade at a time per wallet
        self._logger = get_logger("trading.execution")

    @property
    def wallet_address(self) -> str:
        return self._pubkey

    async def get_spendable_balance_sol(self) -> float | None:
        """Live wallet balance minus the fee/rent buffer ``execute_buy``
        always reserves, so a percentage-of-balance button's 100% tap sizes
        to an amount that can actually clear the balance re-check inside
        ``execute_buy`` instead of being refused for lacking fee money
        (Project 6 button rebuild, 2026-07-18). ``None`` on a read failure —
        an unreadable balance must never be treated as zero or as unlimited
        (Rule 8); callers refuse the button rather than guess.
        """
        try:
            lamports = await self._rpc.get_sol_balance_lamports(self._pubkey)
        except CollectorError:
            return None
        return max(0, lamports - _FEE_BUFFER_LAMPORTS) / _LAMPORTS_PER_SOL

    async def execute_buy(self, intent: TradeIntent) -> str:
        if intent.chain not in ("solana", "sol"):
            return "Live trading is Solana-only."
        # Reject nan/inf explicitly: `nan <= 0` and `nan > cap` are BOTH
        # False, so a non-finite amount would slip past both guards into
        # int(nan * ...) and raise (bug-hunt finding, 2026-07-11). This is the
        # money gate — it must not rely on the caller having validated.
        if not math.isfinite(intent.sol_amount) or intent.sol_amount <= 0:
            return "Buy amount must be positive."
        # 0 = no ceiling (operator request, 2026-07-20): the live balance
        # re-check just below remains the only automatic limit.
        if self._max_buy_sol > 0 and intent.sol_amount > self._max_buy_sol:
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
            return await self._quote_and_swap(
                input_mint=SOL_MINT, output_mint=intent.token_address,
                amount=lamports, mint=intent.token_address, kind="trade_buy",
                action="BUY", detail=f"{intent.sol_amount:g} SOL via {intent.source}",
                no_route_msg=("No route to buy this token right now (Jupiter found "
                              "no swap path). Nothing was spent."),
                spent_noun="spent")

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
            return await self._quote_and_swap(
                input_mint=mint, output_mint=SOL_MINT, amount=raw, mint=mint,
                kind="trade_sell", action="DUMP", detail="100% of position",
                no_route_msg=("No route to sell this token right now (Jupiter found "
                              "no swap path). Nothing was sold — try again shortly."),
                spent_noun="sold")

    # ---- internals ----

    async def _quote_and_swap(self, *, input_mint: str, output_mint: str,
                              amount: int, mint: str, kind: str, action: str,
                              detail: str, no_route_msg: str,
                              spent_noun: str) -> str:
        """Quote fresh, then swap — retrying with a NEW quote when the network
        definitively rejects the transaction pre-broadcast.

        A :class:`TransactionRejectedError` means the node's preflight
        simulation refused the transaction and never forwarded it — nothing
        was spent, so retrying is safe and is not a second trade: it is the
        SAME operator-initiated intent, re-quoted at the current price. This
        is how a fast-moving meme coin gets caught: the first quote is stale
        by the time the transaction lands, so slippage trips; a fresh quote
        re-centers the slippage allowance on the price as it is NOW
        (operator-reported failure, 2026-07-19). Ambiguous submission errors
        (the tx MAY have reached the network) are never retried — that path
        still reports 'check Solscan, do NOT retry blindly'.
        """
        attempts = self._preflight_retries + 1
        reason = ""
        for attempt in range(1, attempts + 1):
            try:
                quote = await self._jupiter.get_quote(
                    input_mint, output_mint, amount, self._slippage_bps,
                    use_cache=False)
            except CollectorError as exc:
                return (f"{action} aborted — could not get a fresh quote "
                        f"({self._safe(str(exc))}). Nothing was {spent_noun}.")
            if quote is None:
                return no_route_msg
            try:
                return await self._execute_swap(
                    quote, mint=mint, kind=kind, action=action, detail=detail)
            except TransactionRejectedError as exc:
                reason = self._safe(str(exc))
                self._logger.warning(
                    "%s attempt %d/%d rejected pre-broadcast for %s: %s",
                    action, attempt, attempts, mint, reason)
        return (f"{action} failed — nothing was {spent_noun}. The network rejected "
                f"it before sending, {attempts} times with fresh quotes: {reason}. "
                f"The coin is likely moving faster than your "
                f"{self._slippage_bps / 100:g}% slippage allowance — try again, or "
                "raise MEMEINTEL_EXECUTION_SLIPPAGE_BPS in .env.")

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
            # The signature is DETERMINISTIC from the signed bytes (it matches
            # what the RPC returns), so we can record it even if the send await
            # is torn by a shutdown cancel mid-flight — see the CancelledError
            # guard below (bug-hunt finding, 2026-07-12).
            expected_sig = self._signature_of(signed)
        except (CollectorError, TradeError) as exc:
            return f"{action} failed before sending — nothing was spent: {self._safe(str(exc))}"

        # Submission. If this raises we cannot be certain the tx did NOT reach
        # the network, so we must NOT invite a blind retry.
        try:
            signature = await self._rpc.send_raw_transaction(signed)
        except asyncio.CancelledError:
            # A graceful-shutdown cancel can land while the sendTransaction
            # request body is already on the wire — the tx may be broadcast
            # with no return value. Journal + log the derived signature so it
            # is never lost, then propagate. The operator verifies on Solscan
            # and must NOT blindly re-tap (mirrors the confirm-stage guard;
            # the send window previously lacked it — bug-hunt finding).
            self._journal(mint, kind,
                          f"live {kind} {detail}: {expected_sig} "
                          "(send cancelled mid-flight — may be broadcast, verify on-chain)")
            self._logger.error(
                "%s for %s cancelled during send — may already be broadcast; "
                "verify on-chain, do NOT re-tap: %s%s",
                action, mint, _SOLSCAN_TX, expected_sig)
            raise
        except TransactionRejectedError:
            # Preflight simulation definitively refused it — the node never
            # broadcast the tx, nothing was spent. Propagate so the caller's
            # retry loop can re-quote at the current price (2026-07-19).
            raise
        except CollectorError as exc:
            self._logger.error("%s submission error for %s: %s", action, mint,
                               self._safe(str(exc)))
            return (f"{action} may not have gone through (submission error). Do NOT retry "
                    f"blindly — check your wallet / Solscan first: {self._safe(str(exc))}")

        # Broadcast: the signature is now the source of truth. Journal it
        # immediately so a confirmation hiccup can never lose the record.
        link = f"{_SOLSCAN_TX}{signature}"
        self._journal(mint, kind, f"live {kind} {detail}: {signature}")
        # Register the position the moment the BUY is broadcast, not when it
        # confirms. `set_holding` was previously only ever called by /holding,
        # so a coin bought through /buy or an alert's Buy button was never
        # marked as held — and the live rug watch, the interest gate and the
        # protective-alert priority all key off holdings. The bot could buy a
        # coin and then never guard it (2026-07-29).
        #
        # Broadcast rather than confirmed is deliberate: a buy whose
        # confirmation times out has very likely landed, and that is precisely
        # the coin most in need of watching. The failure directions are not
        # symmetric — watching a coin we do not own is harmless (a sell simply
        # reports "nothing to dump"), while failing to watch one we do own is
        # what costs money.
        if kind == "trade_buy":
            self._remember_position(mint, held=True)
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
            # A CONFIRMED sell of the whole position ends the holding. Only on
            # `landed` — an unconfirmed sell may not have gone through, and
            # keeping the watch on a coin we might still own is the safe
            # direction (same asymmetry as the buy above).
            if kind == "trade_sell":
                self._remember_position(mint, held=False)
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

    def _signature_of(self, signed_tx_base64: str) -> str:
        """The transaction's fee-payer signature, derivable from the signed
        bytes without broadcasting — deterministic and identical to the string
        the RPC returns. Lets a broadcast tx be recorded even if the send await
        is cancelled mid-flight. Returns 'unknown' if it can't be derived
        (never blocks or fails the trade over this)."""
        from solders.transaction import VersionedTransaction

        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(signed_tx_base64))
            sigs = tx.signatures
            return str(sigs[0]) if sigs else "unknown"
        except Exception:  # noqa: BLE001 — best effort; a derive failure must not raise
            return "unknown"

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

    def _remember_position(self, mint: str, *, held: bool) -> None:
        """Mark/unmark a live position so the rug watch knows to guard it.

        Best-effort and fully isolated: a storage hiccup must never turn a
        completed trade into an error reply (Rule 7).
        """
        token = TokenIdentity(chain="solana", address=mint)
        try:
            if held:
                if self._storage.set_holding(token, note="auto: live buy"):
                    self._logger.info("registered live position in %s for the rug watch",
                                      mint)
            else:
                if self._storage.release_holding(token):
                    self._logger.info("released position in %s after a confirmed sell",
                                      mint)
        except Exception as exc:  # noqa: BLE001 — never break a completed trade
            self._logger.warning("could not update the holding record for %s: %s",
                                 mint, exc)

    def _journal(self, address: str, kind: str, content: str) -> None:
        try:
            self._storage.add_journal(
                TokenIdentity(chain="solana", address=address), kind, content)
        except Exception as exc:  # noqa: BLE001 — journaling must not break the reply
            self._logger.warning("failed to journal %s for %s: %s", kind, address, exc)
