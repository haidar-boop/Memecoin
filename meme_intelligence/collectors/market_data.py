"""Market data collectors (Spec Part 2 Section 2, Part 32 Category 1).

First provider: DexScreener — one of the primary discovery engines. Its
public API needs no key; documented limit is 300 requests/minute for the
``latest/dex`` endpoints, and our default budget stays safely below that
(Rule 11). GeckoTerminal and Birdeye clients join this module in the
discovery-engine phase, all normalizing into the same ``DexPair`` model so
downstream analyzers never see provider-specific shapes (Part 32, Rule 3 —
data must be normalized).
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
