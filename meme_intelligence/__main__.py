"""CLI for exercising the intelligence pipeline end-to-end.

Usage::

    python -m meme_intelligence search PEPE
    python -m meme_intelligence token <contract-address> [--chain solana]
    python -m meme_intelligence discover --network solana [--limit 10]
    python -m meme_intelligence security <contract-address> --chain <chain>
    python -m meme_intelligence scan --network solana [--top 5]

``scan`` runs the Layer 1 -> Layer 2 flow (Spec Part 2, Section 4): discover
new pools, then security-screen the best candidates. The full continuous
controller (Part 22, Section 2) replaces this CLI as the primary entry
point in a later phase.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from meme_intelligence.ai.report_generator import build_report
from meme_intelligence.alerts.notification_engine import ConsoleSink, NotificationEngine
from meme_intelligence.analyzers.onchain_analyzer import OnChainAnalyzer, derive_onchain_profile
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.collectors.market_data import CoinGeckoClient
from meme_intelligence.core.enums import MarketRegime
from meme_intelligence.database.storage import Storage
from meme_intelligence.trading.trade_planner import TradePlanner
from meme_intelligence.workflow.controller import ContinuousScanner
from meme_intelligence.workflow.daily_routine import DailyRoutine
from meme_intelligence.workflow.pipeline import ResearchPipeline
from meme_intelligence.collectors.market_data import DexScreenerClient, GeckoTerminalClient
from meme_intelligence.collectors.security_data import GoPlusClient
from meme_intelligence.config.settings import Settings, get_settings
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError, InsufficientDataError
from meme_intelligence.core.logging_setup import setup_logging
from meme_intelligence.core.models import DexPair
from meme_intelligence.core.rate_limiter import RateLimiter
from meme_intelligence.scanners.discovery import DiscoveryEngine, scan_new_pools


def _shared_collector_kwargs(settings: Settings) -> dict:
    return {
        "cache": TTLCache(settings.http.cache_max_entries, settings.http.cache_ttl_seconds),
        "timeout_seconds": settings.http.timeout_seconds,
        "retry_attempts": settings.http.retry_attempts,
        "retry_base_delay": settings.http.retry_base_delay,
        "retry_max_delay": settings.http.retry_max_delay,
    }


def build_dexscreener(settings: Settings) -> DexScreenerClient:
    return DexScreenerClient(
        base_url=settings.providers.dexscreener_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.dexscreener_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )


def build_geckoterminal(settings: Settings) -> GeckoTerminalClient:
    return GeckoTerminalClient(
        base_url=settings.providers.geckoterminal_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.geckoterminal_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )


def build_goplus(settings: Settings) -> GoPlusClient:
    return GoPlusClient(
        base_url=settings.providers.goplus_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.goplus_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )


def build_coingecko(settings: Settings) -> CoinGeckoClient:
    return CoinGeckoClient(
        base_url=settings.providers.coingecko_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.coingecko_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )


def _format_pair(pair: DexPair) -> str:
    def money(value: float | None) -> str:
        return f"${value:,.0f}" if value is not None else "unknown"

    symbol = pair.base_token.symbol or "?"
    price = f"${pair.price_usd:.8f}" if pair.price_usd is not None else "unknown"
    created = pair.pair_created_at.strftime("%Y-%m-%d %H:%M") if pair.pair_created_at else "unknown"
    return (
        f"{symbol:>10} | {pair.chain:<8} | price {price:>15} | "
        f"liq {money(pair.liquidity_usd):>12} | vol24h {money(pair.volume_24h):>12} | "
        f"created {created}"
    )


async def _cmd_search(args, settings) -> int:
    async with build_dexscreener(settings) as client:
        pairs = await client.search_pairs(args.query)
    return _print_pairs(pairs, args.limit)


async def _cmd_token(args, settings) -> int:
    async with build_dexscreener(settings) as client:
        pairs = await client.get_token_pairs(args.address, chain=args.chain)
    return _print_pairs(pairs, args.limit)


def _print_pairs(pairs: list[DexPair], limit: int) -> int:
    if not pairs:
        print("No pairs found.")
        return 1
    pairs.sort(key=lambda p: p.liquidity_usd or 0.0, reverse=True)
    print(f"Found {len(pairs)} pair(s); top {min(len(pairs), limit)} by liquidity:\n")
    for pair in pairs[:limit]:
        print(_format_pair(pair))
    return 0


async def _cmd_discover(args, settings) -> int:
    engine = DiscoveryEngine(settings.discovery)
    async with build_geckoterminal(settings) as client:
        candidates, rejected = await scan_new_pools(client, engine, args.network)

    print(f"Discovery scan across {', '.join(args.network)}: "
          f"{len(candidates)} candidate(s), {len(rejected)} rejected.\n")
    for candidate in candidates[: args.limit]:
        print(f"score {candidate.discovery_score:5.1f} | {_format_pair(candidate.pair)}")
        print(f"{'':>12}  {'; '.join(candidate.reasons)}")
    if args.show_rejected:
        print("\nRejected:")
        for r in rejected[: args.limit]:
            symbol = r.pair.base_token.symbol or r.pair.base_token.address[:8]
            print(f"  {symbol:>10} | {r.reason}")
    return 0


async def _cmd_security(args, settings) -> int:
    analyzer = SecurityAnalyzer(settings.security, settings.security_weights)
    async with build_goplus(settings) as goplus, build_dexscreener(settings) as dex:
        try:
            profile = await goplus.get_token_security(args.chain, args.address)
        except CollectorError as exc:
            print(f"Security data unavailable: {exc}")
            return 1
        if profile is None:
            print(f"GoPlus has no security data for {args.address} on {args.chain}.")
            return 1
        market = None
        try:
            pairs = await dex.get_token_pairs(args.address)
            if pairs:
                market = max(pairs, key=lambda p: p.liquidity_usd or 0.0)
        except CollectorError as exc:
            print(f"(market data unavailable, assessing without liquidity depth: {exc})")

    try:
        assessment = analyzer.assess(profile, market)
    except InsufficientDataError as exc:
        print(f"Cannot assess: {exc}")
        return 1
    print(assessment.summary())
    return 0 if not assessment.is_destructive else 2


async def _cmd_scan(args, settings) -> int:
    """Layer 1 discovery -> Layer 2 security -> Layer 3 on-chain intelligence."""
    engine = DiscoveryEngine(settings.discovery)
    security_analyzer = SecurityAnalyzer(settings.security, settings.security_weights)
    onchain_analyzer = OnChainAnalyzer(settings.onchain, settings.onchain_weights)

    async with build_geckoterminal(settings) as gecko, build_goplus(settings) as goplus:
        candidates, rejected = await scan_new_pools(gecko, engine, args.network)
        print(f"Layer 1 discovery: {len(candidates)} candidate(s), {len(rejected)} rejected.\n")

        survivors = 0
        for candidate in candidates[: args.top]:
            pair = candidate.pair
            symbol = pair.base_token.symbol or pair.base_token.address[:8]
            print(f"--- {symbol} ({pair.chain}) discovery={candidate.discovery_score:.1f} ---")

            security_profile = None
            try:
                security_profile = await goplus.get_token_security(pair.chain, pair.base_token.address)
            except CollectorError as exc:
                print(f"  security data unavailable: {exc}")

            if security_profile is None:
                print("  security: no data (provider has not indexed this token yet)\n")
                continue
            try:
                security = security_analyzer.assess(security_profile, pair)
            except InsufficientDataError as exc:
                print(f"  security: cannot assess ({exc})\n")
                continue
            print("  " + security.summary().replace("\n", "\n  "))

            # Layer 3 (partial): on-chain intelligence derived from data
            # already in hand — no additional API calls (Rule 10).
            try:
                onchain = onchain_analyzer.assess(derive_onchain_profile(pair, security_profile))
                print("  " + onchain.summary().replace("\n", "\n  ") + "\n")
            except InsufficientDataError:
                print("  on-chain: insufficient data\n")

            if not security.is_destructive:
                survivors += 1

    print(f"Layer 2 security screen: {survivors}/{min(len(candidates), args.top)} "
          f"candidate(s) free of destructive risk.")
    return 0


async def _cmd_plan(args, settings) -> int:
    """Full research pass for one token, ending in a trade plan (Part 8)."""
    gathered, error = await _gather_assessments(args, settings)
    if error:
        print(error)
        return 1
    result, plan = gathered

    for section in (result.security, result.onchain, result.token,
                    result.momentum, result.risk, result.master):
        if section is not None:
            print(section.summary() + "\n")
    print(plan.render())
    return 0 if not result.security.is_destructive else 2


async def _gather_assessments(args, settings):
    """Shared research pass (via the pipeline) used by plan and report commands."""
    regime = MarketRegime(args.regime)
    async with build_dexscreener(settings) as dex, build_goplus(settings) as goplus:
        pairs = await dex.get_token_pairs(args.address, chain=args.chain)
        if not pairs:
            return None, f"No trading pairs found for {args.address}."
        pair = max(pairs, key=lambda p: p.liquidity_usd or 0.0)
        result = await ResearchPipeline(settings, goplus).analyze_pair(pair, regime=regime)

    if result is None:
        return None, (f"Security data unavailable for {args.address} on {pair.chain} "
                      "(chain unsupported or token not indexed yet).")

    plan = TradePlanner(settings.trading, settings.trade_weights).build_plan(
        pair, result.security, onchain=result.onchain, token=result.token, regime=regime,
    )
    return (result, plan), None


async def _cmd_report(args, settings) -> int:
    """Full canonical intelligence report (Part 12), persisted to the database."""
    gathered, error = await _gather_assessments(args, settings)
    if error:
        print(error)
        return 1
    result, plan = gathered

    report = build_report(
        result.pair, result.master, result.security,
        onchain=result.onchain, token=result.token, momentum=result.momentum,
        risk=result.risk, plan=plan,
    )
    print(report.text)

    with Storage(settings.database.path) as storage:
        storage.record_snapshot(result.master, source="report_cli")
    return 0 if not result.security.is_destructive else 2


async def _cmd_daily(args, settings) -> int:
    """Run the full daily research routine (Part 11) and print the report."""
    if args.network:
        # CLI overrides the configured scan networks for this run only
        import dataclasses as _dc
        settings = _dc.replace(
            settings, workflow=_dc.replace(settings.workflow, networks=",".join(args.network))
        )

    async with (
        build_geckoterminal(settings) as gecko,
        build_goplus(settings) as goplus,
        build_coingecko(settings) as coingecko,
        build_dexscreener(settings) as dex,
    ):
        with Storage(settings.database.path) as storage:
            routine = DailyRoutine(
                settings, storage,
                gecko_client=gecko, goplus_client=goplus,
                coingecko_client=coingecko, dexscreener_client=dex,
            )
            report = await routine.run()
    print(report.render())
    return 0


async def _cmd_monitor(args, settings) -> int:
    """Run the continuous scanning loop (Part 13). Ctrl-C stops gracefully."""
    import dataclasses as _dc
    workflow = settings.workflow
    if args.network:
        workflow = _dc.replace(workflow, networks=",".join(args.network))
    if args.interval:
        workflow = _dc.replace(workflow, monitor_interval_seconds=args.interval)
    settings = _dc.replace(settings, workflow=workflow)

    async with build_geckoterminal(settings) as gecko, build_goplus(settings) as goplus:
        with Storage(settings.database.path) as storage:
            notifier = NotificationEngine([ConsoleSink()], settings.alert_engine)
            scanner = ContinuousScanner(
                settings, storage, notifier,
                gecko_client=gecko, goplus_client=goplus,
                regime=MarketRegime(args.regime),
            )
            try:
                history = await scanner.run(max_cycles=args.cycles)
            except KeyboardInterrupt:
                scanner.request_stop()
                history = []

    analyzed = sum(s.analyzed for s in history)
    alerts = sum(len(s.alerts) for s in history)
    print(f"\nMonitor finished: {len(history)} cycle(s), {analyzed} token(s) analyzed, "
          f"{alerts} alert(s) dispatched.")
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    handler = {
        "search": _cmd_search,
        "token": _cmd_token,
        "discover": _cmd_discover,
        "security": _cmd_security,
        "scan": _cmd_scan,
        "plan": _cmd_plan,
        "report": _cmd_report,
        "daily": _cmd_daily,
        "monitor": _cmd_monitor,
    }[args.command]
    return await handler(args, settings)


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

    discover = sub.add_parser("discover", help="scan for newly launched pools")
    discover.add_argument("--network", action="append", default=None,
                          help="network id (repeatable); default: solana")
    discover.add_argument("--limit", type=int, default=10)
    discover.add_argument("--show-rejected", action="store_true")

    security = sub.add_parser("security", help="run a security assessment for one token")
    security.add_argument("address")
    security.add_argument("--chain", required=True, help="chain id (e.g. solana, ethereum, base)")

    scan = sub.add_parser("scan", help="discover new pools, then security-screen the best")
    scan.add_argument("--network", action="append", default=None,
                      help="network id (repeatable); default: solana")
    scan.add_argument("--top", type=int, default=5, help="candidates to security-screen")

    plan = sub.add_parser("plan", help="full research pass + trade plan for one token")
    plan.add_argument("address")
    plan.add_argument("--chain", default=None, help="filter to one chain id (e.g. solana)")
    plan.add_argument("--regime", default="unknown",
                      choices=["bull", "neutral", "bear", "unknown"],
                      help="current market regime (Part 8 Section 10)")

    report = sub.add_parser("report", help="canonical intelligence report for one token")
    report.add_argument("address")
    report.add_argument("--chain", default=None, help="filter to one chain id (e.g. solana)")
    report.add_argument("--regime", default="unknown",
                        choices=["bull", "neutral", "bear", "unknown"])

    daily = sub.add_parser("daily", help="run the full daily research routine (Part 11)")
    daily.add_argument("--network", action="append", default=None,
                       help="network id (repeatable); default from settings")

    monitor = sub.add_parser("monitor", help="continuous scanning loop (Part 13)")
    monitor.add_argument("--network", action="append", default=None,
                         help="network id (repeatable); default from settings")
    monitor.add_argument("--cycles", type=int, default=None,
                         help="stop after N cycles (default: run until Ctrl-C)")
    monitor.add_argument("--interval", type=float, default=None,
                         help="seconds between cycles (default from settings)")
    monitor.add_argument("--regime", default="unknown",
                         choices=["bull", "neutral", "bear", "unknown"])

    args = parser.parse_args(argv)
    if getattr(args, "network", None) is None and args.command in ("discover", "scan"):
        args.network = ["solana"]
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
