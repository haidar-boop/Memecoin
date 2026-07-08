"""Minimal CLI for exercising the data infrastructure end-to-end.

Usage::

    python -m meme_intelligence search PEPE
    python -m meme_intelligence token <contract-address> [--chain solana]

This is a verification harness for the collection layer; the full scanner
controller (Spec Part 22, Section 2) replaces it as the primary entry point
in a later phase.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from meme_intelligence.collectors.market_data import DexScreenerClient
from meme_intelligence.config.settings import get_settings
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.logging_setup import setup_logging
from meme_intelligence.core.models import DexPair
from meme_intelligence.core.rate_limiter import RateLimiter


def _format_pair(pair: DexPair) -> str:
    def money(value: float | None) -> str:
        return f"${value:,.0f}" if value is not None else "unknown"

    symbol = pair.base_token.symbol or "?"
    price = f"${pair.price_usd:.8f}" if pair.price_usd is not None else "unknown"
    created = pair.pair_created_at.strftime("%Y-%m-%d") if pair.pair_created_at else "unknown"
    return (
        f"{symbol:>10} | {pair.chain:<10} | price {price:>15} | "
        f"liq {money(pair.liquidity_usd):>12} | vol24h {money(pair.volume_24h):>12} | "
        f"mcap {money(pair.market_cap):>12} | created {created}"
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    logger = setup_logging(settings.log_level, settings.log_dir)

    client = DexScreenerClient(
        base_url=settings.providers.dexscreener_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.dexscreener_requests_per_minute),
        cache=TTLCache(settings.http.cache_max_entries, settings.http.cache_ttl_seconds),
        timeout_seconds=settings.http.timeout_seconds,
        retry_attempts=settings.http.retry_attempts,
        retry_base_delay=settings.http.retry_base_delay,
        retry_max_delay=settings.http.retry_max_delay,
    )

    async with client:
        if args.command == "search":
            logger.info("searching DexScreener pairs for %r", args.query)
            pairs = await client.search_pairs(args.query)
        else:
            logger.info("fetching DexScreener pairs for token %s", args.address)
            pairs = await client.get_token_pairs(args.address, chain=args.chain)

    if not pairs:
        print("No pairs found.")
        return 1

    pairs.sort(key=lambda p: p.liquidity_usd or 0.0, reverse=True)
    print(f"Found {len(pairs)} pair(s); top {min(len(pairs), args.limit)} by liquidity:\n")
    for pair in pairs[: args.limit]:
        print(_format_pair(pair))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meme_intelligence", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="search pairs by name/symbol/address")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)

    token = sub.add_parser("token", help="fetch pairs for a token contract address")
    token.add_argument("address")
    token.add_argument("--chain", default=None, help="filter to one chain id (e.g. solana)")
    token.add_argument("--limit", type=int, default=5)

    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
