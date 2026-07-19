"""LunarCrush social-intelligence collector (Roadmap item 5 — "real social
intelligence (Twitter/X via paid aggregator)").

LunarCrush aggregates public social-media activity (X/Twitter and others)
per coin and exposes it through a paid API v4. This module is the ONLY place
in the codebase that talks to that API and is deliberately built to the
literal shape of what LunarCrush's public endpoints return — nothing more.

**What this collector maps, and why (Rule 8 — never fabricate data):**

LunarCrush's public v4 ``coins/list`` and ``topic/{topic}`` endpoints
describe the WHOLE conversation about a coin, aggregated across platforms:
overall sentiment, X-specific sentiment, unique X-post counts, social
dominance, "galaxy score", rank, and trend. That maps honestly onto
:class:`CommunityProfile`'s ``positive_sentiment_percent`` and
``user_content_per_day`` (already CoinGecko-shaped, aggregate concepts) plus
five new LunarCrush-specific fields (``social_volume_24h``,
``social_dominance_percent``, ``galaxy_score``, ``alt_rank``,
``social_trend``).

**What this collector deliberately never sets:** ``twitter_followers``,
``twitter_engagement_rate_percent``, ``twitter_growth_rate_7d_percent``, and
``bot_follower_percent``. Those are per-ACCOUNT metrics (one Twitter/X
handle's follower count, engagement rate, growth, bot ratio) and LunarCrush's
public coins/topic endpoints do not expose them anywhere — that data only
exists, for a single named creator, under a completely different endpoint
(``/public/creator/:network/:id/v1``), which is out of scope here (one
account is not "the whole token's social conversation"). Populating those
four fields from data that doesn't describe them would be fabrication, not a
coverage improvement, so this client leaves them untouched (``None``) on
every :class:`CommunityProfile` it returns.

**Matching discipline:** meme coins routinely share a ticker symbol across
unrelated chains, so this client matches a token to a LunarCrush coin ONLY
via ``blockchains[].address`` (the on-chain contract address LunarCrush
reports per chain) — never via symbol or name. A symbol match could silently
attribute one coin's social data to a different coin on a different chain
(a Rule-8 data-fabrication bug, not just an inefficiency).

Every field read from LunarCrush is defensively coerced (absent/null/
wrong-type never raises — Rule 6) because live example payloads were not
available while building this: field presence is treated as best-effort.
"""

from __future__ import annotations

from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import CommunityProfile, TokenIdentity

# LunarCrush network strings -> this codebase's chain vocabulary (matches
# the DexScreener-style ids used throughout, e.g. collectors/market_data.py's
# _COINGECKO_PLATFORMS / GeckoTerminal alias tables). Any LunarCrush network
# not listed here is skipped when building the address directory (logged at
# debug, never raised — an unmapped chain is a coverage gap, not an error).
_CHAIN_ALIASES = {
    "solana": "solana",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "bsc": "bnb",
    "binance-smart-chain": "bnb",
    "base": "base",
    "polygon": "polygon",
    "matic": "polygon",
    "arbitrum": "arbitrum",
    "avalanche": "avalanche",
    "avax": "avalanche",
    "optimism": "optimism",
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # NaN check without importing math


def _to_int(value: Any) -> int | None:
    """Parse a count field that providers sometimes send as a decimal
    string (e.g. "1234.0") — int() rejects those directly, so go through
    float first (mirrors collectors/wallet_data.py's _to_int)."""
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else None


def _to_percent(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None or not (0.0 <= parsed <= 100.0):
        return None
    return parsed


class LunarCrushClient(BaseCollector):
    """Client for the LunarCrush public API v4 (social intelligence)."""

    def __init__(self, api_key: str, *, base_url: str = "https://lunarcrush.com/api4",
                 **kwargs: Any) -> None:
        if not api_key:
            raise ValueError("LunarCrushClient requires an API key")
        kwargs.setdefault("name", "lunarcrush")
        kwargs.setdefault("base_url", base_url)
        # Belt-and-suspenders redaction (Rule 16): the key lives in a header,
        # not the URL, but every other collector scrubs its secret from any
        # raised error message the same way regardless of where it travels.
        kwargs.setdefault("redact", (api_key,))
        super().__init__(**kwargs)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def _directory(self) -> dict[tuple[str, str], dict]:
        """(chain, lowercased address) -> LunarCrush coin dict, built from a
        4-hour-cached full snapshot of every tracked coin (Rule 10/11 — this
        is a coarse, full-catalog poll, not a per-token lookup, so it gets a
        much longer TTL than the 900s topic-detail cache below)."""
        payload = await self._get_json(
            "/public/coins/list/v1", headers=self._headers,
            cache_key="lunarcrush:coins:list", cache_ttl=14400.0,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        directory: dict[tuple[str, str], dict] = {}
        if not isinstance(data, list):
            return directory
        for coin in data:
            if not isinstance(coin, dict):
                continue
            blockchains = coin.get("blockchains")
            if not isinstance(blockchains, list):
                continue
            for entry in blockchains:
                if not isinstance(entry, dict):
                    continue
                network = entry.get("network")
                address = entry.get("address")
                if not isinstance(network, str) or not isinstance(address, str) or not address:
                    continue
                chain = _CHAIN_ALIASES.get(network)
                if chain is None:
                    self._logger.debug(
                        "%s: unmapped chain network %r, skipping directory entry",
                        self.name, network)
                    continue
                directory[(chain, address.lower())] = coin
        return directory

    async def _topic_detail(self, topic: str) -> dict | None:
        """Per-topic detail (platform-level sentiment/counts/trend), or None
        on any malformed response — a best-effort enrichment, never fatal
        (Rule 9: a topic-detail failure must not erase coin-level data)."""
        payload = await self._get_json(
            f"/public/topic/{topic}/v1", headers=self._headers,
            cache_key=f"lunarcrush:topic:{topic}", cache_ttl=900.0,  # research cadence
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else None

    async def get_community_profile(self, token: TokenIdentity) -> CommunityProfile | None:
        """Social-conversation facts for one token by contract address.

        Returns ``None`` when the token is not tracked by LunarCrush at all
        (an honest gap, not an error — Rule 8) or when the chain has no
        entry in this client's address directory. A topic-detail failure
        degrades gracefully to coin-level-only data rather than losing the
        directory match already found (Rule 9).
        """
        try:
            directory = await self._directory()
        except CollectorError as exc:
            self._logger.info("%s: coin directory unavailable: %s", self.name, exc)
            return None
        coin = directory.get((token.chain, token.address.lower()))
        if coin is None:
            self._logger.info("%s: %s/%s not tracked by LunarCrush",
                              self.name, token.chain, token.address)
            return None

        coin_sentiment = _to_percent(coin.get("sentiment"))
        social_volume_24h = _to_int(coin.get("social_volume_24h"))
        social_dominance_percent = _to_percent(coin.get("social_dominance"))
        galaxy_score = _to_float(coin.get("galaxy_score"))
        alt_rank = _to_int(coin.get("alt_rank"))

        twitter_sentiment = None
        twitter_post_count = None
        social_trend = None
        topic = coin.get("topic")
        if isinstance(topic, str) and topic:
            try:
                detail = await self._topic_detail(topic)
            except CollectorError as exc:
                # Topic detail is a best-effort enrichment; the coin-level
                # data already gathered above must survive this failure.
                self._logger.info("%s: topic detail unavailable for %s: %s",
                                  self.name, topic, exc)
                detail = None
            if detail is not None:
                types_sentiment = detail.get("types_sentiment")
                if isinstance(types_sentiment, dict):
                    twitter_sentiment = _to_percent(types_sentiment.get("twitter"))
                types_count = detail.get("types_count")
                if isinstance(types_count, dict):
                    twitter_post_count = _to_float(types_count.get("twitter"))
                trend = detail.get("trend")
                if isinstance(trend, str) and trend:
                    social_trend = trend

        positive_sentiment_percent = (
            twitter_sentiment if twitter_sentiment is not None else coin_sentiment)
        user_content_per_day = twitter_post_count

        return CommunityProfile(
            token=token,
            source=self.name,
            positive_sentiment_percent=positive_sentiment_percent,
            user_content_per_day=user_content_per_day,
            social_volume_24h=social_volume_24h,
            social_dominance_percent=social_dominance_percent,
            galaxy_score=galaxy_score,
            alt_rank=alt_rank,
            social_trend=social_trend,
            # twitter_followers / twitter_engagement_rate_percent /
            # twitter_growth_rate_7d_percent / bot_follower_percent are
            # deliberately left unset — see module docstring (Rule 8).
        )
