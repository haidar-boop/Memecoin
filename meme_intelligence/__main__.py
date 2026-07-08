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

from meme_intelligence.ai.comparison import render_comparison
from meme_intelligence.ai.reasoning import build_judgment_service
from meme_intelligence.ai.report_generator import build_report
from meme_intelligence.alerts.notification_engine import ConsoleSink, NotificationEngine
from meme_intelligence.analyzers.onchain_analyzer import OnChainAnalyzer, derive_onchain_profile
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.analyzers.wallet_intelligence import sightings_from_assessment
from meme_intelligence.collectors.market_data import CoinGeckoClient
from meme_intelligence.collectors.market_service import MarketDataService
from meme_intelligence.collectors.wallet_data import (
    BirdeyeClient,
    HeliusClient,
    WalletDataService,
)
from meme_intelligence.core.enums import MarketRegime, ResearchMode
from meme_intelligence.database.storage import Storage
from meme_intelligence.trading.trade_planner import TradePlanner
from meme_intelligence.workflow.controller import ContinuousScanner
from meme_intelligence.workflow.daily_routine import DailyRoutine
from meme_intelligence.workflow.pipeline import ResearchPipeline
from meme_intelligence.workflow.watchlist_review import review_entries
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


def build_market_service(settings: Settings, *clients) -> MarketDataService:
    """Failover-pooled market data across the given provider clients (Part 15)."""
    return MarketDataService(
        list(clients),
        failure_threshold=settings.providers.failure_threshold,
        cooldown_seconds=settings.providers.cooldown_seconds,
    )


def build_wallet_service(settings: Settings) -> WalletDataService | None:
    """Wallet intelligence service, or None when no keys are configured (Part 17)."""
    helius = birdeye = None
    if settings.helius_api_key:
        helius = HeliusClient(
            settings.helius_api_key,
            rpc_url=settings.providers.helius_rpc_url,
            api_url=settings.providers.helius_api_url,
            rate_limiter=RateLimiter.per_minute(settings.providers.helius_requests_per_minute),
            **_shared_collector_kwargs(settings),
        )
    if settings.birdeye_api_key:
        birdeye = BirdeyeClient(
            settings.birdeye_api_key,
            base_url=settings.providers.birdeye_base_url,
            rate_limiter=RateLimiter.per_minute(settings.providers.birdeye_requests_per_minute),
            **_shared_collector_kwargs(settings),
        )
    if helius is None and birdeye is None:
        return None
    return WalletDataService(
        helius, birdeye,
        top_holders_limit=settings.wallet.top_holders_limit,
        recent_trades_limit=settings.wallet.recent_trades_limit,
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

    for section in (result.security, result.onchain, result.token, result.foundation,
                    result.narrative, result.momentum, result.risk,
                    result.ai_judgment, result.master):
        if section is not None:
            print(section.summary() + "\n")
    print(plan.render())
    return 0 if not result.security.is_destructive else 2


async def _gather_assessments(args, settings):
    """Shared research pass (via the pipeline) used by plan and report commands."""
    regime = MarketRegime(args.regime)
    wallet_service = build_wallet_service(settings)
    ai_service = None
    if getattr(args, "ai", False):
        ai_service = build_judgment_service(settings)
        if ai_service is None:
            return None, ("--ai requires MEMEINTEL_ANTHROPIC_API_KEY "
                          "(set it in the environment or .env).")
    try:
        async with (
            build_dexscreener(settings) as dex,
            build_geckoterminal(settings) as gecko,
            build_goplus(settings) as goplus,
        ):
            service = build_market_service(settings, dex, gecko)
            pair = await service.get_best_pair(args.address, chain=args.chain)
            if pair is None:
                return None, f"No trading pairs found for {args.address}."
            pipeline = ResearchPipeline(settings, goplus, wallet_service=wallet_service,
                                        ai_service=ai_service)
            result = await pipeline.analyze_pair(
                pair, regime=regime,
                research_mode=ResearchMode(getattr(args, "ai_mode", "standard")),
            )
    finally:
        if wallet_service is not None:
            await wallet_service.close()

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
        onchain=result.onchain, token=result.token, narrative=result.narrative,
        momentum=result.momentum, risk=result.risk, plan=plan,
    )
    print(report.text)
    if result.foundation is not None:
        print("\n" + result.foundation.summary())
    if result.wallet is not None:
        print("\n" + result.wallet.summary())
    if result.ai_judgment is not None:
        print("\n" + result.ai_judgment.summary())

    with Storage(settings.database.path) as storage:
        storage.record_snapshot(result.master, source="report_cli")
        if result.wallet is not None:
            # Sightings feed wallet track records for Part 24's learning loop.
            storage.record_wallet_sightings(
                result.pair.base_token, sightings_from_assessment(result.wallet),
            )
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
                coingecko_client=coingecko,
                # Failover-pooled service; exposes the same get_token_pairs API.
                dexscreener_client=build_market_service(settings, dex, gecko),
            )
            report = await routine.run()
    print(report.render())
    return 0


async def _cmd_quick(args, settings) -> int:
    """Level 1 fast scan (Part 16, Section 6): compact card, red flags never skipped."""
    gathered, error = await _gather_assessments(args, settings)
    if error:
        print(error)
        return 1
    result, plan = gathered
    pair, security, master = result.pair, result.security, result.master

    def money(value):
        return f"${value:,.0f}" if value is not None else "unknown"

    symbol = pair.base_token.symbol or pair.base_token.address[:8]
    print(f"QUICK SCAN — {symbol} ({pair.chain})  [Level 1: fast filter, not confirmation]")
    print(f"  mcap {money(pair.market_cap)} | liq {money(pair.liquidity_usd)} | "
          f"vol24h {money(pair.volume_24h)}")
    print(f"  Security: {security.overall_score:.0f}/100 [{security.band}] "
          f"tier={security.tier.value}")

    # Red flags are NEVER skipped for speed (Part 16, Section 6).
    if security.destructive_findings:
        for finding in security.destructive_findings:
            print(f"  RED FLAG: {finding.message}")
    else:
        print("  Red flags: none confirmed "
              f"({len(security.unknown_fields)} security fact(s) still unverified)")

    top_risks = [f.message for f in security.findings[:3]]
    if result.risk:
        top_risks.extend(r for r in result.risk.main_risks[:2] if r not in top_risks)
    for risk in top_risks[:3]:
        print(f"  Risk: {risk}")

    if result.momentum:
        print(f"  Momentum: {result.momentum.overall_score:.0f}/100 "
              f"zone={result.momentum.entry_zone.value} "
              f"action={result.momentum.preferred_action.value}")
    print(f"  Opportunity score: {master.final_score:.0f}/100 "
          f"(evidence coverage {master.coverage:.0%})")
    print(f"  Recommendation: {master.classification.value.upper()} — "
          f"run a full report before acting; quick scan is a filter, not a decision.")
    return 0 if not security.is_destructive else 2


async def _cmd_compare(args, settings) -> int:
    """Compare command (Part 16, Section 7): table + explicit ranking."""
    targets = []
    for raw in args.addresses:
        if ":" in raw:  # per-token chain override: "ethereum:0xabc..."
            chain, address = raw.split(":", 1)
            targets.append((address, chain))
        else:
            targets.append((raw, args.chain))

    results = []
    regime = MarketRegime(args.regime)
    async with (
        build_dexscreener(settings) as dex,
        build_geckoterminal(settings) as gecko,
        build_goplus(settings) as goplus,
    ):
        service = build_market_service(settings, dex, gecko)
        pipeline = ResearchPipeline(settings, goplus)
        for address, chain in targets:
            pair = await service.get_best_pair(address, chain=chain)
            if pair is None:
                print(f"skipping {address}: no trading pairs found")
                continue
            result = await pipeline.analyze_pair(pair, regime=regime)
            if result is None:
                print(f"skipping {address}: security data unavailable")
                continue
            results.append(result)

    if len(results) < 2:
        print("Need at least two analyzable tokens to compare.")
        return 1
    print(render_comparison(results))
    return 0


async def _cmd_watchlist(args, settings) -> int:
    """Watchlist command (Part 16, Section 8): show tracked tokens; --refresh re-scores."""
    with Storage(settings.database.path) as storage:
        if args.refresh:
            async with (
                build_dexscreener(settings) as dex,
                build_geckoterminal(settings) as gecko,
                build_goplus(settings) as goplus,
            ):
                service = build_market_service(settings, dex, gecko)
                pipeline = ResearchPipeline(settings, goplus)
                changes = await review_entries(
                    storage, service, pipeline,
                    limit=settings.workflow.watchlist_review_limit,
                )
            print(f"Refreshed: {len(changes)} change(s)")
            for change in changes:
                symbol = change.token.symbol or change.token.address[:8]
                print(f"  - {symbol}: {change.change} ({change.detail})")
            print()

        entries = storage.get_watchlist(include_archived=args.include_archived)
        if not entries:
            print("Watchlist is empty. Run `daily` or `monitor` to populate it.")
            return 0
        print(f"WATCHLIST ({len(entries)} tracked)")
        for entry in entries:
            symbol = entry.token.symbol or entry.token.address[:8]
            score = f"{entry.last_score:.0f}" if entry.last_score is not None else "?"
            print(f"  [{entry.tier.value:>22}] {symbol:>10} ({entry.token.chain}) "
                  f"score={score} class={entry.last_classification or '?'} "
                  f"updated={entry.updated_at.strftime('%m-%d %H:%M')}")
            if entry.thesis:
                print(f"{'':>26}thesis: {entry.thesis}")
    return 0


async def _cmd_wallets(args, settings) -> int:
    """Smart money & whale intelligence for one token (Part 17)."""
    wallet_service = build_wallet_service(settings)
    if wallet_service is None:
        print("No wallet-data API keys configured. Set MEMEINTEL_HELIUS_API_KEY and/or "
              "MEMEINTEL_BIRDEYE_API_KEY (see .env.example).")
        return 1

    from meme_intelligence.analyzers.wallet_intelligence import WalletIntelligenceAnalyzer
    from meme_intelligence.core.models import TokenIdentity

    async with (
        wallet_service,
        build_dexscreener(settings) as dex,
        build_geckoterminal(settings) as gecko,
    ):
        service = build_market_service(settings, dex, gecko)
        pair = await service.get_best_pair(args.address, chain=args.chain or "solana")
        token = pair.base_token if pair else TokenIdentity(
            chain=args.chain or "solana", address=args.address)
        data = await wallet_service.gather(token)

    if not data.sources:
        print(f"No wallet data available for {args.address} (providers returned nothing).")
        return 1

    analyzer = WalletIntelligenceAnalyzer(settings.wallet, settings.smart_money_weights)
    try:
        assessment = analyzer.assess(data, pair)
    except InsufficientDataError as exc:
        print(f"Cannot assess: {exc}")
        return 1

    print(assessment.summary())
    with Storage(settings.database.path) as storage:
        recorded = storage.record_wallet_sightings(
            token, sightings_from_assessment(assessment))
    print(f"\n({recorded} wallet sighting(s) recorded for future track-record building)")
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

    async with (
        build_geckoterminal(settings) as gecko,
        build_goplus(settings) as goplus,
        build_dexscreener(settings) as dex,
    ):
        with Storage(settings.database.path) as storage:
            notifier = NotificationEngine([ConsoleSink()], settings.alert_engine)
            scanner = ContinuousScanner(
                settings, storage, notifier,
                gecko_client=gecko, goplus_client=goplus,
                market_service=build_market_service(settings, dex, gecko),
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
        "quick": _cmd_quick,
        "compare": _cmd_compare,
        "watchlist": _cmd_watchlist,
        "wallets": _cmd_wallets,
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
    plan.add_argument("--ai", action="store_true",
                      help="run the AI reasoning layer (needs MEMEINTEL_ANTHROPIC_API_KEY)")
    plan.add_argument("--ai-mode", default="standard", dest="ai_mode",
                      choices=[m.value for m in ResearchMode],
                      help="AI research mode (Part 23 Section 10)")
    plan.add_argument("--chain", default=None, help="filter to one chain id (e.g. solana)")
    plan.add_argument("--regime", default="unknown",
                      choices=["bull", "neutral", "bear", "unknown"],
                      help="current market regime (Part 8 Section 10)")

    report = sub.add_parser("report", help="canonical intelligence report for one token")
    report.add_argument("address")
    report.add_argument("--ai", action="store_true",
                        help="run the AI reasoning layer (needs MEMEINTEL_ANTHROPIC_API_KEY)")
    report.add_argument("--ai-mode", default="standard", dest="ai_mode",
                        choices=[m.value for m in ResearchMode],
                        help="AI research mode (Part 23 Section 10)")
    report.add_argument("--chain", default=None, help="filter to one chain id (e.g. solana)")
    report.add_argument("--regime", default="unknown",
                        choices=["bull", "neutral", "bear", "unknown"])

    quick = sub.add_parser("quick", help="Level 1 fast scan: compact card (Part 16)")
    quick.add_argument("address")
    quick.add_argument("--chain", default=None)
    quick.add_argument("--regime", default="unknown",
                       choices=["bull", "neutral", "bear", "unknown"])

    compare = sub.add_parser("compare", help="compare tokens: table + ranking (Part 16)")
    compare.add_argument("addresses", nargs="+",
                         help="two or more addresses; prefix with chain: for per-token chains")
    compare.add_argument("--chain", default=None, help="default chain for unprefixed addresses")
    compare.add_argument("--regime", default="unknown",
                         choices=["bull", "neutral", "bear", "unknown"])

    watchlist = sub.add_parser("watchlist", help="show tracked tokens; --refresh re-scores")
    watchlist.add_argument("--refresh", action="store_true")
    watchlist.add_argument("--include-archived", action="store_true")

    wallets = sub.add_parser("wallets", help="smart money & whale intelligence (Part 17)")
    wallets.add_argument("address")
    wallets.add_argument("--chain", default="solana",
                         help="chain id (wallet intelligence is Solana-first)")

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
