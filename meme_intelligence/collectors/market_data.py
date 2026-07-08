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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import CommunityProfile, DexPair, TokenIdentity


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


def _to_percent(value: Any) -> float | None:
    """Parse a 0-100 percentage field; out-of-range or non-finite values are
    treated as missing data rather than fabricated or allowed to crash a
    downstream analyzer that enforces the 0-100 contract (Rule 6, Rule 8).
    """
    parsed = _to_float(value)
    if parsed is None or not (0.0 <= parsed <= 100.0):
        return None
    return parsed


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
        txns = raw.get("txns") or {}
        txns_24h = txns.get("h24") or {}
        txns_1h = txns.get("h1") or {}
        volume = raw.get("volume") or {}
        price_change = raw.get("priceChange") or {}
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
            volume_24h=_to_float(volume.get("h24")),
            price_change_24h=_to_float(price_change.get("h24")),
            buys_24h=_to_int(txns_24h.get("buys")),
            sells_24h=_to_int(txns_24h.get("sells")),
            price_change_1h=_to_float(price_change.get("h1")),
            price_change_6h=_to_float(price_change.get("h6")),
            volume_1h=_to_float(volume.get("h1")),
            volume_6h=_to_float(volume.get("h6")),
            buys_1h=_to_int(txns_1h.get("buys")),
            sells_1h=_to_int(txns_1h.get("sells")),
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

    async def get_token_pairs(self, token_address: str, chain: str | None = None) -> list[DexPair]:
        """Pools for one token — same interface as DexScreener's method, so the
        two providers are interchangeable in a failover pool (Part 15, Section 4).

        GeckoTerminal's endpoint is per-network, so ``chain`` is required here;
        a missing chain raises :class:`CollectorError` so a provider pool skips
        to the next provider instead of crashing.
        """
        if not token_address:
            raise ValueError("token_address must be non-empty")
        if not chain:
            raise CollectorError(f"{self.name}: chain is required for token pair lookup")
        network = to_geckoterminal_network(chain)
        payload = await self._get_json(
            f"api/v2/networks/{network}/tokens/{token_address}/pools",
            cache_key=f"geckoterminal:token_pools:{network}:{token_address.lower()}",
            cache_ttl=30.0,
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

        transactions = attrs.get("transactions") or {}
        txns_24h = transactions.get("h24") or {}
        txns_1h = transactions.get("h1") or {}
        volume = attrs.get("volume_usd") or {}
        price_change = attrs.get("price_change_percentage") or {}

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
            volume_24h=_to_float(volume.get("h24")),
            price_change_24h=_to_float(price_change.get("h24")),
            buys_24h=_to_int(txns_24h.get("buys")),
            sells_24h=_to_int(txns_24h.get("sells")),
            buyers_24h=_to_int(txns_24h.get("buyers")),
            sellers_24h=_to_int(txns_24h.get("sellers")),
            price_change_1h=_to_float(price_change.get("h1")),
            price_change_6h=_to_float(price_change.get("h6")),
            volume_1h=_to_float(volume.get("h1")),
            volume_6h=_to_float(volume.get("h6")),
            buys_1h=_to_int(txns_1h.get("buys")),
            sells_1h=_to_int(txns_1h.get("sells")),
            pair_created_at=_from_iso_timestamp(attrs.get("pool_created_at")),
            url=None,
        )


@dataclass(frozen=True)
class MajorsSnapshot:
    """24h state of the majors used for the market-environment check (Part 11, Section 2)."""

    btc_price_usd: float | None = None
    btc_change_24h_percent: float | None = None
    eth_change_24h_percent: float | None = None
    sol_change_24h_percent: float | None = None


# DexScreener-style chain ids -> CoinGecko asset-platform ids for the
# contract-address coin lookup (community data collection).
_COINGECKO_PLATFORMS = {
    "solana": "solana",
    "ethereum": "ethereum",
    "base": "base",
    "bnb": "binance-smart-chain",
    "bsc": "binance-smart-chain",
    "arbitrum": "arbitrum-one",
    "polygon": "polygon-pos",
    "avalanche": "avalanche",
    "optimism": "optimistic-ethereum",
}


class CoinGeckoClient(BaseCollector):
    """Client for the public CoinGecko API.

    Two duties: the morning market-environment check (BTC/ETH/SOL trend)
    and free per-token community data (Part 5 — the "cheap aggregator"
    decision: CoinGecko community data at $0 now, a paid social aggregator
    later if the system earns it; see handoff/DECISIONS_LOG.md). Kept to a
    small request budget within the free tier (Rule 11); an optional demo
    API key raises the rate limit.
    """

    def __init__(self, api_key: str = "", **kwargs: Any) -> None:
        kwargs.setdefault("name", "coingecko")
        kwargs.setdefault("base_url", "https://api.coingecko.com")
        super().__init__(**kwargs)
        self._headers = {"x-cg-demo-api-key": api_key} if api_key else None

    async def get_majors(self) -> MajorsSnapshot:
        payload = await self._get_json(
            "api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            headers=self._headers,
            cache_key="coingecko:majors",
            cache_ttl=120.0,
        )
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object, got {type(payload).__name__}")

        def entry(coin: str, field: str) -> float | None:
            data = payload.get(coin)
            return _to_float(data.get(field)) if isinstance(data, dict) else None

        return MajorsSnapshot(
            btc_price_usd=entry("bitcoin", "usd"),
            btc_change_24h_percent=entry("bitcoin", "usd_24h_change"),
            eth_change_24h_percent=entry("ethereum", "usd_24h_change"),
            sol_change_24h_percent=entry("solana", "usd_24h_change"),
        )

    async def get_community_profile(self, token: TokenIdentity) -> "CommunityProfile | None":
        """Community facts for one token by contract address (Part 5 data feed).

        Returns ``None`` when the chain has no CoinGecko platform mapping or
        the token is not listed there (very new launches take days to be
        indexed) — an honest gap, not an error (Rule 8). Fields CoinGecko
        does not track (Twitter engagement, Discord, bot detection) stay
        ``None`` and the community engine reports the reduced coverage.
        """
        platform = _COINGECKO_PLATFORMS.get(token.chain)
        if platform is None:
            self._logger.info("%s: no CoinGecko platform for chain %s", self.name, token.chain)
            return None
        # Lowercase only for the cache key: some providers reach the same
        # token via a checksummed address and some via lowercase, which
        # otherwise fragments the cache into two entries for one token
        # (Rules 10/11). The request itself keeps the caller's original
        # casing since CoinGecko accepts either.
        cache_key = f"coingecko:community:{platform}:{token.address.lower()}"
        not_listed_key = f"{cache_key}:404"
        if self._cache is not None and await self._cache.get(not_listed_key) is not None:
            return None
        try:
            payload = await self._get_json(
                f"api/v3/coins/{platform}/contract/{token.address}",
                headers=self._headers,
                cache_key=cache_key,
                cache_ttl=600.0,  # research cadence (Part 21 Section 4)
            )
        except CollectorError as exc:
            # Unlisted tokens return 404: "not listed" is a data gap, not a failure.
            # Cached separately (not via the success cache_key) so repeated
            # lookups of a not-yet-indexed token don't re-hit the API every
            # cycle within the TTL window (Rules 10/11).
            if exc.status_code == 404:
                self._logger.info("%s: %s/%s not listed on CoinGecko",
                                  self.name, token.chain, token.address)
                if self._cache is not None:
                    await self._cache.set(not_listed_key, True, 600.0)
                return None
            raise
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object, got {type(payload).__name__}")

        community = payload.get("community_data")
        community = community if isinstance(community, dict) else {}

        # Reddit zeros usually mean "no subreddit tracked", not "zero
        # activity" — only trust them when a real subscriber base exists.
        reddit_subscribers = _to_int(community.get("reddit_subscribers"))
        reddit_posts_per_day = None
        user_content_per_day = None
        if reddit_subscribers:
            posts_48h = _to_float(community.get("reddit_average_posts_48h"))
            comments_48h = _to_float(community.get("reddit_average_comments_48h"))
            if posts_48h is not None:
                reddit_posts_per_day = posts_48h / 2.0
            if posts_48h is not None and comments_48h is not None:
                user_content_per_day = (posts_48h + comments_48h) / 2.0
        else:
            reddit_subscribers = None

        return CommunityProfile(
            token=token,
            source=self.name,
            telegram_members=_to_int(community.get("telegram_channel_user_count")),
            reddit_subscribers=reddit_subscribers,
            reddit_posts_per_day=reddit_posts_per_day,
            user_content_per_day=user_content_per_day,
            positive_sentiment_percent=_to_percent(payload.get("sentiment_votes_up_percentage")),
        )


# DexScreener-style chain ids -> GeckoTerminal network ids, so both market
# providers accept the same chain vocabulary (Part 15 Section 4 — rotation
# requires interchangeable providers).
_GECKOTERMINAL_NETWORK_ALIASES = {
    "ethereum": "eth",
    "polygon": "polygon_pos",
    "avalanche": "avax",
    "bnb": "bsc",
}


def to_geckoterminal_network(chain: str) -> str:
    return _GECKOTERMINAL_NETWORK_ALIASES.get(chain, chain)
