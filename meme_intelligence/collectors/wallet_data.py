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


def _from_unix(value: Any) -> datetime | None:
    ts = _to_float(value)
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# SPL Token stores ``decimals`` as a u8; bound the fallback exponent so a
# corrupt payload cannot trigger a runaway ``10 ** decimals``.
_MAX_TOKEN_DECIMALS = 255


def _ui_amount(amount_obj: Any) -> float | None:
    """Human-scale amount for a Solana token-amount object.

    The RPC ``uiAmount`` field is deprecated and nullable; fall back to the
    non-nullable raw ``amount`` scaled by ``decimals`` so token supply and
    holder balances survive when the chain returns ``uiAmount: null``.
    """
    if not isinstance(amount_obj, dict):
        return None
    ui = _to_float(amount_obj.get("uiAmount"))
    if ui is not None:
        return ui
    raw = _to_float(amount_obj.get("amount"))
    decimals = amount_obj.get("decimals")
    if raw is not None and isinstance(decimals, int) and 0 <= decimals <= _MAX_TOKEN_DECIMALS:
        return raw / (10 ** decimals)
    return None


class HeliusClient(BaseCollector):
    """Client for Helius Solana RPC + Enhanced Transactions API."""

    def __init__(self, api_key: str, *, rpc_url: str = "https://mainnet.helius-rpc.com",
                 api_url: str = "https://api.helius.xyz", **kwargs: Any) -> None:
        if not api_key:
            raise ValueError("HeliusClient requires an API key")
        kwargs.setdefault("name", "helius")
        kwargs.setdefault("base_url", rpc_url)
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
        supply = _ui_amount((supply_result or {}).get("value"))

        largest = await self._rpc(
            "getTokenLargestAccounts", [mint],
            cache_key=f"helius:largest:{mint}", cache_ttl=120.0,
        )
        accounts = (largest or {}).get("value") or []
        if not accounts:
            return []

        token_accounts = [a.get("address") for a in accounts[:limit] if a.get("address")]
        owners_result = await self._rpc(
            "getMultipleAccounts", [token_accounts, {"encoding": "jsonParsed"}],
            cache_key=f"helius:owners:{mint}:{len(token_accounts)}", cache_ttl=120.0,
        )
        owner_infos = (owners_result or {}).get("value") or []

        holdings: list[WalletHolding] = []
        for account, info in zip(accounts, owner_infos):
            ui_amount = _ui_amount(account)
            owner = None
            if isinstance(info, dict):
                owner = (((info.get("data") or {}).get("parsed") or {})
                         .get("info") or {}).get("owner")
            if not owner or owner in _BURN_OWNERS:
                continue
            percent = None
            if ui_amount is not None and supply and supply > 0:
                percent = 100.0 * ui_amount / supply
            if percent is None:
                continue
            holdings.append(WalletHolding(owner=owner, percent=percent, ui_amount=ui_amount))
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
            "holder_count": int(data["holder"]) if _to_float(data.get("holder")) is not None else None,
            "unique_wallets_24h": (int(data["uniqueWallet24h"])
                                   if _to_float(data.get("uniqueWallet24h")) is not None else None),
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
            try:
                holders = tuple(await self._helius.get_top_holders(
                    token.address, limit=self._holders_limit))
                sources.append("helius")
                transfers = tuple(await self._helius.get_recent_transfers(
                    token.address, limit=self._trades_limit))
            except CollectorError:
                pass
        if self._birdeye is not None:
            try:
                overview = await self._birdeye.get_token_overview(token.address)
                holder_count = overview.get("holder_count")
                unique_wallets = overview.get("unique_wallets_24h")
                sources.append("birdeye")
                trades = tuple(await self._birdeye.get_recent_trades(
                    token.address, limit=self._trades_limit))
            except CollectorError:
                pass

        return WalletIntelData(
            token=token,
            sources=tuple(sources),
            top_holders=holders,
            recent_trades=trades,
            recent_transfers=transfers,
            holder_count=holder_count,
            unique_wallets_24h=unique_wallets,
        )
