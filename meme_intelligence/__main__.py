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
from meme_intelligence.alerts.notification_engine import (
    AlertEvent,
    ConsoleSink,
    NotificationEngine,
)
from meme_intelligence.alerts.sinks import DiscordSink, TelegramSink, parse_routes
from meme_intelligence.alerts.telegram_commands import (
    CommandContext,
    TelegramCommandListener,
)
from meme_intelligence.analyzers.onchain_analyzer import OnChainAnalyzer, derive_onchain_profile
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.analyzers.wallet_intelligence import sightings_from_assessment
from meme_intelligence.collectors.market_data import CoinGeckoClient
from meme_intelligence.collectors.market_service import MarketDataService
from meme_intelligence.collectors.jupiter_data import JupiterClient
from meme_intelligence.collectors.wallet_data import (
    BirdeyeClient,
    HeliusClient,
    WalletDataService,
)
from meme_intelligence.core.enums import AlertPriority, MarketRegime, ResearchMode
from meme_intelligence.database.storage import Storage
from meme_intelligence.trading.trade_planner import TradePlanner
from meme_intelligence.workflow.controller import ContinuousScanner
from meme_intelligence.workflow.daily_routine import DailyRoutine
from meme_intelligence.workflow.pipeline import ResearchPipeline
from meme_intelligence.workflow.watchlist_review import review_entries
from meme_intelligence.collectors.market_data import DexScreenerClient, GeckoTerminalClient
from meme_intelligence.collectors.pumpfun import PumpFunFrontendClient, PumpPortalClient
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
        api_key=settings.coingecko_api_key,
        base_url=settings.providers.coingecko_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.coingecko_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )


def build_pumpportal(settings: Settings) -> PumpPortalClient:
    """Free PumpPortal launch-event stream (Part 32.5 Section 3)."""
    return PumpPortalClient(settings.providers.pumpportal_ws_url)


def build_pumpfun_frontend(settings: Settings) -> PumpFunFrontendClient:
    return PumpFunFrontendClient(
        base_url=settings.providers.pumpfun_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.pumpfun_requests_per_minute),
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


def build_jupiter(settings: Settings) -> JupiterClient | None:
    """Live round-trip sell-test client, or None when no key is configured (Project 1)."""
    if not settings.jupiter_api_key:
        return None
    return JupiterClient(
        settings.jupiter_api_key,
        base_url=settings.providers.jupiter_base_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.jupiter_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )


def build_learning_service(settings: Settings):
    """Construct the self-learning mind layer (Section 10).

    Imported lazily so the numpy/faiss/lightgbm stack is loaded only when the
    ``mind`` command or the opt-in monitor hook actually needs it — every other
    CLI command stays fast and dependency-light.
    """
    from meme_intelligence.learning.service import LearningService

    return LearningService(settings)


def build_executor(settings: Settings, storage, jupiter_client):
    """Pick the trade executor (Project 6): live when explicitly enabled AND a
    trading key + Jupiter client + Helius RPC are all present; dry-run
    otherwise. Returns (executor, rpc_client_or_None) — the RPC client is a
    BaseCollector the caller must close."""
    from meme_intelligence.trading.execution import DryRunExecutor

    ex = settings.execution
    if not (ex.live_enabled and settings.trading_private_key):
        return DryRunExecutor(storage), None
    if jupiter_client is None or not settings.helius_api_key:
        print("Note: MEMEINTEL_EXECUTION_LIVE_ENABLED is on but a trading key, "
              "Jupiter key, or Helius key is missing — buy/dump run in DRY RUN.")
        return DryRunExecutor(storage), None
    from meme_intelligence.trading.execution import LiveExecutor
    from meme_intelligence.trading.solana_rpc import SolanaRpcClient

    rpc = SolanaRpcClient(
        settings.helius_api_key, rpc_url=settings.providers.helius_rpc_url,
        rate_limiter=RateLimiter.per_minute(settings.providers.helius_requests_per_minute),
        **_shared_collector_kwargs(settings),
    )
    try:
        executor = LiveExecutor(
            storage, jupiter_client=jupiter_client, rpc_client=rpc,
            private_key_base58=settings.trading_private_key,
            max_buy_sol=ex.max_buy_sol, slippage_bps=ex.slippage_bps,
            priority_fee_max_lamports=ex.priority_fee_max_lamports,
            confirm_timeout_seconds=ex.confirm_timeout_seconds,
        )
    except ValueError as exc:
        print(f"Note: live trading disabled — {exc}. Buy/dump run in DRY RUN.")
        return DryRunExecutor(storage), rpc
    print(f"LIVE TRADING ARMED — trading wallet {executor.wallet_address}. "
          f"Per-trade cap {ex.max_buy_sol:g} SOL.")
    return executor, rpc


def build_sinks(settings: Settings) -> list:
    """Console always; Telegram/Discord activate when their secrets exist (Part 29)."""
    sinks: list = [ConsoleSink()]
    min_priority = AlertPriority(settings.alert_delivery.external_min_priority)
    shared = {
        "rate_limiter": RateLimiter.per_minute(settings.alert_delivery.requests_per_minute),
        "timeout_seconds": settings.http.timeout_seconds,
        "retry_attempts": settings.http.retry_attempts,
        "min_priority": min_priority,
    }
    if settings.telegram_bot_token and settings.telegram_chat_id:
        # Buy/Dump buttons appear only when the operator turned them on
        # (Project 6); the amounts are the configured presets.
        presets = (settings.execution.buy_preset_list()
                   if settings.execution.buy_button_enabled else ())
        sinks.append(TelegramSink(
            settings.telegram_bot_token, settings.telegram_chat_id,
            routes=parse_routes(settings.alert_delivery.telegram_routes),
            buy_presets_sol=presets,
            **shared,
        ))
    if settings.discord_webhook_url:
        sinks.append(DiscordSink(
            settings.discord_webhook_url,
            routes=parse_routes(settings.alert_delivery.discord_routes), **shared,
        ))
    return sinks


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
    jupiter_client = build_jupiter(settings)
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
            build_coingecko(settings) as coingecko,
        ):
            service = build_market_service(settings, dex, gecko)
            pair = await service.get_best_pair(args.address, chain=args.chain)
            if pair is None:
                return None, f"No trading pairs found for {args.address}."
            pipeline = ResearchPipeline(settings, goplus, wallet_service=wallet_service,
                                        jupiter_client=jupiter_client,
                                        community_client=coingecko, ai_service=ai_service)
            result = await pipeline.analyze_pair(
                pair, regime=regime,
                research_mode=ResearchMode(getattr(args, "ai_mode", "standard")),
            )
    finally:
        if wallet_service is not None:
            await wallet_service.close()
        if jupiter_client is not None:
            await jupiter_client.close()
        if ai_service is not None:
            # The AsyncAnthropic client wraps its own httpx AsyncClient —
            # close it like every other client this command opens (bug-hunt
            # finding: the monitor path already did; this path leaked it).
            await ai_service._client.close()

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
        storage.record_snapshot(
            result.master, source="report_cli",
            pair=result.pair, regime=args.regime,
            opportunity_rank=result.opportunity.score if result.opportunity else None)
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

    jupiter_client = build_jupiter(settings)
    try:
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
                    jupiter_client=jupiter_client,
                    coingecko_client=coingecko,
                    # Failover-pooled service; exposes the same get_token_pairs API.
                    dexscreener_client=build_market_service(settings, dex, gecko),
                )
                report = await routine.run()
    finally:
        if jupiter_client is not None:
            await jupiter_client.close()
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
    jupiter_client = build_jupiter(settings)
    try:
        async with (
            build_dexscreener(settings) as dex,
            build_geckoterminal(settings) as gecko,
            build_goplus(settings) as goplus,
        ):
            service = build_market_service(settings, dex, gecko)
            pipeline = ResearchPipeline(settings, goplus, jupiter_client=jupiter_client)
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
    finally:
        if jupiter_client is not None:
            await jupiter_client.close()

    if len(results) < 2:
        print("Need at least two analyzable tokens to compare.")
        return 1
    print(render_comparison(results))
    return 0


async def _cmd_backtest(args, settings) -> int:
    """Backtesting & self-improvement report (Part 24); --refresh measures
    due outcome windows (live price fetch for tokens without snapshots)."""
    from meme_intelligence.analytics.backtesting import (
        evaluate_predictions,
        failure_success_patterns,
        label_alert_outcomes,
        performance_metrics,
        refresh_outcomes,
        render_backtest_report,
        signal_performance,
        weight_experiments,
    )

    # When the mind layer is enabled, measured outcomes also resolve its coins
    # and fire instant learning (Section 1). Built lazily so the ML stack loads
    # only for users who opted in.
    learning_service = build_learning_service(settings) if settings.learning.enabled else None

    with Storage(settings.database.path) as storage:
        recorded = 0
        if args.refresh:
            async with (
                build_dexscreener(settings) as dex,
                build_geckoterminal(settings) as gecko,
            ):
                service = build_market_service(settings, dex, gecko)
                recorded = await refresh_outcomes(
                    storage, service, settings=settings.backtest,
                    learning_service=learning_service)
        else:
            recorded = await refresh_outcomes(
                storage, None, settings=settings.backtest,
                learning_service=learning_service)
        if learning_service is not None:
            learning_service.persist()

        labeled = label_alert_outcomes(storage, settings.backtest)
        verdicts = evaluate_predictions(storage, settings.backtest)
        if not verdicts:
            print("No predictions have measurable outcomes yet.\n"
                  "Keep `monitor` running (or re-run analyses later), then use\n"
                  "`backtest --refresh` to measure due windows against live prices.")
            return 0
        metrics = performance_metrics(verdicts, settings.backtest)
        print(render_backtest_report(
            metrics,
            signal_performance(verdicts, settings.backtest),
            weight_experiments(verdicts, settings.backtest),
            failure_success_patterns(verdicts, settings.backtest),
            alerts_labeled=labeled,
            outcomes_recorded=recorded,
        ))
    return 0


async def _cmd_alerts(args, settings) -> int:
    """Alert history + performance analysis (Part 29, Sections 11-12);
    --test sends a test alert through every configured sink."""
    if args.test:
        from meme_intelligence.core.models import TokenIdentity

        event = AlertEvent(
            priority=AlertPriority.HIGH,
            alert_type="high_priority_opportunity",
            token=TokenIdentity(chain="test", address="TestTokenAddress",
                                name="Delivery Test", symbol="TEST"),
            title="Test alert — delivery check for every configured sink",
            reasons=("this is a synthetic event; no token was analyzed",),
            scores={"master": 88.0},
            why_it_matters="If you can read this, alert delivery works.",
            monitoring=("nothing — this is only a delivery test",),
        )
        sinks = build_sinks(settings)
        engine = NotificationEngine(sinks, settings.alert_engine)
        await engine.dispatch([event])
        external = [type(s).__name__ for s in sinks[1:]]
        print(f"\nTest alert dispatched to: console"
              + (", " + ", ".join(external) if external else
                 " only (no Telegram/Discord secrets configured)"))
        for sink in sinks[1:]:
            await sink.close()
        return 0

    with Storage(settings.database.path) as storage:
        history = storage.alert_history(limit=args.limit)
        performance = storage.alert_performance()

    if not history:
        print("No alerts recorded yet — history accumulates while `monitor` runs.")
        return 0

    print(f"Last {len(history)} alert(s):\n")
    for row in history:
        symbol = row["symbol"] or row["address"][:8]
        score = f"{row['score_at_alert']:.0f}" if row["score_at_alert"] is not None else "?"
        print(f"  {row['created_at'][:16]}  [{row['priority']:>8}] "
              f"{row['alert_type']:<26} {symbol:<10} score {score}")

    if performance:
        print("\nAlert performance (score drift after alert, Part 29 Section 12):")
        print("  positive drift after opportunity alerts = useful signal;")
        print("  negative drift after risk alerts = the alert fired correctly.\n")
        for row in performance:
            drift = row["avg_score_drift"]
            print(f"  {row['alert_type']:<26} n={row['alerts_measured']:<4} "
                  f"avg drift {drift:+.1f}  improved {row['improved_count']}/{row['alerts_measured']}")
    else:
        print("\nNo re-assessments after alerts yet — performance analysis needs "
              "follow-up snapshots (keep `monitor` running).")
    return 0


async def _cmd_watchlist(args, settings) -> int:
    """Watchlist command (Part 16, Section 8): show tracked tokens; --refresh re-scores."""
    with Storage(settings.database.path) as storage:
        if args.refresh:
            jupiter_client = build_jupiter(settings)
            try:
                async with (
                    build_dexscreener(settings) as dex,
                    build_geckoterminal(settings) as gecko,
                    build_goplus(settings) as goplus,
                ):
                    service = build_market_service(settings, dex, gecko)
                    pipeline = ResearchPipeline(settings, goplus, jupiter_client=jupiter_client)
                    changes = await review_entries(
                        storage, service, pipeline,
                        limit=settings.workflow.watchlist_review_limit,
                    )
            finally:
                if jupiter_client is not None:
                    await jupiter_client.close()
            print(f"Refreshed: {len(changes)} change(s)")
            for change in changes:
                symbol = change.token.symbol or change.token.address[:8]
                print(f"  - {symbol}: {change.change} ({change.detail})")
            print()

        if getattr(args, "top", False):
            ranked = storage.top_opportunities(limit=args.limit)
            if not ranked:
                print("No ranked opportunities yet. Run `daily` or `monitor` to populate.")
                return 0
            print(f"TOP OPPORTUNITIES (Part 28 §5 ranking — top {len(ranked)})")
            for row in ranked:
                symbol = row["symbol"] or row["address"][:8]
                rank = f"{row['opportunity_rank']:.0f}" if row["opportunity_rank"] is not None else "?"
                score = f"{row['last_score']:.0f}" if row["last_score"] is not None else "?"
                print(f"  opportunity={rank:>3}  {symbol:>10} ({row['chain']}) "
                      f"[{row['tier']}] master={score} class={row['last_classification'] or '?'}")
                if row["thesis"]:
                    print(f"{'':>16}thesis: {row['thesis']}")
            return 0

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


# A rug-risk score at/above this (0-100) makes `mind evaluate` exit with code 2,
# mirroring the destructive-security convention of the `security` command.
_MIND_DESTRUCTIVE_RUG_SCORE = 70


def _fmt_opt(value, spec: str = ".2f") -> str:
    """Format an optional metric: 'n/a' when there is no data (Rule 8)."""
    return "n/a" if value is None else format(value, spec)


async def _cmd_mind(args, settings) -> int:
    """Self-learning mind layer: analog + model + rug verdict, or metrics (Section 10)."""
    service = build_learning_service(settings)

    if args.mind_command == "metrics":
        metrics = service.get_learning_metrics(persist=False)
        rug = metrics["rug"]
        print("Mind-layer self-evaluation metrics")
        print(f"  Resolved coins:        "
              f"{metrics.get('resolved_coins_total', metrics['resolved_count'])}")
        print(f"  Graded predictions:    {metrics['resolved_count']}")
        print(f"  Overall accuracy:      {_fmt_opt(metrics['overall_accuracy'])}")
        print(f"  Directional hit-rate:  {_fmt_opt(metrics['directional']['hit_rate'])} "
              f"(n={metrics['directional']['samples']})")
        print(f"  Rug precision/recall:  {_fmt_opt(rug['precision'])} / {_fmt_opt(rug['recall'])} "
              f"(F1 {_fmt_opt(rug['f1'])}, {rug['actual_rugs']} actual rug(s))")
        print(f"  Brier score:           {_fmt_opt(metrics['brier_score'])}")
        print(f"  Analog memory size:    {metrics['analog_memory_size']}")
        print(f"  Classifier ready:      {metrics['classifier_ready']}")
        return 0

    # mind evaluate <address>
    async with build_dexscreener(settings) as dex, build_geckoterminal(settings) as gecko:
        market = build_market_service(settings, dex, gecko)
        pair = await market.get_best_pair(args.address, chain=args.chain)
    if pair is None:
        print(f"No tradable pair found for {args.address} on {args.chain}.")
        return 1

    profile = None
    async with build_goplus(settings) as goplus:
        try:
            profile = await goplus.get_token_security(args.chain, args.address)
        except CollectorError as exc:
            print(f"(security data unavailable: {exc} — proceeding without it)")

    from datetime import datetime, timezone
    age_seconds = 0.0
    if pair.pair_created_at is not None:
        age_seconds = max(0.0, (datetime.now(timezone.utc)
                                - pair.pair_created_at).total_seconds())
    snapshot = {
        "age_seconds": age_seconds,
        "price_usd": pair.price_usd,
        "liquidity_usd": pair.liquidity_usd,
        "market_cap_usd": pair.market_cap,
        "volume_1h_usd": pair.volume_1h,
        "holder_count": profile.holder_count if profile else None,
        "buys": pair.buys_1h,
        "sells": pair.sells_1h,
        "top10_holder_percent": profile.top10_holder_percent if profile else None,
    }
    verdict = service.evaluate_coin(
        args.address, args.chain, [snapshot], security=profile,
        creator=profile.creator_address if profile else None)
    service.persist()

    probs = verdict["final_probabilities"]
    print(f"Mind-layer verdict for {args.address} ({args.chain})")
    print("  Outcome probabilities: " + "  ".join(
        f"{label}={probs.get(label, 0.0):.0%}" for label in ("pump", "flat", "dump", "rug")))
    print(f"  Rug risk score:        {verdict['rug_risk_score']}/100")
    print(f"  Rug signals fired:     {', '.join(verdict['rug_signals_fired']) or 'none'}")
    print(f"  Matched archetype:     {verdict['matched_archetype'] or 'none'}")
    print(f"  Novelty score:         {_fmt_opt(verdict['novelty_score'], '.2f')}")
    print(f"  Model confidence:      {verdict['model_confidence']:.0%}")
    print(f"  Learned sample size:   {verdict['sample_size']}")
    if verdict["nearest_analogs"]:
        print("  Nearest past analogs:")
        for analog in verdict["nearest_analogs"]:
            print(f"    - {analog['address']} ({analog['chain']}): "
                  f"{analog['similarity']:.0%} similar, resolved as {analog['resolved_as']}")
    else:
        print("  Nearest past analogs:  none yet (memory still cold)")
    # Treat a high rug-risk score as the destructive exit code, like `security`.
    return 2 if verdict["rug_risk_score"] >= _MIND_DESTRUCTIVE_RUG_SCORE else 0


async def _cmd_monitor(args, settings) -> int:
    """Run the continuous scanning loop (Part 13). Ctrl-C stops gracefully."""
    import contextlib
    import dataclasses as _dc
    workflow = settings.workflow
    if args.network:
        workflow = _dc.replace(workflow, networks=",".join(args.network))
    if args.interval:
        workflow = _dc.replace(workflow, monitor_interval_seconds=args.interval)
    settings = _dc.replace(settings, workflow=workflow)
    if args.pumpfun:
        settings = _dc.replace(
            settings, pumpfun=_dc.replace(settings.pumpfun, enable_in_monitor=True))
    if args.learn:
        settings = _dc.replace(
            settings, learning=_dc.replace(settings.learning, enable_in_monitor=True))

    async with contextlib.AsyncExitStack() as stack:
        gecko = await stack.enter_async_context(build_geckoterminal(settings))
        goplus = await stack.enter_async_context(build_goplus(settings))
        dex = await stack.enter_async_context(build_dexscreener(settings))
        coingecko = await stack.enter_async_context(build_coingecko(settings))
        # Pump.fun launch discovery (Part 32.5 Section 3) is opt-in: it adds
        # a WebSocket stream plus per-launch traction rechecks (Rule 11).
        pumpportal = pumpfun = None
        if settings.pumpfun.enable_in_monitor:
            pumpportal = await stack.enter_async_context(build_pumpportal(settings))
            pumpfun = await stack.enter_async_context(build_pumpfun_frontend(settings))
        # Metered layers (Parts 17/23) join the loop only when their
        # enable_in_monitor flag is set AND their keys exist; a set flag
        # with missing keys is reported, not silently ignored (Rule 13).
        wallet_service = ai_service = None
        if settings.wallet.enable_in_monitor:
            wallet_service = build_wallet_service(settings)
            if wallet_service is None:
                print("Note: MEMEINTEL_WALLET_ENABLE_IN_MONITOR is on but no "
                      "MEMEINTEL_HELIUS_API_KEY / MEMEINTEL_BIRDEYE_API_KEY is set — "
                      "smart-money analysis stays off.")
            else:
                stack.push_async_callback(wallet_service.close)
        if settings.ai.enable_in_monitor or settings.ai.verify_opportunities:
            ai_service = build_judgment_service(settings)
            if ai_service is None and settings.ai.enable_in_monitor:
                # verify_opportunities is on by default, so only complain
                # when the user explicitly asked for per-token judging.
                print("Note: MEMEINTEL_AI_ENABLE_IN_MONITOR is on but "
                      "MEMEINTEL_ANTHROPIC_API_KEY is not set — AI judgments stay off.")
            elif ai_service is not None:
                # AsyncAnthropic wraps its own httpx client; close it on
                # shutdown like every other HTTP client this command opens.
                stack.push_async_callback(ai_service._client.close)

        # Self-learning mind layer (Section 10): opt-in via --learn or
        # MEMEINTEL_LEARNING_ENABLE_IN_MONITOR. Its state is flushed to disk on
        # shutdown so learning compounds across restarts (Rule 7).
        learning_service = None
        if settings.learning.enable_in_monitor:
            learning_service = build_learning_service(settings)
            stack.callback(learning_service.persist)

        alert_sinks = build_sinks(settings)
        # Telegram/DiscordSink each hold an aiohttp session (BaseCollector);
        # ConsoleSink (always sinks[0]) doesn't and has no close(). Without
        # this, the monitor's session leaked for the process lifetime —
        # harmless at hard process exit, but real for --cycles runs or any
        # future long-lived host of this command (bug-hunt finding).
        for sink in alert_sinks[1:]:
            stack.push_async_callback(sink.close)

        # Live Jupiter round-trip sell test (Project 1): joins the loop
        # whenever a key is configured — same "no key = feature off"
        # pattern as the other keyed collectors.
        jupiter_client = build_jupiter(settings)
        if jupiter_client is not None:
            stack.push_async_callback(jupiter_client.close)

        with Storage(settings.database.path) as storage:
            notifier = NotificationEngine(alert_sinks, settings.alert_engine)
            scanner = ContinuousScanner(
                settings, storage, notifier,
                gecko_client=gecko, goplus_client=goplus,
                jupiter_client=jupiter_client,
                market_service=build_market_service(settings, dex, gecko),
                community_client=coingecko,
                pumpportal_client=pumpportal,
                pumpfun_client=pumpfun,
                wallet_service=wallet_service,
                ai_service=ai_service,
                learning_service=learning_service,
                regime=MarketRegime(args.regime),
            )
            # Two-way Telegram control (Project 2): opt-in via
            # MEMEINTEL_TELEGRAM_COMMANDS_ENABLED; needs the same bot
            # secrets the alert sink uses. Built AFTER the scanner because
            # its command context wraps the scanner's public methods.
            if settings.telegram_commands.enabled:
                if settings.telegram_bot_token and settings.telegram_chat_id:
                    executor, exec_rpc = build_executor(settings, storage, jupiter_client)
                    if exec_rpc is not None:
                        stack.push_async_callback(exec_rpc.close)
                    context = CommandContext(
                        storage=storage,
                        settings=settings,
                        status_provider=scanner.status_snapshot,
                        check_runner=scanner.check_token,
                        learning_service=learning_service,
                        executor=executor,
                    )
                    listener = TelegramCommandListener(
                        settings.telegram_bot_token, settings.telegram_chat_id,
                        context,
                        poll_timeout_seconds=settings.telegram_commands.poll_timeout_seconds,
                        idle_delay_seconds=settings.telegram_commands.idle_delay_seconds,
                        error_backoff_max_seconds=(
                            settings.telegram_commands.error_backoff_max_seconds),
                        rate_limiter=RateLimiter.per_minute(120.0),
                    )
                    scanner.set_telegram_listener(listener)
                    stack.push_async_callback(listener.close)
                    stack.push_async_callback(listener.stop)
                else:
                    print("Note: MEMEINTEL_TELEGRAM_COMMANDS_ENABLED is on but "
                          "MEMEINTEL_TELEGRAM_BOT_TOKEN / MEMEINTEL_TELEGRAM_CHAT_ID "
                          "is not set — Telegram commands stay off.")
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
        "alerts": _cmd_alerts,
        "backtest": _cmd_backtest,
        "wallets": _cmd_wallets,
        "daily": _cmd_daily,
        "monitor": _cmd_monitor,
        "mind": _cmd_mind,
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

    backtest = sub.add_parser("backtest",
                              help="prediction accuracy + self-improvement report (Part 24)")
    backtest.add_argument("--refresh", action="store_true",
                          help="measure due outcome windows via live market data")

    alerts = sub.add_parser("alerts", help="alert history + performance; --test checks delivery")
    alerts.add_argument("--limit", type=int, default=20)
    alerts.add_argument("--test", action="store_true",
                        help="send a synthetic alert through every configured sink")

    watchlist = sub.add_parser("watchlist", help="show tracked tokens; --refresh re-scores")
    watchlist.add_argument("--refresh", action="store_true")
    watchlist.add_argument("--include-archived", action="store_true")
    watchlist.add_argument("--top", action="store_true",
                           help="rank tracked tokens by opportunity score (Part 28 §5)")
    watchlist.add_argument("--limit", type=int, default=10,
                           help="number of ranked opportunities to show with --top")

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
    monitor.add_argument("--pumpfun", action="store_true",
                         help="also watch pump.fun launches via the free "
                              "PumpPortal stream (Part 32.5)")
    monitor.add_argument("--learn", action="store_true",
                         help="feed analyzed coins into the self-learning mind "
                              "layer as the scanner runs")

    # Self-learning mind layer: analog + model + rug reasoning (Section 10).
    mind = sub.add_parser("mind", help="self-learning mind layer (evaluate / metrics)")
    mind_sub = mind.add_subparsers(dest="mind_command", required=True)
    mind_eval = mind_sub.add_parser(
        "evaluate", help="analog + model + rug verdict for one token")
    mind_eval.add_argument("address")
    mind_eval.add_argument("--chain", default="solana", help="chain id (default solana)")
    mind_sub.add_parser(
        "metrics", help="self-evaluation metrics over resolved predictions")

    args = parser.parse_args(argv)
    if getattr(args, "network", None) is None and args.command in ("discover", "scan"):
        args.network = ["solana"]
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
