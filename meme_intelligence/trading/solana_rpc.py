"""Minimal Solana JSON-RPC client for live trade execution (Project 6).

Just the calls the executor needs — submit a signed transaction, confirm it
landed, read the wallet's SOL balance, and read an SPL token balance — over
the operator's existing Helius RPC endpoint. Built on
:class:`~meme_intelligence.collectors.base.BaseCollector` for rate limiting,
retries, and timeout handling (Rules 6/7/11). Kept separate from the
holder-reading :class:`~meme_intelligence.collectors.wallet_data.HeliusClient`
so the trading path is its own module (Rule 4).
"""

from __future__ import annotations

from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError

# SPL token program (owner of every token account).
_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Jupiter aggregator custom program error 6001 — the swap's price moved past
# the slippage allowance between quote and landing. The single most common
# rejection on fresh meme pools (seen live 2026-07-19).
_SLIPPAGE_PROGRAM_ERROR = "0x1771"


class TransactionRejectedError(CollectorError):
    """sendTransaction's preflight simulation definitively REJECTED the
    transaction: the node never broadcast it, so nothing was spent and it is
    safe to rebuild (fresh quote) and retry. Distinct from an ambiguous
    submission error, where the transaction may have reached the network."""


class SolanaRpcClient(BaseCollector):
    """Thin JSON-RPC client over a Solana RPC endpoint (Helius)."""

    def __init__(self, api_key: str, *, rpc_url: str = "https://mainnet.helius-rpc.com",
                 **kwargs: Any) -> None:
        if not api_key:
            raise ValueError("SolanaRpcClient requires an API key")
        kwargs.setdefault("name", "solana_rpc")
        kwargs.setdefault("base_url", rpc_url)
        # The api key is a secret in the URL query — redact it from logs (Rule 16).
        kwargs.setdefault("redact", (api_key,))
        super().__init__(**kwargs)
        self._api_key = api_key

    async def _rpc(self, method: str, params: list) -> Any:
        payload = await self._get_json(
            f"/?api-key={self._api_key}",
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object from RPC")
        if "error" in payload:
            error = payload["error"]
            # Full detail (program logs and all) goes to the LOG only — the
            # raised message must stay short enough to read on a phone, never
            # the multi-KB 'data' dump (operator complaint, 2026-07-19).
            self._logger.warning("%s: RPC error on %s: %s", self.name, method, error)
            if method == "sendTransaction":
                rejection = self._describe_send_rejection(error)
                if rejection is not None:
                    raise TransactionRejectedError(f"{self.name}: {rejection}")
            raise CollectorError(
                f"{self.name}: RPC error on {method}: {self._brief_error(error)}")
        return payload.get("result")

    @staticmethod
    def _brief_error(error: Any) -> str:
        """Code + message only, truncated — never the 'data' field, which
        carries the full simulation log dump."""
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message", ""))[:200]
            return f"code {code}: {message}" if code is not None else message
        return str(error)[:200]

    @staticmethod
    def _describe_send_rejection(error: Any) -> str | None:
        """Short human explanation when preflight simulation definitively
        rejected the transaction (it was never broadcast), else ``None``.

        RPC code -32002 ("Transaction simulation failed") means the node
        simulated the transaction, it failed, and the node did NOT forward it
        to the network — nothing was spent, and retrying with a fresh quote
        is safe."""
        if not isinstance(error, dict):
            return None
        message = str(error.get("message", ""))
        if error.get("code") != -32002 and "simulation failed" not in message.lower():
            return None
        if _SLIPPAGE_PROGRAM_ERROR in message:
            return ("the price moved beyond the slippage allowance before the "
                    "trade could land (rejected pre-send, nothing was spent)")
        return (f"the network's pre-send check rejected it, nothing was spent "
                f"({message[:160]})")

    async def get_sol_balance_lamports(self, owner: str) -> int:
        """Wallet SOL balance in lamports."""
        result = await self._rpc("getBalance", [owner])
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            # A non-numeric balance means we cannot trust the read. Fail as a
            # CollectorError (not a raw TypeError) so the executor reports the
            # clean "could not read the wallet balance — Nothing was spent"
            # instead of a traceback, and never proceeds on a bad number
            # (bug-hunt finding, 2026-07-11).
            raise CollectorError(
                f"{self.name}: getBalance returned a non-numeric value") from exc

    async def get_token_balance_raw(self, owner: str, mint: str) -> int:
        """Total raw units of ``mint`` held by ``owner`` across its token accounts."""
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = (result or {}).get("value") or [] if isinstance(result, dict) else []
        total = 0
        skipped = 0
        for account in accounts:
            try:
                amount = (((account["account"]["data"]["parsed"]["info"]
                            ["tokenAmount"])).get("amount"))
                total += int(amount)
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
        if skipped:
            # An unparseable token account means the summed balance may be an
            # UNDER-count, so a "sell 100%" dump built on it could leave a
            # remainder. Rare (a mint is usually one account), but log it so a
            # partial dump is diagnosable rather than silent (bug-hunt finding).
            self._logger.warning(
                "%s: skipped %d unparseable token account(s) for mint %s; "
                "balance may be under-reported", self.name, skipped, mint)
        return total

    async def send_raw_transaction(self, signed_base64: str) -> str:
        """Submit a base64 signed transaction; returns the signature string."""
        signature = await self._rpc(
            "sendTransaction",
            [signed_base64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        )
        if not isinstance(signature, str) or not signature:
            raise CollectorError(f"{self.name}: sendTransaction returned no signature")
        return signature

    async def signature_status(self, signature: str) -> dict | None:
        """Confirmation status for one signature, or None while still unknown.

        Returns a dict with ``confirmationStatus`` ('processed'/'confirmed'/
        'finalized') and ``err`` (None on success) once the cluster knows the
        signature; None until then.
        """
        result = await self._rpc(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        values = (result or {}).get("value") or [] if isinstance(result, dict) else []
        return values[0] if values and isinstance(values[0], dict) else None
