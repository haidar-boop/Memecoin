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
            raise CollectorError(f"{self.name}: RPC error on {method}: {payload['error']}")
        return payload.get("result")

    async def get_sol_balance_lamports(self, owner: str) -> int:
        """Wallet SOL balance in lamports."""
        result = await self._rpc("getBalance", [owner])
        value = (result or {}).get("value") if isinstance(result, dict) else None
        return int(value) if value is not None else 0

    async def get_token_balance_raw(self, owner: str, mint: str) -> int:
        """Total raw units of ``mint`` held by ``owner`` across its token accounts."""
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        accounts = (result or {}).get("value") or [] if isinstance(result, dict) else []
        total = 0
        for account in accounts:
            try:
                amount = (((account["account"]["data"]["parsed"]["info"]
                            ["tokenAmount"])).get("amount"))
                total += int(amount)
            except (KeyError, TypeError, ValueError):
                continue
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
