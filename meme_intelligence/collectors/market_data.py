"""Market data collectors (Spec Part 2 Section 2, Part 32 Category 1).

Providers:

* **DexScreener** — pair lookup and search; public API, no key, documented
  limit 300 req/min for ``latest/dex`` endpoints.
* **GeckoTerminal** — multi-chain new-pool and trending-pool discovery;
  public API, no key, documented free limit ~30 req/min.

Both normalize into the same ``DexPair`` model so downstream analyzers
never see provider-specific shapes (Part 32, Rule 3 — data must be
normalized). Default request budgets stay safely below documented limits
(Rule 11).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import DexPair, TokenIdentity


def _to_float(value: Any) -> float | None:
    """Parse a numeric field that providers send as float, int, or string."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _from_ms_timestamp(value: Any) -> datetime | None:
    ms = _to_float(value)
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _from_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class DexScreenerClient(BaseCollector):
    """Client for the public DexScreener REST API."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "dexscreener")
        kwargs.setdefault("base_url", "https://api.dexscreener.com")
        super().__init__(**kwargs)

    async def get_token_pairs(self, token_address: str, chain: str | None = None) -> list[DexPair]:
        """All trading pairs for a token contract, optionally filtered to one chain."""
        if not token_address:
            raise ValueError("token_address must be non-empty")
        payload = await self._get_json(
            f"latest/dex/tokens/{token_address}",
            cache_key=f"dexscreener:tokens:{token_address.lower()}",
        )
        pairs = self._parse_pairs(payload)
        if chain is not None:
            pairs = [p for p in pairs if p.chain == chain]
        return pairs

    async def search_pairs(self, query: str) -> list[DexPair]:
        """Search pairs by token name, symbol, or address."""
        if not query:
            raise ValueError("query must be non-empty")
        payload = await self._get_json(
            "latest/dex/search",
            params={"q": query},
            cache_key=f"dexscreener:search:{query.lower()}",
        )
        return self._parse_pairs(payload)

    async def get_pair(self, chain: str, pair_address: str) -> DexPair | None:
        """One specific pair by chain + pair address, or ``None`` if unknown."""
        if not chain or not pair_address:
            raise ValueError("chain and pair_address must be non-empty")
        payload = await self._get_json(
            f"latest/dex/pairs/{chain}/{pair_address}",
            cache_key=f"dexscreener:pair:{chain}:{pair_address.lower()}",
        )
        pairs = self._parse_pairs(payload)
        return pairs[0] if pairs else None

    def _parse_pairs(self, payload: Any) -> list[DexPair]:
        """Normalize a DexScreener response into ``DexPair`` models.

        Malformed entries are logged and skipped rather than aborting the
        whole batch — one bad record must not blind the scanner (Rule 6).
        """
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object, got {type(payload).__name__}")

        raw_pairs = payload.get("pairs") or []
        if not isinstance(raw_pairs, list):
            raise CollectorError(f"{self.name}: 'pairs' field is not a list")

        pairs: list[DexPair] = []
        for raw in raw_pairs:
            try:
                pairs.append(self._parse_pair(raw))
            except (KeyError, TypeError, AttributeError) as exc:
                self._logger.warning("skipping malformed pair entry: %s", exc)
        return pairs

    @staticmethod
    def _parse_pair(raw: dict[str, Any]) -> DexPair:
        base = raw.get("baseToken") or {}
        quote = raw.get("quoteToken") or {}
        txns_24h = (raw.get("txns") or {}).get("h24") or {}
        return DexPair(
            chain=raw["chainId"],
            pair_address=raw["pairAddress"],
            base_token=TokenIdentity(
                chain=raw["chainId"],
                address=base["address"],
                name=base.get("name"),
                symbol=base.get("symbol"),
            ),
            dex_id=raw.get("dexId"),
            quote_symbol=quote.get("symbol"),
            price_usd=_to_float(raw.get("priceUsd")),
            liquidity_usd=_to_float((raw.get("liquidity") or {}).get("usd")),
            fdv=_to_float(raw.get("fdv")),
            market_cap=_to_float(raw.get("marketCap")),
            volume_24h=_to_float((raw.get("volume") or {}).get("h24")),
            price_change_24h=_to_float((raw.get("priceChange") or {}).get("h24")),
            buys_24h=_to_int(txns_24h.get("buys")),
            sells_24h=_to_int(txns_24h.get("sells")),
            pair_created_at=_from_ms_timestamp(raw.get("pairCreatedAt")),
            url=raw.get("url"),
        )


class GeckoTerminalClient(BaseCollector):
    """Client for the public GeckoTerminal REST API (Spec Part 2 — GeckoTerminal).

    The ``new_pools`` endpoint is the system's primary free source for
    Layer 1 discovery (Part 2 Section 4): newly created liquidity pools
    across chains, refreshed continuously.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "geckoterminal")
        kwargs.setdefault("base_url", "https://api.geckoterminal.com")
        super().__init__(**kwargs)

    async def get_new_pools(self, network: str, page: int = 1) -> list[DexPair]:
        """Most recently created pools on one network (e.g. ``solana``, ``eth``, ``base``)."""
        if not network:
            raise ValueError("network must be non-empty")
        payload = await self._get_json(
            f"api/v2/networks/{network}/new_pools",
            params={"page": str(page)},
            cache_key=f"geckoterminal:new_pools:{network}:{page}",
            cache_ttl=10.0,  # new-pool data is only useful fresh (Part 15 critical data)
        )
        return self._parse_pools(payload)

    async def get_trending_pools(self, network: str) -> list[DexPair]:
        """Currently trending pools on one network."""
        if not network:
            raise ValueError("network must be non-empty")
        payload = await self._get_json(
            f"api/v2/networks/{network}/trending_pools",
            cache_key=f"geckoterminal:trending:{network}",
            cache_ttl=60.0,
        )
        return self._parse_pools(payload)

    def _parse_pools(self, payload: Any) -> list[DexPair]:
        """Normalize a GeckoTerminal JSON:API response into ``DexPair`` models."""
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object, got {type(payload).__name__}")
        items = payload.get("data") or []
        if not isinstance(items, list):
            raise CollectorError(f"{self.name}: 'data' field is not a list")

        pools: list[DexPair] = []
        for item in items:
            try:
                pools.append(self._parse_pool(item))
            except (KeyError, TypeError, AttributeError, IndexError) as exc:
                self._logger.warning("skipping malformed pool entry: %s", exc)
        return pools

    @staticmethod
    def _parse_pool(item: dict[str, Any]) -> DexPair:
        attrs = item["attributes"]
        relationships = item.get("relationships") or {}

        # Base token id has the form "<network>_<address>"; the pool id shares
        # the same prefix, so split on the first underscore.
        base_id = ((relationships.get("base_token") or {}).get("data") or {}).get("id", "")
        network, _, base_address = base_id.partition("_")
        if not network or not base_address:
            raise KeyError(f"unparseable base token id: {base_id!r}")

        # attributes["name"] looks like "WIF / SOL"; the left side is the base symbol.
        pool_name = attrs.get("name") or ""
        base_symbol = pool_name.split(" / ")[0].strip() or None

        txns_24h = (attrs.get("transactions") or {}).get("h24") or {}
        volume_24h = (attrs.get("volume_usd") or {}).get("h24")
        price_change_24h = (attrs.get("price_change_percentage") or {}).get("h24")

        return DexPair(
            chain=network,
            pair_address=attrs["address"],
            base_token=TokenIdentity(chain=network, address=base_address, symbol=base_symbol),
            dex_id=((relationships.get("dex") or {}).get("data") or {}).get("id"),
            quote_symbol=(pool_name.split(" / ")[1].strip() if " / " in pool_name else None),
            price_usd=_to_float(attrs.get("base_token_price_usd")),
            liquidity_usd=_to_float(attrs.get("reserve_in_usd")),
            fdv=_to_float(attrs.get("fdv_usd")),
            market_cap=_to_float(attrs.get("market_cap_usd")),
            volume_24h=_to_float(volume_24h),
            price_change_24h=_to_float(price_change_24h),
            buys_24h=_to_int(txns_24h.get("buys")),
            sells_24h=_to_int(txns_24h.get("sells")),
            pair_created_at=_from_iso_timestamp(attrs.get("pool_created_at")),
            url=None,
        )
