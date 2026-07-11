"""Wallet-intelligence collectors (Spec Part 17; Part 2 — Helius/Birdeye).

Solana-first: both providers cover Solana deeply, which is also where new
meme launches concentrate. EVM wallet intelligence can join later behind
the same normalized models.

* **Helius** — top holders straight from chain state (two RPC calls:
  ``getTokenLargestAccounts`` + ``getMultipleAccounts`` to resolve token
  accounts to owner wallets, plus ``getTokenSupply`` for percentages) and
  parsed token transfers from the Enhanced Transactions API.
* **Birdeye** — token overview (holder/wallet counts) and recent trades
  with the trading wallet, side, and USD size.

Both free tiers are metered, so responses cache aggressively and the
wallet layer only runs on demand (Rule 10/11).
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import (
    TokenIdentity,
    TokenTrade,
    TokenTransfer,
    WalletHolding,
    WalletIntelData,
)

# Well-known Solana burn/system addresses excluded from holder analysis.
_BURN_OWNERS = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _token_amount(value: Any) -> float | None:
    """UI-denominated amount from a Solana token-amount object.

    ``uiAmount`` is deprecated/nullable in the RPC spec, while the raw
    ``amount`` + ``decimals`` are always present — a null ``uiAmount``
    previously nulled the supply and dropped every holding (bug-hunt
    finding). Fall back through uiAmountString and amount/10**decimals.
    """
    if not isinstance(value, dict):
        return None
    amount = _to_float(value.get("uiAmount"))
    if amount is not None:
        return amount
    amount = _to_float(value.get("uiAmountString"))
    if amount is not None:
        return amount
    raw = _to_float(value.get("amount"))
    decimals = _to_int(value.get("decimals"))
    if raw is not None and decimals is not None and 0 <= decimals <= 30:
        return raw / (10 ** decimals)
    return None


def _to_int(value: Any) -> int | None:
    """Parse a count field that providers sometimes send as a decimal
    string (e.g. "1234.0") — int() rejects those directly, so go through
    float first."""
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else None


def _from_unix(value: Any) -> datetime | None:
    ts = _to_float(value)
    if ts is None or not math.isfinite(ts) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


class HeliusClient(BaseCollector):
    """Client for Helius Solana RPC + Enhanced Transactions API."""

    def __init__(self, api_key: str, *, rpc_url: str = "https://mainnet.helius-rpc.com",
                 api_url: str = "https://api.helius.xyz", **kwargs: Any) -> None:
        if not api_key:
            raise ValueError("HeliusClient requires an API key")
        kwargs.setdefault("name", "helius")
        kwargs.setdefault("base_url", rpc_url)
        # The key is embedded directly in the RPC request path (Helius has
        # no header-auth option), so it must be scrubbed from any raised
        # error message the same way Telegram/Discord credentials are
        # (Rule 16) — otherwise a timeout/429/5xx logs it in plaintext.
        kwargs.setdefault("redact", (api_key,))
        super().__init__(**kwargs)
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")

    async def _rpc(self, method: str, params: list, *, cache_key: str | None = None,
                   cache_ttl: float | None = None) -> Any:
        payload = await self._get_json(
            f"/?api-key={self._api_key}",
            cache_key=cache_key, cache_ttl=cache_ttl,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object from RPC")
        if "error" in payload:
            raise CollectorError(f"{self.name}: RPC error on {method}: {payload['error']}")
        return payload.get("result")

    async def get_top_holders(self, mint: str, limit: int = 20) -> list[WalletHolding]:
        """Top holders as owner wallets with supply percentages.

        Chain truth, not an indexer: largest token accounts -> resolve each
        to its owner wallet -> divide by supply. Burn addresses excluded.
        """
        if not mint:
            raise ValueError("mint must be non-empty")

        supply_result = await self._rpc(
            "getTokenSupply", [mint],
            cache_key=f"helius:supply:{mint}", cache_ttl=300.0,
        )
        supply = _token_amount(((supply_result or {}).get("value") or {}))

        largest = await self._rpc(
            "getTokenLargestAccounts", [mint],
            cache_key=f"helius:largest:{mint}", cache_ttl=120.0,
        )
        accounts = (largest or {}).get("value") or []
        if not accounts:
            return []

        # Keep the account OBJECTS that make it into the owner lookup, so the
        # owner_infos response (which corresponds to token_accounts, in order)
        # is zipped below against the SAME accounts — not the full, unfiltered
        # `accounts` list. Zipping the full list would misalign owner→amount
        # the moment any account inside the window was dropped for a missing
        # address, attributing a balance to the wrong wallet (fabricated whale
        # data, Rule 8 — bug-hunt finding, 2026-07-11).
        kept = [a for a in accounts[:limit] if a.get("address")]
        token_accounts = [a["address"] for a in kept]
        # Content-derived cache key (bug-hunt finding, independently confirmed
        # twice): keying by len() alone let a STALE owners response — cached a
        # rate-limiter-wait after the largest-accounts response, so their TTL
        # windows are offset — be zipped against a FRESH account list whose
        # top-20 membership had changed, attributing balances to the wrong
        # wallets (fabricated whale data, Rule 8).
        # sha256 (not for security — just a stable cache-key fingerprint of the
        # account list; sha256 keeps static analysers happy about hash choice).
        accounts_fingerprint = hashlib.sha256(
            ",".join(token_accounts).encode()).hexdigest()
        owners_result = await self._rpc(
            "getMultipleAccounts", [token_accounts, {"encoding": "jsonParsed"}],
            cache_key=f"helius:owners:{mint}:{accounts_fingerprint}", cache_ttl=120.0,
        )
        owner_infos = (owners_result or {}).get("value") or []

        # Aggregate by OWNER (the docstring's contract): a whale split across
        # several token accounts (associated + auxiliary) previously appeared
        # as several small independent holders, so 2x3% never crossed the 5%
        # risk-whale line and concentration was understated (bug-hunt finding).
        by_owner: dict[str, float] = {}
        # strict=False: a malformed getMultipleAccounts response could return
        # fewer entries than requested; truncating (and under-counting) is the
        # safe degradation here, not a crash. Alignment is guaranteed by using
        # `kept` (the accounts that produced token_accounts).
        for account, info in zip(kept, owner_infos, strict=False):
            ui_amount = _token_amount(account)
            owner = None
            if isinstance(info, dict):
                owner = (((info.get("data") or {}).get("parsed") or {})
                         .get("info") or {}).get("owner")
            if not owner or owner in _BURN_OWNERS or ui_amount is None:
                continue
            by_owner[owner] = by_owner.get(owner, 0.0) + ui_amount

        if not supply or supply <= 0:
            return []
        holdings = [
            WalletHolding(owner=owner, percent=100.0 * amount / supply, ui_amount=amount)
            for owner, amount in by_owner.items()
        ]
        holdings.sort(key=lambda h: h.percent, reverse=True)
        return holdings

    async def get_recent_transfers(self, mint: str, limit: int = 50) -> list[TokenTransfer]:
        """Recent parsed token transfers involving the mint (Enhanced API)."""
        if not mint:
            raise ValueError("mint must be non-empty")
        payload = await self._get_json(
            f"{self._api_url}/v0/addresses/{mint}/transactions",
            params={"api-key": self._api_key, "limit": str(min(limit, 100))},
            cache_key=f"helius:txs:{mint}", cache_ttl=60.0,
        )
        if not isinstance(payload, list):
            raise CollectorError(f"{self.name}: expected JSON array from enhanced API")

        transfers: list[TokenTransfer] = []
        for tx in payload:
            if not isinstance(tx, dict):
                continue
            timestamp = _from_unix(tx.get("timestamp"))
            for transfer in tx.get("tokenTransfers") or []:
                if transfer.get("mint") != mint:
                    continue
                transfers.append(TokenTransfer(
                    from_owner=transfer.get("fromUserAccount") or None,
                    to_owner=transfer.get("toUserAccount") or None,
                    ui_amount=_to_float(transfer.get("tokenAmount")),
                    timestamp=timestamp,
                ))
        return transfers


class BirdeyeClient(BaseCollector):
    """Client for the Birdeye Data Services public API."""

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        if not api_key:
            raise ValueError("BirdeyeClient requires an API key")
        kwargs.setdefault("name", "birdeye")
        kwargs.setdefault("base_url", "https://public-api.birdeye.so")
        super().__init__(**kwargs)
        self._headers = {"X-API-KEY": api_key, "x-chain": "solana"}

    async def _birdeye_get(self, path: str, params: dict, *, cache_key: str,
                           cache_ttl: float) -> dict:
        payload = await self._get_json(
            path, params=params, headers=self._headers,
            cache_key=cache_key, cache_ttl=cache_ttl,
        )
        if not isinstance(payload, dict) or not payload.get("success", False):
            raise CollectorError(f"{self.name}: unsuccessful response on {path}: "
                                 f"{str(payload)[:120]}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def get_token_overview(self, mint: str) -> dict[str, Any]:
        """Holder count and 24h wallet/trade activity for one token."""
        if not mint:
            raise ValueError("mint must be non-empty")
        data = await self._birdeye_get(
            "defi/token_overview", {"address": mint},
            cache_key=f"birdeye:overview:{mint}", cache_ttl=120.0,
        )
        return {
            "holder_count": _to_int(data.get("holder")),
            "unique_wallets_24h": _to_int(data.get("uniqueWallet24h")),
        }

    async def get_recent_trades(self, mint: str, limit: int = 50) -> list[TokenTrade]:
        """Recent swaps with the trading wallet, side, and USD size."""
        if not mint:
            raise ValueError("mint must be non-empty")
        data = await self._birdeye_get(
            "defi/txs/token",
            {"address": mint, "offset": "0", "limit": str(min(limit, 50)),
             "tx_type": "swap", "sort_type": "desc"},
            cache_key=f"birdeye:trades:{mint}", cache_ttl=60.0,
        )
        def leg_usd(leg: Any) -> float | None:
            """USD value of one trade leg: uiAmount x per-unit USD price."""
            if not isinstance(leg, dict):
                return None
            ui = _to_float(leg.get("uiAmount"))
            price = _to_float(leg.get("price")) or _to_float(leg.get("nearestPrice"))
            if ui is None or price is None:
                return None
            return abs(ui * price)

        def leg_price(leg: Any) -> float | None:
            if isinstance(leg, dict) and leg.get("address") == mint:
                return _to_float(leg.get("price")) or _to_float(leg.get("nearestPrice"))
            return None

        trades: list[TokenTrade] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner")
            side = item.get("side")
            if not owner or side not in ("buy", "sell"):
                continue
            base, quote = item.get("base"), item.get("quote")
            trades.append(TokenTrade(
                owner=owner, side=side,
                volume_usd=leg_usd(base) or leg_usd(quote),
                timestamp=_from_unix(item.get("blockUnixTime")),
                price_usd=leg_price(quote) or leg_price(base),
            ))
        return trades


class WalletDataService:
    """Combines the wallet collectors into one normalized snapshot (Rule 9).

    Each source failing individually degrades the snapshot instead of
    killing it — whatever was gathered is returned with its source list,
    and the analyzer's coverage reporting does the rest.
    """

    def __init__(self, helius: HeliusClient | None, birdeye: BirdeyeClient | None,
                 *, top_holders_limit: int = 20, recent_trades_limit: int = 50) -> None:
        if helius is None and birdeye is None:
            raise ValueError("WalletDataService needs at least one provider")
        self._helius = helius
        self._birdeye = birdeye
        self._holders_limit = top_holders_limit
        self._trades_limit = recent_trades_limit

    async def close(self) -> None:
        for client in (self._helius, self._birdeye):
            if client is not None:
                await client.close()

    async def __aenter__(self) -> "WalletDataService":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def gather(self, token: TokenIdentity) -> WalletIntelData:
        sources: list[str] = []
        holders: tuple[WalletHolding, ...] = ()
        transfers: tuple[TokenTransfer, ...] = ()
        trades: tuple[TokenTrade, ...] = ()
        holder_count = unique_wallets = None

        if self._helius is not None:
            # Two independent calls: a get_recent_transfers failure must
            # not erase the "helius" source tag (or the already-fetched
            # holders) earned by a successful get_top_holders — the
            # combined try previously discarded both on a partial failure,
            # leaving real Helius holder data returned with no source
            # attribution (Rule 8: the caller can no longer tell where it
            # came from).
            helius_contributed = False
            try:
                holders = tuple(await self._helius.get_top_holders(
                    token.address, limit=self._holders_limit))
                helius_contributed = True
            except CollectorError:
                pass
            try:
                transfers = tuple(await self._helius.get_recent_transfers(
                    token.address, limit=self._trades_limit))
                helius_contributed = True
            except CollectorError:
                pass
            if helius_contributed:
                sources.append("helius")
        if self._birdeye is not None:
            # Same per-call isolation as the Helius block above (bug-hunt
            # finding: the combined try discarded an already-fetched overview
            # — holder_count, unique_wallets — whenever the trades call
            # failed, and dropped the "birdeye" source tag with it).
            birdeye_contributed = False
            try:
                overview = await self._birdeye.get_token_overview(token.address)
                holder_count = overview.get("holder_count")
                unique_wallets = overview.get("unique_wallets_24h")
                birdeye_contributed = True
            except CollectorError:
                pass
            try:
                trades = tuple(await self._birdeye.get_recent_trades(
                    token.address, limit=self._trades_limit))
                birdeye_contributed = True
            except CollectorError:
                pass
            if birdeye_contributed:
                sources.append("birdeye")

        return WalletIntelData(
            token=token,
            sources=tuple(sources),
            top_holders=holders,
            recent_trades=trades,
            recent_transfers=transfers,
            holder_count=holder_count,
            unique_wallets_24h=unique_wallets,
        )
