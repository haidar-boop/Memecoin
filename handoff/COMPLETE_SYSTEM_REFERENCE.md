# Complete System Reference — Every Subsystem, Every Setting, Every Dormant Kit

**This is the exhaustive "what does the bot actually do" reference.** It exists because
three other docs in this folder (`README.md`, `ARCHITECTURE.md`, `STATUS.md`) had drifted
out of sync with each other and with the code — different test counts, a stale branch
name, contradictory claims about which dormant features are actually built. This document
was generated 2026-07-20 by having 8 independent agents read every source file directly
(not summarize prior docs) and 2 more cross-check every config claim against
`meme_intelligence/config/settings.py` and hunt for gaps — see
[DECISIONS_LOG.md](./DECISIONS_LOG.md)'s 2026-07-20 "full handoff rebuild" entry for the
process. It does not replace `README.md` / `ARCHITECTURE.md` / `OPERATIONS.md` / `ROADMAP.md`
/ `STATUS.md` / `DECISIONS_LOG.md` — those keep their narrative/runbook/history roles. This
is the reference you open when you need the ground truth on one specific module, command,
or setting and don't want to trust a paragraph that might be nine days stale.

**Read `PROJECT_RULES.md` first if you haven't** — the 21 rules this whole system is built
against are assumed context below.

## Current State Snapshot (verified fresh at write time — do not hand-copy forward)

| Fact | Value | How to re-verify |
|---|---|---|
| Branch | `claude/bot-owners-manual-0a16e5` | `git rev-parse --abbrev-ref HEAD` |
| Tests passing | **938** | `python -m pytest tests/ -q` |
| Source lines | ~23,000 (`meme_intelligence/`) | `find meme_intelligence -name "*.py" \| xargs wc -l \| tail -1` |
| Test lines | ~15,000 (`tests/`) | `find tests -name "*.py" \| xargs wc -l \| tail -1` |
| `MEMEINTEL_*` settings | 348 unique | `grep -oE "MEMEINTEL_[A-Z0-9_]+" .env.example \| sort -u \| wc -l` |
| Settings dataclasses | 45 | `grep -c "^@dataclass" meme_intelligence/config/settings.py` |
| CLI subcommands | 16 (`alerts backtest compare daily discover mind monitor plan quick report scan search security token wallets watchlist`) | `grep -oE 'add_parser\("[a-z_-]+"' meme_intelligence/__main__.py` |
| Live trading | ARMED on the operator's droplet (dry-run by code default; his `.env` turns it on) | `/status` → `trading LIVE` |

**The three numbers that used to disagree** (626 in `ARCHITECTURE.md`, 766 in `README.md`,
938 in `STATUS.md`) are now all 938 as of this rewrite — but they *will* drift again the
moment a test is added. Don't trust any doc's hardcoded count, including this one months
from now; run the command in the table.

## Table of Contents

1. [Discovery & Collectors — Layer 1](#1-discovery--collectors--layer-1)
2. [Analyzers & Scoring — Layer 2/3](#2-analyzers--scoring--layer-23)
3. [Mind / Learning Layer](#3-mind--learning-layer)
4. [Workflow / Orchestration](#4-workflow--orchestration)
5. [Alerts & Telegram Control](#5-alerts--telegram-control)
6. [Trading & Execution](#6-trading--execution)
7. [Database, Analytics & AI Reasoning](#7-database-analytics--ai-reasoning)
8. [Config, Core Infra & CLI](#8-config-core-infra--cli)
9. [Dormant Kits — Master Table](#9-dormant-kits--master-table)
10. [History of Reversals](#10-history-of-reversals)

Each subsystem section below follows the same shape: **Overview** (what it's for and how it
fits the pipeline) → **Files** (what lives where) → **Configuration** (every real env var,
default, and purpose — pulled directly from `settings.py`, not guessed) → **Key Mechanisms**
(the actual formulas/gates/thresholds, with file:line pointers) → **Telegram / CLI Surface**
(what the operator can type to touch it) → **Dormant Features Here** (what's built but off,
and the exact command/env var to turn it on) → **Integration Points** (what it reads from
and feeds into elsewhere in the pipeline).

---

## 1. Discovery & Collectors — Layer 1

### Overview
This subsystem is Layer 1 of the four-layer scanning architecture: it finds *candidates*, never confirms them. It has two independent front doors that both feed the same downstream analysis pipeline:

1. **Pool-scan discovery** (`scanners/discovery.py`) — pulls newly-created / trending liquidity pools from DexScreener and GeckoTerminal (via the failover-pooled `MarketDataService`), applies hard gates (must have measurable liquidity above a floor, must be younger than a max-age window), dedupes to one pool per base token (deepest liquidity wins), and ranks survivors with a 0–100 "Discovery Score" (4 components × 25 pts: freshness, liquidity, volume, trading activity).
2. **Pump.fun launch funnel** (`scanners/launch_monitor.py` + `collectors/pumpfun.py`) — a real-time, event-driven feed (PumpPortal WebSocket) of bonding-curve token creations and graduations. Raw launches pass a basic structural filter (accepted launchpad, non-anonymous, dev-buy not oversized), survivors are tracked and periodically rechecked against the unofficial Pump.fun frontend API for traction, and only launches meeting a Section-8 evidence threshold (market cap floor, mcap growth vs launch, community replies, still trading) — or an outright bonding-curve graduation — are "promoted" to READY. A READY candidate still must be independently confirmed by a market-data provider (DexScreener/GeckoTerminal) before the analysis pipeline treats it as real; discovery is explicitly never confirmation (both modules' docstrings state this per spec Part 32.5 §2).

All collectors inherit a shared `BaseCollector` (`collectors/base.py`) that gives every provider, for free: a token-bucket rate limiter acquired before each request, TTL response caching, exponential-backoff retry with jitter on transient failures (timeouts/5xx/429, honoring `Retry-After`), consistent status/timeout/payload error handling, and secret redaction in any raised error message. Market, security, wallet, social, and swap-quote providers are additionally normalized into shared core models (`DexPair`, `SecurityProfile`, `WalletIntelData`, `CommunityProfile`, `LiquidityProbeResult`) so downstream code never sees provider-specific shapes. Multi-provider categories (market data) sit behind `ProviderPool` for automatic failover/cooldown, and `MarketDataService` adds cross-source liquidity verification on top of that pool. A dormant DexScreener "boost radar" (`workflow/boost_watcher.py`) polls the same DexScreener boost feed independently of the scan cycle and DMs the operator the first time any token crosses a paid-promotion threshold — off by default, no effect on discovery/scoring/trading unless enabled.

### Files
| File | Role |
|---|---|
| `meme_intelligence/scanners/discovery.py` | `DiscoveryEngine`: hard-filters, dedupes, and scores new/trending DEX pools into `TokenCandidate`s (0–100 Discovery Score) or `RejectedPool`s with reasons. `scan_new_pools()` wraps a GeckoTerminal-style client across a list of networks. |
| `meme_intelligence/scanners/launch_monitor.py` | `LaunchMonitor`: state machine tracking pump.fun launches from `PENDING` → `READY` → confirmed/expired. Basic filtering, traction rechecks against the frontend API, graduation fast-path, TTL-based expiry for both pending and ready states. `collect_launch_candidates()` is the per-cycle I/O composition (drain stream → recheck → return ready candidates). |
| `meme_intelligence/collectors/base.py` | `BaseCollector`: shared HTTP plumbing — rate limiting, caching, retry/backoff, timeout/status/payload error handling, secret scrubbing, proxy/CA-bundle aware session. Every concrete collector below subclasses it (except `PumpPortalClient`, which is a WebSocket stream, not request/response). |
| `meme_intelligence/collectors/market_data.py` | `DexScreenerClient` (pair lookup, search, per-pair fetch, paid-boost feed) and `GeckoTerminalClient` (new-pool / trending-pool discovery, per-token pools) — both public, keyless, normalize into `DexPair`. Also `CoinGeckoClient` (BTC/ETH/SOL market-environment snapshot + free per-token community data) and the chain-vocabulary translation helpers between DexScreener-style ids and GeckoTerminal network ids. |
| `meme_intelligence/collectors/market_service.py` | `MarketDataService`: wraps the market providers in a `ProviderPool` for automatic failover; tracks which provider actually answered per pair (bounded LRU) so `cross_check_liquidity()` can verify against a genuinely independent second source rather than guessing. |
| `meme_intelligence/collectors/pumpfun.py` | `PumpPortalClient` (single-connection PumpPortal WebSocket listener: `subscribeNewToken` + `subscribeMigration`, reconnect-with-backoff, bounded launch/migration buffers) and `PumpFunFrontendClient` (unofficial per-coin traction snapshot: market cap, reply count, curve progress, ban/nsfw flags, last-trade time). |
| `meme_intelligence/collectors/jupiter_data.py` | `JupiterClient`: Jupiter Swap API quote/swap client. `check_round_trip_liquidity()` does a live buy-then-sell probe (the "can you actually sell it?" honeypot test); `build_swap_transaction()` builds signed-ready swap transactions for the live-trading executor. Requires a Jupiter Developer Platform API key. |
| `meme_intelligence/collectors/security_data.py` | `GoPlusClient`: contract-security data for EVM chains and Solana (honeypot/mintable/freezable/ownership/holder-concentration/LP-lock flags), normalized into `SecurityProfile`. Handles GoPlus's stringly-typed booleans/fractions and per-chain endpoint/field differences. |
| `meme_intelligence/collectors/social_data.py` | `LunarCrushClient`: paid social-intelligence aggregator (X/Twitter-adjacent). Matches tokens to LunarCrush coins strictly by on-chain contract address (never symbol/name) to avoid cross-chain ticker collisions; deliberately leaves four per-account Twitter metrics unset since the public endpoints used don't expose them. |
| `meme_intelligence/collectors/wallet_data.py` | `HeliusClient` (top holders from raw chain state via RPC, parsed token transfers via Enhanced Transactions API) and `BirdeyeClient` (holder/wallet counts, recent trades). `WalletDataService` combines both with per-call failure isolation into one `WalletIntelData` snapshot. |
| `meme_intelligence/workflow/boost_watcher.py` | `BoostWatcher`: standalone background poll loop (independent task, own error backoff) over `DexScreenerClient.get_boosts()`; DMs the operator once per token the first time its cumulative boost count crosses a threshold. Off by default. |

**Not read but referenced** (for context, not catalogued in depth): `meme_intelligence/core/base.py`'s collaborators — `core/rate_limiter.py` (token-bucket `RateLimiter`), `core/provider_pool.py` (`ProviderPool` failover/cooldown), `core/retry.py` (`retry_async` exponential backoff+jitter, honors `Retry-After`), `core/cache.py` (`TTLCache`), and `meme_intelligence/__main__.py` (wiring: which collectors get constructed and which `enable_in_monitor` flags gate them into the continuous scanner).

### Configuration
**`DiscoverySettings`** — group `DISCOVERY` (`meme_intelligence/config/settings.py:392-414`)

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_DISCOVERY_MIN_LIQUIDITY_USD` | `min_liquidity_usd` | `5000.0` | Hard gate: pools below this are rejected outright. |
| `MEMEINTEL_DISCOVERY_TARGET_LIQUIDITY_USD` | `target_liquidity_usd` | `50000.0` | Liquidity value that earns full (25) discovery-score marks. |
| `MEMEINTEL_DISCOVERY_MIN_VOLUME_24H_USD` | `min_volume_24h_usd` | `1000.0` | Volume floor for the volume score component (0 marks below). |
| `MEMEINTEL_DISCOVERY_TARGET_VOLUME_24H_USD` | `target_volume_24h_usd` | `50000.0` | 24h volume that earns full (25) volume-score marks. |
| `MEMEINTEL_DISCOVERY_MAX_AGE_HOURS` | `max_age_hours` | `24.0` | Hard gate: pools older than this are no longer "new" and are rejected. |
| `MEMEINTEL_DISCOVERY_TARGET_TXNS_24H` | `target_txns_24h` | `200` | Trade-count (buys+sells) that earns full (25) activity-score marks. |

**`PumpFunSettings`** — group `PUMPFUN` (`settings.py:417-465`)

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` | **Master switch** — the whole launch funnel (PumpPortal stream + frontend rechecks) is OFF in the continuous scanner until set true. |
| `MEMEINTEL_PUMPFUN_LAUNCHPADS` | `launchpads` | `"pump"` | Comma-separated accepted launchpad/pool ids from the stream; anything else is rejected in basic filtering. |
| `MEMEINTEL_PUMPFUN_MAX_CREATOR_BUY_PERCENT` | `max_creator_buy_percent` | `20.0` | Basic filter: dev-buy above this % of supply at launch = insider-grab, rejected. |
| `MEMEINTEL_PUMPFUN_MAX_PENDING` | `max_pending` | `500` | Bounded tracking-table capacity; full table rejects new launches. |
| `MEMEINTEL_PUMPFUN_PENDING_TTL_HOURS` | `pending_ttl_hours` | `24.0` | PENDING launches with no promotion within this window are dropped. |
| `MEMEINTEL_PUMPFUN_RECHECK_INTERVAL_SECONDS` | `recheck_interval_seconds` | `120.0` | Per-launch traction-recheck cadence against the frontend API. |
| `MEMEINTEL_PUMPFUN_MAX_RECHECKS_PER_CYCLE` | `max_rechecks_per_cycle` | `8` | Frontend-API call budget per scanner cycle. |
| `MEMEINTEL_PUMPFUN_MIN_MARKET_CAP_GROWTH_RATIO` | `min_market_cap_growth_ratio` | `1.5` | Promotion gate: SOL-denominated mcap must have grown at least this multiple vs the launch snapshot. |
| `MEMEINTEL_PUMPFUN_MIN_USD_MARKET_CAP` | `min_usd_market_cap` | `10000.0` | Promotion gate: minimum USD market cap = evidence of real buying. |
| `MEMEINTEL_PUMPFUN_MIN_REPLY_COUNT` | `min_reply_count` | `5` | Promotion gate: minimum pump.fun community replies. |
| `MEMEINTEL_PUMPFUN_MAX_LAST_TRADE_AGE_MINUTES` | `max_last_trade_age_minutes` | `30.0` | Promotion gate: must have traded within this window (not abandoned). |
| `MEMEINTEL_PUMPFUN_READY_TTL_HOURS` | `ready_ttl_hours` | `72.0` | READY candidates whose market confirmation never succeeds are dropped after this (longer than `pending_ttl_hours` since market indexing can lag). |

**`BoostWatcherSettings`** — group `BOOST_WATCHER` (`settings.py:468-492`)

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_BOOST_WATCHER_ENABLED` | `enabled` | `False` | **Master switch** — the entire boost radar is off by default; enabling it is the only behavior change (Rule 18). |
| `MEMEINTEL_BOOST_WATCHER_THRESHOLD` | `threshold` | `100.0` | Cumulative DexScreener boost count that triggers a one-time alert per token. |
| `MEMEINTEL_BOOST_WATCHER_POLL_INTERVAL_SECONDS` | `poll_interval_seconds` | `30.0` | Poll cadence; matches DexScreener's ~30s server-side edge cache (polling faster re-reads the same cached body). |
| `MEMEINTEL_BOOST_WATCHER_CHAIN_FILTER` | `chain_filter` | `"solana"` | Restrict alerts to one chain; empty string = all chains. |
| `MEMEINTEL_BOOST_WATCHER_MAX_SEEN_KEYS` | `max_seen_keys` | `5000` | Bounded dedup memory of already-alerted (chain, token) keys. |

**`ProviderSettings`** — group `PROVIDERS` (`settings.py:345-389`), covers every collector's endpoint + rate limit + failover behavior:

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_PROVIDERS_DEXSCREENER_BASE_URL` | `dexscreener_base_url` | `https://api.dexscreener.com` | DexScreener API host. |
| `MEMEINTEL_PROVIDERS_DEXSCREENER_REQUESTS_PER_MINUTE` | `dexscreener_requests_per_minute` | `240.0` | Kept below the documented 300/min limit. |
| `MEMEINTEL_PROVIDERS_GECKOTERMINAL_BASE_URL` | `geckoterminal_base_url` | `https://api.geckoterminal.com` | GeckoTerminal API host. |
| `MEMEINTEL_PROVIDERS_GECKOTERMINAL_REQUESTS_PER_MINUTE` | `geckoterminal_requests_per_minute` | `25.0` | Kept below the documented free ~30/min limit. |
| `MEMEINTEL_PROVIDERS_GOPLUS_BASE_URL` | `goplus_base_url` | `https://api.gopluslabs.io` | GoPlus API host. |
| `MEMEINTEL_PROVIDERS_GOPLUS_REQUESTS_PER_MINUTE` | `goplus_requests_per_minute` | `20.0` | Conservative free-tier budget. |
| `MEMEINTEL_PROVIDERS_COINGECKO_BASE_URL` | `coingecko_base_url` | `https://api.coingecko.com` | CoinGecko API host. |
| `MEMEINTEL_PROVIDERS_COINGECKO_REQUESTS_PER_MINUTE` | `coingecko_requests_per_minute` | `10.0` | Documented free limit ~10-30/min. |
| `MEMEINTEL_PROVIDERS_HELIUS_RPC_URL` | `helius_rpc_url` | `https://mainnet.helius-rpc.com` | Helius RPC host. |
| `MEMEINTEL_PROVIDERS_HELIUS_API_URL` | `helius_api_url` | `https://api.helius.xyz` | Helius Enhanced Transactions API host. |
| `MEMEINTEL_PROVIDERS_HELIUS_REQUESTS_PER_MINUTE` | `helius_requests_per_minute` | `120.0` | Free tier allows ~10 rps; kept well below. |
| `MEMEINTEL_PROVIDERS_BIRDEYE_BASE_URL` | `birdeye_base_url` | `https://public-api.birdeye.so` | Birdeye API host. |
| `MEMEINTEL_PROVIDERS_BIRDEYE_REQUESTS_PER_MINUTE` | `birdeye_requests_per_minute` | `20.0` | Free tier ~1 rps + monthly CU budget. |
| `MEMEINTEL_PROVIDERS_JUPITER_BASE_URL` | `jupiter_base_url` | `https://api.jup.ag` | Jupiter Swap API host. |
| `MEMEINTEL_PROVIDERS_JUPITER_REQUESTS_PER_MINUTE` | `jupiter_requests_per_minute` | `50.0` | Free tier documented limit 60/min (1 rps). |
| `MEMEINTEL_PROVIDERS_PUMPPORTAL_WS_URL` | `pumpportal_ws_url` | `wss://pumpportal.fun/api/data` | PumpPortal free data WebSocket. |
| `MEMEINTEL_PROVIDERS_PUMPFUN_BASE_URL` | `pumpfun_base_url` | `https://frontend-api-v3.pump.fun` | Unofficial frontend API host — configurable because it has changed/rotated before. |
| `MEMEINTEL_PROVIDERS_PUMPFUN_REQUESTS_PER_MINUTE` | `pumpfun_requests_per_minute` | `30.0` | No documented limit; conservative default. |
| `MEMEINTEL_PROVIDERS_LUNARCRUSH_BASE_URL` | `lunarcrush_base_url` | `https://lunarcrush.com/api4` | LunarCrush API host. |
| `MEMEINTEL_PROVIDERS_LUNARCRUSH_REQUESTS_PER_MINUTE` | `lunarcrush_requests_per_minute` | `10.0` | Real per-plan limits undocumented; conservative default the operator can raise. |
| `MEMEINTEL_PROVIDERS_FAILURE_THRESHOLD` | `failure_threshold` | `3` | Consecutive failures before `ProviderPool` cools a provider down. |
| `MEMEINTEL_PROVIDERS_COOLDOWN_SECONDS` | `cooldown_seconds` | `60.0` | How long an unhealthy provider is skipped before being retried. |

**`HttpSettings`** — group `HTTP` (`settings.py:314-342`), shared by `BaseCollector`:

| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_HTTP_TIMEOUT_SECONDS` | `timeout_seconds` | `10.0` |
| `MEMEINTEL_HTTP_RETRY_ATTEMPTS` | `retry_attempts` | `4` |
| `MEMEINTEL_HTTP_RETRY_BASE_DELAY` | `retry_base_delay` | `0.5` |
| `MEMEINTEL_HTTP_RETRY_MAX_DELAY` | `retry_max_delay` | `8.0` |
| `MEMEINTEL_HTTP_CACHE_TTL_SECONDS` | `cache_ttl_seconds` | `30.0` |
| `MEMEINTEL_HTTP_CACHE_MAX_ENTRIES` | `cache_max_entries` | `2048` |

**`WalletIntelSettings`** — group `WALLET` (`settings.py:795-866`), gates `wallet_data.py` usage:

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_WALLET_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` | Master switch for wallet lookups inside the continuous scanner (report/plan/wallets commands always allowed regardless). |
| `MEMEINTEL_WALLET_TOP_HOLDERS_LIMIT` | `top_holders_limit` | `20` | Helius `get_top_holders` limit. |
| `MEMEINTEL_WALLET_RECENT_TRADES_LIMIT` | `recent_trades_limit` | `50` | Birdeye/Helius recent-activity limit. |
| `MEMEINTEL_WALLET_CREDIT_GATE_MIN_SECURITY_SCORE` | `credit_gate_min_security_score` | `50.0` | Gated (monitor-loop) lookups only spend credits on candidates at/above this security score. |
| `MEMEINTEL_WALLET_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` | `credit_gate_max_lookups_per_day` | `200` | Daily cap on gated lookups (0 = unlimited); forced lookups (operator/`/check`/plan/report) bypass it. |
| `MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES` | `credit_gate_cooldown_minutes` | `60.0` | Minimum time between gated lookups of the same token (0 = off). |
| (also: `whale_min_percent`=1.0, `risk_whale_percent`=5.0, `target_accumulating_wallets`=10, `artificial_same_size_fraction`=0.30, `dominant_buyer_volume_fraction`=0.60, `min_buy_volume_for_dominance_usd`=500.0 — scoring anchors, not discovery-specific.) | | | |

**`SocialIntelSettings`** — group `SOCIAL` (`settings.py:869-897`), gates `social_data.py` (LunarCrush) usage:

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` | Master switch — LunarCrush calls stay out of the continuous scanner until enabled AND a key is configured. |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_MIN_SECURITY_SCORE` | `credit_gate_min_security_score` | `50.0` | Same gating shape as wallet. |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` | `credit_gate_max_lookups_per_day` | `200` | Daily cap on gated LunarCrush lookups. |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_COOLDOWN_MINUTES` | `credit_gate_cooldown_minutes` | `60.0` | Per-token cooldown between gated lookups. |

**`LiquidityProbeSettings`** — group `LIQUIDITY_PROBE` (`settings.py:900-935`), gates `jupiter_data.py` round-trip probe:

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_LIQUIDITY_PROBE_ENABLED` | `enabled` | `True` | Master switch — ON by default (unlike the other credit-gated layers). |
| `MEMEINTEL_LIQUIDITY_PROBE_PROBE_SOL_AMOUNT` | `probe_sol_amount` | `0.3` | SOL size of the live buy probe (~$50 at time of writing; tune as SOL price moves). |
| `MEMEINTEL_LIQUIDITY_PROBE_SLIPPAGE_BPS` | `slippage_bps` | `500` | 5% tolerance on the probe quotes. |
| `MEMEINTEL_LIQUIDITY_PROBE_SELL_CONFIRM_FRACTION` | `sell_confirm_fraction` | `0.05` | Fraction re-probed on a failed full-size sell to distinguish "pool too thin" from "true honeypot". |

**API keys** (env vars, not `MEMEINTEL_<GROUP>_<FIELD>` shaped — top-level secrets read directly in `Settings.from_env`): `MEMEINTEL_HELIUS_API_KEY`, `MEMEINTEL_BIRDEYE_API_KEY`, `MEMEINTEL_LUNARCRUSH_API_KEY`, `MEMEINTEL_JUPITER_API_KEY`, `MEMEINTEL_COINGECKO_API_KEY` (optional demo key). DexScreener, GeckoTerminal, GoPlus, and PumpPortal/pump.fun frontend are keyless.

### Key Mechanisms
- **Discovery Score formula** (`scanners/discovery.py:29,119-174`): 4 components × 25 pts max, summed to 0–100. Freshness decays linearly from 25 at pool creation to 0 at `max_age_hours`. Liquidity and volume are linearly scaled from their `min_*` (0 pts) to `target_*` (25 pts) anchors, clamped. Activity scales 24h buys+sells linearly to `target_txns_24h` (25 pts at/above target). Any missing input (age/volume/txns unknown) scores that component 0 rather than guessing (Rule 8).
- **Discovery hard gates** (`discovery.py:100-117`): liquidity `None` → rejected ("cannot verify pool"); liquidity below `min_liquidity_usd` → rejected; age above `max_age_hours` → rejected. Unknown liquidity is explicitly treated as unverifiable, not neutral.
- **Dedup rule** (`discovery.py:90-98`): one candidate per `(chain, base_token_address)`, keeping whichever pool reports the highest `liquidity_usd`.
- **Launch basic filter** (`launch_monitor.py:138-148`): reject if launchpad not in `launchpad_list`; reject if both name and symbol are missing (anonymous); reject if `initial_buy_percent` (creator's launch-time supply share, derived from `initialBuy` tokens vs the 1B fixed pump.fun supply) exceeds `max_creator_buy_percent`.
- **Section-8 promotion gates** (`launch_monitor.py:223-264`): every gate needs real data to pass (missing data fails the gate, never assumed True) — instant pass on bonding-curve completion (`state.complete`); otherwise, market cap ≥ `min_usd_market_cap` AND SOL-denominated mcap growth vs launch ≥ `min_market_cap_growth_ratio` AND reply count ≥ `min_reply_count` AND last trade within `max_last_trade_age_minutes`. Growth is compared SOL-to-SOL specifically to avoid SOL-price-drift noise.
- **Migration fast-path** (`launch_monitor.py:152-170`): a bonding-curve graduation event (`txType=migrate` on the PumpPortal stream) instantly promotes an already-tracked PENDING launch to READY, skipping the traction gates — graduation is treated as the strongest signal the launchpad emits.
- **Frontend-miss tolerance** (`launch_monitor.py:40,198-207`): a tracked launch is dropped after `_MAX_FRONTEND_MISSES = 3` consecutive 404s from the frontend API (hardcoded constant, not a settings field) — one miss is tolerated as fresh-mint indexing lag.
- **Dual TTL expiry** (`launch_monitor.py:266-290`): PENDING entries expire after `pending_ttl_hours` from first-seen; READY entries get a separate, longer `ready_ttl_hours` measured from promotion — explicitly fixing a prior bug where zombie READY candidates whose market confirmation never landed would retry forever, burning API calls and permanently occupying `max_pending` capacity.
- **Capacity-before-expiry ordering** (`launch_monitor.py:105-109`): `ingest()` calls `_expire_stale()` *before* checking `max_pending` capacity, specifically to avoid a full tracking table staying permanently full.
- **Failover / provider health** (`core/provider_pool.py`): tries providers in order; a `TransientCollectorError` (5xx/timeout/429-after-retries) or unexpected exception counts toward `failure_threshold` consecutive failures, then cools the provider down for `cooldown_seconds`; a plain `CollectorError` (e.g. 404 = "no data for this item") does NOT count against provider health — it just fails over to the next provider for that item.
- **Cross-source liquidity verification** (`collectors/market_service.py:103-150`): excludes the provider that actually supplied the pair (tracked via a bounded LRU of pair→provider, cap 4096) rather than guessing by list position, so a provider can never "confirm" its own data. Agreement = both liquidity figures within `_AGREEMENT_FACTOR = 2.0×` of each other, OR both exactly $0 (dead pool, treated as agreement, not "unable to verify"). Returns `(True, note)` / `(False, note)` / `(None, note)` — `None` means genuinely unverified, never silently treated as confirmed.
- **Token-bucket rate limiting** (`core/rate_limiter.py`): classic bucket, `rate_per_second` refills over time up to `burst`; `acquire()` blocks (async sleep) rather than dropping requests. `RateLimiter.per_minute()` converts the `ProviderSettings.*_requests_per_minute` values.
- **Retry/backoff** (`core/retry.py`): only `TransientCollectorError` is retried (permanent errors propagate immediately); delay doubles each attempt capped at `retry_max_delay`, ±25% jitter to avoid retry storms, and a 429's `Retry-After` header (parsed in `base.py:143-153`, clamped 0–120s) overrides the computed delay if longer.
- **PumpPortal reconnect backoff** (`collectors/pumpfun.py:196-230`): exponential backoff 1s→60s cap; backoff only resets once a message actually arrives post-handshake (not merely on connect) — fixed a prior storm where an accept-then-drop server (or PumpPortal's one-connection-per-client policy rejecting a second client) caused ~1 reconnect/sec forever.
- **Launch buffer overflow policy** (`collectors/pumpfun.py:79-84`): bounded deques (`_LAUNCH_BUFFER_MAX=2048`, `_MIGRATION_BUFFER_MAX=512`) drop the OLDEST events on overflow — correct for a freshness-driven feed.
- **Jupiter round-trip honeypot test** (`collectors/jupiter_data.py:155-221`): buy quote → sell-back quote at full size; if the full sell finds no route, re-probes with `sell_confirm_fraction` (5%) of the received tokens before declaring non-sellable — distinguishes a genuine honeypot (tiny sell also fails) from a pool merely too thin to exit the whole position at once (tiny sell succeeds).
- **GoPlus boolean parsing** (`collectors/security_data.py:65-82`): only exact `"1"`/`"0"` (or bool) is trusted; anything else (empty string, typo, new API value) becomes `None` (unknown) rather than being read as `False`/safe.
- **Burn-address exclusion** (`security_data.py:39-62`): exact-match only against known EVM/Solana burn addresses when computing holder concentration and LP-lock percentage — deliberately not a substring check, to avoid false-positiving on ordinary addresses that happen to contain "dead"-like substrings.
- **LunarCrush chain-matching discipline** (`social_data.py`): matches tokens to LunarCrush coins only via on-chain contract address, never symbol/name, because meme tickers routinely collide across chains.
- **Helius owner aggregation** (`wallet_data.py:145-197`): aggregates token-account balances by resolved OWNER wallet (not by raw token account) so a whale split across multiple accounts isn't undercounted; zips the largest-accounts response against a content-fingerprinted (sha256 of account list) cache key for the owner-resolution call so a stale owners response can never be paired with a changed accounts list.
- **Boost-watcher baseline priming** (`workflow/boost_watcher.py:110-137`): the first poll after start only records the current boosted set as "seen" without alerting — enabling the watcher never dumps the whole existing boosted set to the phone; only crossings from that point on notify. Dedup is a bounded key set (`max_seen_keys`) of `(chain, address)`.

### Telegram / CLI Surface
None directly in these files — this subsystem has no Telegram command or CLI subcommand of its own. It is purely internal plumbing feeding the scanner/analysis pipeline. Wiring/enable points live in `meme_intelligence/__main__.py`: the pump.fun launch monitor, wallet service, and social (LunarCrush) service are each constructed and joined to the continuous monitor loop only if their respective `enable_in_monitor` flag is set (with a printed operator note if the flag is set but the required API key is missing); the boost watcher is constructed and started only if `settings.boost_watcher.enabled` is true. Downstream Telegram commands (`/check`, wallets, report, plan, etc.) live in other subsystems and were out of scope for this catalog, though wallet/social lookups triggered by those commands are noted as bypassing the credit-gate budgets described above.

### Dormant Features Here
1. **Pump.fun launch funnel (entire discovery front)** — `PumpFunSettings.enable_in_monitor` defaults `False`. Off by default, the continuous scanner only sees pool-scan discovery (DexScreener/GeckoTerminal); the PumpPortal WebSocket and frontend-API recheck loop are never constructed. Enable with `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR=true` (see `__main__.py:1005-1010`).

2. **DexScreener boost radar** (`workflow/boost_watcher.py`) — `BoostWatcherSettings.enabled` defaults `False`. Fully built (poll loop, alert emission, backoff, dedup) but never started unless `MEMEINTEL_BOOST_WATCHER_ENABLED=true`. Even when enabled it is deliberately decoupled from scoring/trading — it only sends a heads-up DM ("attention, not endorsement... verify before acting"), never a buy signal, and never touches the discovery/analysis pipeline.

3. **Wallet intelligence in the continuous monitor** (`collectors/wallet_data.py`) — `WalletIntelSettings.enable_in_monitor` defaults `False`. Helius/Birdeye calls are metered/paid, so by default wallet lookups only happen on-demand (report/plan/wallets Telegram commands, per `__main__.py` comments), not on every scanned token. Enable with `MEMEINTEL_WALLET_ENABLE_IN_MONITOR=true` AND a configured `MEMEINTEL_HELIUS_API_KEY` / `MEMEINTEL_BIRDEYE_API_KEY` — setting the flag without a key is detected and reported to the operator rather than silently no-op'ing. Even when enabled, a credit-gate (`credit_gate_min_security_score`, `credit_gate_max_lookups_per_day`, `credit_gate_cooldown_minutes`) still throttles spend on scanner-triggered lookups; forced lookups (operator holdings, `/check`, plan/report) always bypass the gate.

4. **Social intelligence via LunarCrush** (`collectors/social_data.py`) — `SocialIntelSettings.enable_in_monitor` defaults `False`. Built 2026-07-20 per the docstring, explicitly "kept OFF until [the operator] supplies a paid LunarCrush key and runs deploy/enable-x-community-tracking.sh." Enable with `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR=true` plus `MEMEINTEL_LUNARCRUSH_API_KEY`; same credit-gate shape as wallet intel applies once on.

5. **Jupiter live round-trip liquidity probe** — the one exception: `LiquidityProbeSettings.enabled` defaults `True` (already active), requiring only `MEMEINTEL_JUPITER_API_KEY` to be present; listed here for completeness since it sits in the same collectors layer and is easy to mistake for another opt-in feature.

No other dormant switches were found in the files read for this subsystem; all `ProviderSettings`/`HttpSettings`/`DiscoverySettings` values are always-active tuning knobs, not feature flags.

### Integration Points
**Reads from:**
- DexScreener public REST API (`latest/dex/*`, `token-boosts/*/v1`) — no key.
- GeckoTerminal public REST API (`api/v2/networks/*`) — no key.
- CoinGecko public REST API (`api/v3/simple/price`, `api/v3/coins/{platform}/contract/{address}`) — optional demo key for a higher rate limit.
- GoPlus Security public API (EVM `api/v1/token_security/{chain_id}`, Solana `api/v1/solana/token_security`) — no key.
- PumpPortal WebSocket (`wss://pumpportal.fun/api/data`) — no key, one connection only.
- Pump.fun unofficial frontend API (`frontend-api-v3.pump.fun/coins/{mint}`) — no key, unstable/unofficial surface.
- Jupiter Swap API (`swap/v1/quote`, `swap/v1/swap`) — requires Developer Platform API key.
- Helius Solana RPC + Enhanced Transactions API — requires API key.
- Birdeye Data Services API — requires API key.
- LunarCrush public API v4 — requires paid API key.

**Writes to / feeds:**
- `DiscoveryEngine.evaluate()` output (`TokenCandidate`, `RejectedPool`) feeds Layer 2 (security scanning) and Layer 3 (deep intelligence analysis) in the scanning pipeline; rejections are logged for the future learning/backtesting system (Part 24) rather than silently dropped.
- `LaunchMonitor.ready_candidates()` output (`LaunchCandidate`) feeds the scanner for independent market-data confirmation before entering the same analysis pipeline as pool-scan candidates; `LaunchMonitor.confirm()`/`defer()` are called back by the scanner once confirmation succeeds or fails for a cycle.
- `MarketDataService` / individual collector clients are consumed throughout `meme_intelligence/analyzers/`, `meme_intelligence/scanners/`, and `meme_intelligence/workflow/` for security scoring, on-chain/wallet scoring, community scoring, and the live-trading executor (Jupiter swap building).
- `BoostWatcher` dispatches `AlertEvent`s through `alerts.notification_engine.NotificationEngine` — the same alert-delivery path as the rest of the system, tagged `alert_type="boost"` and `AlertPriority.MEDIUM`.
- All collectors log through the shared `core.logging_setup.get_logger` hierarchy under `collectors.<name>` / `scanners.<name>` / `workflow.<name>` namespaces (Rule 13 — every important action logged).

**Depends on shared core infrastructure:** `core/cache.py` (`TTLCache`), `core/rate_limiter.py` (`RateLimiter`), `core/retry.py` (`retry_async`), `core/provider_pool.py` (`ProviderPool`), `core/errors.py` (`CollectorError`, `TransientCollectorError`, `RateLimitedError`, `AllProvidersFailedError`, `ConfigurationError`), `core/models.py` (normalized dataclasses: `DexPair`, `TokenIdentity`, `SecurityProfile`, `CommunityProfile`, `WalletIntelData`, `PumpFunLaunch`, `PumpFunCoinState`, `LiquidityProbeResult`), and `workflow/controller.py`'s `_BoundedKeySet` (reused by `BoostWatcher` for dedup).

---

## 2. Analyzers & Scoring — Layer 2/3

### Overview
The Analyzers & Scoring subsystem (`meme_intelligence/analyzers/`) is the judgment layer of the pipeline: it turns normalized, source-agnostic profiles (`SecurityProfile`, `CommunityProfile`, `OnChainProfile`, `DexPair`, `WalletIntelData`, plus AI/analyst judgment slots) into per-category 0-100 scores, then combines those categories into one Final Intelligence Score and a five-band Classification (Elite Opportunity / Strong Candidate / Watchlist / Speculative / Avoid).

Every analyzer shares one evidence discipline, implemented once in `common.py`: a `SubScore` accumulates either averaged 0-100 "signals" (quality judgments) or "deductions" from a start-of-100 baseline (risk checks), tracks which facts were observed vs. unknown, and reports `None` (not a fabricated number) when nothing could be evaluated. Findings are tagged `ACCEPTABLE_UNCERTAINTY` / `SERIOUS_WARNING` / `DESTRUCTIVE`; a single `DESTRUCTIVE` finding zeroes the whole category (security, community) and becomes a hard "Avoid" override at the master level. Confidence (`HIGH`/`MEDIUM`/`LOW`) is derived from the known-to-total-facts ratio and is capped at `MEDIUM` until multi-source confirmation exists (Rule 9).

Seven analyzer engines each own one category (Foundation, Security, Community, Blockchain/On-chain, Momentum, Narrative, Timing — heuristic, no dedicated engine). `ScoringEngine` (Part 10/31) is the master combiner: it applies red-flag overrides, runs a six-question decision tree that can reject outright or cap the classification, then computes the Part 31-locked weighted score, renormalized over whichever categories have data, with `coverage` reported honestly. `RiskAnalyzer` (Part 9) and `OpportunityRanker` (Part 28) are two independent, secondary lenses built from the same analyzer outputs — risk is a 0-100 "higher = riskier" number that never touches the master score or its classification, and the opportunity rank is an upside-tilted re-weighting used only for watchlist prioritization. `WalletIntelligenceAnalyzer` (Part 17) is the smart-money/whale module — it is functionally complete but gated to run only selectively (a metered-API credit gate) because it costs real Helius/Birdeye API spend; it is dormant in the continuous scanner by default and only fires on-demand or after a candidate clears a cheap pre-filter.

### Files
| File | Role |
|---|---|
| `meme_intelligence/analyzers/common.py` | Shared `Finding`/`SubScore` evidence primitives, `confidence_from_facts`, `scale`/`scale_inverted` helpers used by every analyzer |
| `meme_intelligence/analyzers/security_analyzer.py` | Security engine (Parts 4/18/33): contract, liquidity, distribution, developer, manipulation sub-scores; destructive-finding override to 0; risk tier/band |
| `meme_intelligence/analyzers/security_monitor.py` | Continuous security-fact diffing (Part 18 §10/13): detects deterioration (honeypot appearing, LP unlock, tax hikes, concentration creep) between analyses for alerting |
| `meme_intelligence/analyzers/community_analyzer.py` | Community engine (Part 5, Part 18 §8): engagement/growth/loyalty/creativity/dev-relationship; fake-community destructive flag; `merge_community_profiles` for multi-source merge |
| `meme_intelligence/analyzers/onchain_analyzer.py` | On-chain engine (Part 6): holder health, smart money, whale behavior, developer activity, volume quality (wash-trading detection), token flow; market-phase classification; `derive_onchain_profile` builds a partial profile from market+security data |
| `meme_intelligence/analyzers/token_analyzer.py` | Token structure engine (Part 7): valuation, liquidity-to-mcap, supply concentration, volume, plus qualitative competition/catalyst slots; market-cap stage and valuation classification |
| `meme_intelligence/analyzers/foundation_analyzer.py` | Foundation engine (Part 5 §1-5/12): combines AI/analyst judgment slots (meme strength, narrative, brand, dev communication, long-term) with the community-quality score |
| `meme_intelligence/analyzers/momentum_analyzer.py` | Momentum engine (Part 14, doctrine from Part 26): price/volume/social/on-chain lenses (25% each), entry-zone classification, preferred action |
| `meme_intelligence/analyzers/narrative_analyzer.py` | Narrative engine (Part 19): viral score (5x20%) and narrative-intelligence score (5x20%) sharing a cultural-timing lens; sentiment, narrative risk, strengths/weaknesses |
| `meme_intelligence/analyzers/risk_analyzer.py` | Risk engine (Part 9): 0-100 "higher = riskier" score across security/market/community/token/execution; `PortfolioRiskManager` (exposure limits, drawdown posture); `emergency_flags` |
| `meme_intelligence/analyzers/wallet_intelligence.py` | Smart-money/whale engine (Part 17): whale classification, 5-lens Smart Money Confidence Score, accumulation verdict, exchange-flow estimate, pump-and-exit detection; `WalletTrackRecord`/`wallet_reputation` for future outcome tracking |
| `meme_intelligence/analyzers/opportunity_ranker.py` | Watchlist opportunity ranking (Part 28 §5-6): a second, upside-tilted ranking axis over already-computed category scores, distinct from and never mutating the master score |
| `meme_intelligence/analyzers/scoring_engine.py` | Master scoring/decision engine (Part 10/31): red-flag overrides, decision tree, Part-31-locked weighted score, classification, `derive_timing_score` heuristic |
| `meme_intelligence/core/models.py` | Shared dataclasses: `TokenIdentity`, `DexPair`, `SecurityProfile`, `CommunityProfile`, `OnChainProfile`, `WalletIntelData`, `CategoryScores`, `classify()`, `compute_weighted_score()` |
| `meme_intelligence/core/enums.py` | All framework enums: `Classification`, `RiskTier`, `ConfidenceLevel`, `MarketPhase`, `CommunityRating`, `EntryZone`, `PreferredAction`, `WhaleType`, `AccumulationVerdict`, etc. |

### Configuration
All defaults below are read directly from `meme_intelligence/config/settings.py`. Env vars follow `MEMEINTEL_<GROUP>_<FIELD>` (group names shown are literal, from `Settings.from_env`'s `_load_group` calls).

**Master weights / bands**
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_WEIGHTS_FOUNDATION` | `ScoringWeights.foundation` | 0.15 | Master score category weight |
| `MEMEINTEL_WEIGHTS_SECURITY` | `.security` | 0.15 | " |
| `MEMEINTEL_WEIGHTS_COMMUNITY` | `.community` | 0.15 | " |
| `MEMEINTEL_WEIGHTS_BLOCKCHAIN` | `.blockchain` | 0.15 | " |
| `MEMEINTEL_WEIGHTS_MOMENTUM` | `.momentum` | 0.15 | " |
| `MEMEINTEL_WEIGHTS_NARRATIVE` | `.narrative` | 0.15 | " |
| `MEMEINTEL_WEIGHTS_TIMING` | `.timing` | 0.10 | " |
| `MEMEINTEL_BANDS_ELITE` | `ClassificationBands.elite` | 90.0 | Score >= this -> Elite Opportunity |
| `MEMEINTEL_BANDS_STRONG_CANDIDATE` | `.strong_candidate` | 80.0 | -> Strong Candidate |
| `MEMEINTEL_BANDS_WATCHLIST` | `.watchlist` | 70.0 | -> Watchlist |
| `MEMEINTEL_BANDS_SPECULATIVE` | `.speculative` | 60.0 | -> Speculative; below -> Avoid |

**Security (`SecuritySubWeights` group `SECURITY_WEIGHTS`; `SecurityThresholds` group `SECURITY`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_SECURITY_WEIGHTS_CONTRACT` | contract | 0.25 |
| `MEMEINTEL_SECURITY_WEIGHTS_LIQUIDITY` | liquidity | 0.20 |
| `MEMEINTEL_SECURITY_WEIGHTS_DISTRIBUTION` | distribution | 0.20 |
| `MEMEINTEL_SECURITY_WEIGHTS_DEVELOPER` | developer | 0.20 |
| `MEMEINTEL_SECURITY_WEIGHTS_MANIPULATION` | manipulation | 0.15 |
| `MEMEINTEL_SECURITY_MAX_TAX_PERCENT` | max_tax_percent | 10.0 |
| `MEMEINTEL_SECURITY_EXTREME_TAX_PERCENT` | extreme_tax_percent | 25.0 |
| `MEMEINTEL_SECURITY_MIN_LIQUIDITY_USD` | min_liquidity_usd | 5000.0 |
| `MEMEINTEL_SECURITY_HEALTHY_LIQUIDITY_USD` | healthy_liquidity_usd | 50000.0 |
| `MEMEINTEL_SECURITY_MIN_LP_LOCKED_PERCENT` | min_lp_locked_percent | 50.0 |
| `MEMEINTEL_SECURITY_GOOD_LP_LOCKED_PERCENT` | good_lp_locked_percent | 80.0 |
| `MEMEINTEL_SECURITY_WARN_TOP_HOLDER_PERCENT` | warn_top_holder_percent | 10.0 |
| `MEMEINTEL_SECURITY_MAX_TOP_HOLDER_PERCENT` | max_top_holder_percent | 20.0 |
| `MEMEINTEL_SECURITY_WARN_TOP10_HOLDER_PERCENT` | warn_top10_holder_percent | 50.0 |
| `MEMEINTEL_SECURITY_MAX_TOP10_HOLDER_PERCENT` | max_top10_holder_percent | 70.0 |
| `MEMEINTEL_SECURITY_MIN_HOLDER_COUNT` | min_holder_count | 50 |
| `MEMEINTEL_SECURITY_WARN_CREATOR_PERCENT` | warn_creator_percent | 5.0 |
| `MEMEINTEL_SECURITY_MAX_CREATOR_PERCENT` | max_creator_percent | 10.0 |
| `MEMEINTEL_SECURITY_MAX_ROUND_TRIP_LOSS_PERCENT` | max_round_trip_loss_percent | 50.0 |
| `MEMEINTEL_SECURITY_EXTREME_ROUND_TRIP_LOSS_PERCENT` | extreme_round_trip_loss_percent | 90.0 |

**Community (`CommunitySubWeights` group `COMMUNITY_WEIGHTS`; `CommunityThresholds` group `COMMUNITY`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_COMMUNITY_WEIGHTS_ENGAGEMENT` / `GROWTH` / `LOYALTY` / `CREATIVITY` / `DEV_RELATIONSHIP` | each sub-weight | 0.20 each |
| `MEMEINTEL_COMMUNITY_EXCELLENT_ENGAGEMENT_RATE_PERCENT` | excellent_engagement_rate_percent | 5.0 |
| `MEMEINTEL_COMMUNITY_FAKE_ENGAGEMENT_RATE_PERCENT` | fake_engagement_rate_percent | 0.5 |
| `MEMEINTEL_COMMUNITY_MIN_FOLLOWERS_FOR_FAKE_CHECK` | min_followers_for_fake_check | 10000 |
| `MEMEINTEL_COMMUNITY_BOT_FOLLOWER_WARN_PERCENT` | bot_follower_warn_percent | 30.0 |
| `MEMEINTEL_COMMUNITY_BOT_FOLLOWER_ARTIFICIAL_PERCENT` | bot_follower_artificial_percent | 50.0 |
| `MEMEINTEL_COMMUNITY_DUPLICATE_MESSAGE_WARN_PERCENT` | duplicate_message_warn_percent | 20.0 |
| `MEMEINTEL_COMMUNITY_TELEGRAM_ACTIVE_TARGET_PERCENT` | telegram_active_target_percent | 15.0 |
| `MEMEINTEL_COMMUNITY_TARGET_GROWTH_RATE_7D_PERCENT` | target_growth_rate_7d_percent | 30.0 |
| `MEMEINTEL_COMMUNITY_TARGET_DEV_UPDATES_PER_WEEK` | target_dev_updates_per_week | 3.0 |
| `MEMEINTEL_COMMUNITY_TARGET_USER_CONTENT_PER_DAY` | target_user_content_per_day | 20.0 |

**On-chain (`OnChainSubWeights` group `ONCHAIN_WEIGHTS`; `OnChainThresholds` group `ONCHAIN`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_ONCHAIN_WEIGHTS_HOLDER_HEALTH` | holder_health | 0.20 |
| `MEMEINTEL_ONCHAIN_WEIGHTS_SMART_MONEY` | smart_money | 0.20 |
| `MEMEINTEL_ONCHAIN_WEIGHTS_WHALE_BEHAVIOR` | whale_behavior | 0.15 |
| `MEMEINTEL_ONCHAIN_WEIGHTS_DEVELOPER_ACTIVITY` | developer_activity | 0.15 |
| `MEMEINTEL_ONCHAIN_WEIGHTS_VOLUME_QUALITY` | volume_quality | 0.15 |
| `MEMEINTEL_ONCHAIN_WEIGHTS_TOKEN_FLOW` | token_flow | 0.15 |
| `MEMEINTEL_ONCHAIN_MIN_HOLDER_COUNT` | min_holder_count | 50 |
| `MEMEINTEL_ONCHAIN_TARGET_HOLDER_COUNT` | target_holder_count | 2000 |
| `MEMEINTEL_ONCHAIN_HOLDER_GROWTH_TARGET_PERCENT_24H` | holder_growth_target_percent_24h | 20.0 |
| `MEMEINTEL_ONCHAIN_HEALTHY_TRADES_PER_TRADER` | healthy_trades_per_trader | 3.0 |
| `MEMEINTEL_ONCHAIN_WASH_TRADES_PER_TRADER` | wash_trades_per_trader | 10.0 |
| `MEMEINTEL_ONCHAIN_VOLUME_PER_HOLDER_HEALTHY_USD` | volume_per_holder_healthy_usd | 500.0 |
| `MEMEINTEL_ONCHAIN_VOLUME_PER_HOLDER_SUSPICIOUS_USD` | volume_per_holder_suspicious_usd | 5000.0 |
| `MEMEINTEL_ONCHAIN_BUY_RATIO_WEAK` | buy_ratio_weak | 0.35 |
| `MEMEINTEL_ONCHAIN_BUY_RATIO_STRONG` | buy_ratio_strong | 0.60 |

**Token structure (`TokenSubWeights` group `TOKEN_WEIGHTS`; `TokenThresholds` group `TOKEN`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_TOKEN_WEIGHTS_VALUATION` | valuation | 0.20 |
| `MEMEINTEL_TOKEN_WEIGHTS_LIQUIDITY` | liquidity | 0.20 |
| `MEMEINTEL_TOKEN_WEIGHTS_SUPPLY` | supply | 0.15 |
| `MEMEINTEL_TOKEN_WEIGHTS_VOLUME` | volume | 0.15 |
| `MEMEINTEL_TOKEN_WEIGHTS_COMPETITION` | competition | 0.15 |
| `MEMEINTEL_TOKEN_WEIGHTS_CATALYSTS` | catalysts | 0.15 |
| `MEMEINTEL_TOKEN_EARLY_STAGE_MCAP_USD` | early_stage_mcap_usd | 1,000,000.0 |
| `MEMEINTEL_TOKEN_MATURE_STAGE_MCAP_USD` | mature_stage_mcap_usd | 100,000,000.0 |
| `MEMEINTEL_TOKEN_FDV_DILUTION_WARN_RATIO` | fdv_dilution_warn_ratio | 1.5 |
| `MEMEINTEL_TOKEN_FDV_DILUTION_SEVERE_RATIO` | fdv_dilution_severe_ratio | 3.0 |
| `MEMEINTEL_TOKEN_LOW_LIQUIDITY_TO_MCAP_PERCENT` | low_liquidity_to_mcap_percent | 1.0 |
| `MEMEINTEL_TOKEN_HEALTHY_LIQUIDITY_TO_MCAP_PERCENT` | healthy_liquidity_to_mcap_percent | 5.0 |
| `MEMEINTEL_TOKEN_MIN_VOLUME_TO_MCAP_PERCENT` | min_volume_to_mcap_percent | 1.0 |
| `MEMEINTEL_TOKEN_TARGET_VOLUME_TO_MCAP_PERCENT` | target_volume_to_mcap_percent | 20.0 |
| `MEMEINTEL_TOKEN_EXCESSIVE_VOLUME_TO_MCAP_PERCENT` | excessive_volume_to_mcap_percent | 500.0 |
| `MEMEINTEL_TOKEN_MIN_CIRCULATING_FRACTION` | min_circulating_fraction | 0.3 |
| `MEMEINTEL_TOKEN_HEALTHY_CIRCULATING_FRACTION` | healthy_circulating_fraction | 0.9 |

**Foundation (`FoundationSubWeights` group `FOUNDATION_WEIGHTS`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_FOUNDATION_WEIGHTS_MEME_STRENGTH` | meme_strength | 0.20 |
| `MEMEINTEL_FOUNDATION_WEIGHTS_NARRATIVE` | narrative | 0.20 |
| `MEMEINTEL_FOUNDATION_WEIGHTS_BRAND` | brand | 0.15 |
| `MEMEINTEL_FOUNDATION_WEIGHTS_COMMUNITY_QUALITY` | community_quality | 0.20 |
| `MEMEINTEL_FOUNDATION_WEIGHTS_DEV_COMMUNICATION` | dev_communication | 0.15 |
| `MEMEINTEL_FOUNDATION_WEIGHTS_LONG_TERM` | long_term | 0.10 |

**Momentum (`MomentumSubWeights` group `MOMENTUM_WEIGHTS`; `MomentumThresholds` group `MOMENTUM`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_MOMENTUM_WEIGHTS_PRICE` / `VOLUME` / `SOCIAL` / `ONCHAIN` | each | 0.25 each |
| `MEMEINTEL_MOMENTUM_TARGET_TREND_24H_PERCENT` | target_trend_24h_percent | 30.0 |
| `MEMEINTEL_MOMENTUM_SPIKE_1H_PERCENT` | spike_1h_percent | 30.0 |
| `MEMEINTEL_MOMENTUM_LATE_EXTENSION_24H_PERCENT` | late_extension_24h_percent | 100.0 |
| `MEMEINTEL_MOMENTUM_VOLUME_ACCELERATION_RATIO` | volume_acceleration_ratio | 1.5 |
| `MEMEINTEL_MOMENTUM_VOLUME_FADE_RATIO` | volume_fade_ratio | 0.5 |
| `MEMEINTEL_MOMENTUM_BUY_RATIO_SHIFT` | buy_ratio_shift | 0.05 |
| `MEMEINTEL_MOMENTUM_TARGET_SOCIAL_GROWTH_7D_PERCENT` | target_social_growth_7d_percent | 30.0 |

**Narrative / Viral (`NarrativeSubWeights` group `NARRATIVE_WEIGHTS`; `ViralSubWeights` group `VIRAL_WEIGHTS`; `NarrativeThresholds` group `NARRATIVE`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_NARRATIVE_WEIGHTS_MEME_STRENGTH` / `CULTURAL_TIMING` / `VIRAL_POTENTIAL` / `COMMUNITY_CREATIVITY` / `LONG_TERM_STRENGTH` | each | 0.20 each |
| `MEMEINTEL_VIRAL_WEIGHTS_MEMORABILITY` / `SHAREABILITY` / `EMOTIONAL_IMPACT` / `CULTURAL_TIMING` / `COMMUNITY_PARTICIPATION` | each | 0.20 each |
| `MEMEINTEL_NARRATIVE_POSITIVE_SENTIMENT_PERCENT` | positive_sentiment_percent | 60.0 |
| `MEMEINTEL_NARRATIVE_NEGATIVE_SENTIMENT_PERCENT` | negative_sentiment_percent | 40.0 |

**Risk (`RiskSubWeights` group `RISK_WEIGHTS`; `RiskSettings` group `RISK`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_RISK_WEIGHTS_SECURITY` | security | 0.25 |
| `MEMEINTEL_RISK_WEIGHTS_MARKET` | market | 0.20 |
| `MEMEINTEL_RISK_WEIGHTS_COMMUNITY` | community | 0.15 |
| `MEMEINTEL_RISK_WEIGHTS_TOKEN` | token | 0.20 |
| `MEMEINTEL_RISK_WEIGHTS_EXECUTION` | execution | 0.20 |
| `MEMEINTEL_RISK_MAX_OPEN_POSITIONS` | max_open_positions | 10 |
| `MEMEINTEL_RISK_MAX_SINGLE_POSITION_PERCENT` | max_single_position_percent | 10.0 |
| `MEMEINTEL_RISK_MAX_CHAIN_CONCENTRATION_PERCENT` | max_chain_concentration_percent | 50.0 |
| `MEMEINTEL_RISK_MAX_NARRATIVE_CONCENTRATION_PERCENT` | max_narrative_concentration_percent | 40.0 |
| `MEMEINTEL_RISK_MAX_TOTAL_EXPOSURE_PERCENT` | max_total_exposure_percent | 80.0 |
| `MEMEINTEL_RISK_REDUCED_DAILY_LOSS_PERCENT` | reduced_daily_loss_percent | 5.0 |
| `MEMEINTEL_RISK_DEFENSIVE_DAILY_LOSS_PERCENT` | defensive_daily_loss_percent | 10.0 |
| `MEMEINTEL_RISK_REDUCED_WEEKLY_LOSS_PERCENT` | reduced_weekly_loss_percent | 10.0 |
| `MEMEINTEL_RISK_DEFENSIVE_WEEKLY_LOSS_PERCENT` | defensive_weekly_loss_percent | 20.0 |

**Opportunity ranking (`OpportunityWeights` group `OPPORTUNITY_WEIGHTS`)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_OPPORTUNITY_WEIGHTS_GROWTH_POTENTIAL` | growth_potential | 0.30 |
| `MEMEINTEL_OPPORTUNITY_WEIGHTS_MOMENTUM` | momentum | 0.25 |
| `MEMEINTEL_OPPORTUNITY_WEIGHTS_FOUNDATION` | foundation | 0.20 |
| `MEMEINTEL_OPPORTUNITY_WEIGHTS_RISK` | risk | 0.15 |
| `MEMEINTEL_OPPORTUNITY_WEIGHTS_TIMING` | timing | 0.10 |

**Wallet intelligence / smart money (`SmartMoneySubWeights` group `SMART_MONEY_WEIGHTS`; `WalletIntelSettings` group `WALLET`)**
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_SMART_MONEY_WEIGHTS_QUALITY_WALLETS` / `HISTORICAL_SUCCESS` / `ENTRY_TIMING` / `HOLDING_BEHAVIOR` / `RISK_SIGNALS` | each | 0.20 each | Smart Money Confidence Score lenses |
| `MEMEINTEL_WALLET_WHALE_MIN_PERCENT` | whale_min_percent | 1.0 | holder share % counted as a whale |
| `MEMEINTEL_WALLET_RISK_WHALE_PERCENT` | risk_whale_percent | (see code; large-holder threshold) | classifies a holder as `WhaleType.RISK` |
| `MEMEINTEL_WALLET_TARGET_ACCUMULATING_WALLETS` | target_accumulating_wallets | 10 | distinct net buyers earning full marks |
| `MEMEINTEL_WALLET_ARTIFICIAL_SAME_SIZE_FRACTION` | artificial_same_size_fraction | 0.30 | identical-size-trade fraction flagged as scripted |
| `MEMEINTEL_WALLET_DOMINANT_BUYER_VOLUME_FRACTION` | dominant_buyer_volume_fraction | 0.60 | one-wallet buy-share flagged as artificial demand |
| `MEMEINTEL_WALLET_MIN_BUY_VOLUME_FOR_DOMINANCE_USD` | min_buy_volume_for_dominance_usd | 500.0 | dominance check ignored below this $ volume |
| `MEMEINTEL_WALLET_ENABLE_IN_MONITOR` | enable_in_monitor | **false** | **DORMANT switch** — wallet calls in the continuous 24/7 scanner |
| `MEMEINTEL_WALLET_CREDIT_GATE_MIN_SECURITY_SCORE` | credit_gate_min_security_score | 50.0 | candidate's security score floor to spend a wallet lookup |
| `MEMEINTEL_WALLET_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` | credit_gate_max_lookups_per_day | 200 | daily lookup budget (0 = unlimited) |
| `MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES` | credit_gate_cooldown_minutes | 60.0 | per-token repeat-lookup cooldown (0 = off) |

**Social intelligence — LunarCrush (`SocialIntelSettings` group `SOCIAL`, same credit-gate shape, also dormant)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR` | enable_in_monitor | false (dormant) |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_MIN_SECURITY_SCORE` | credit_gate_min_security_score | 50.0 |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` | credit_gate_max_lookups_per_day | 200 |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_COOLDOWN_MINUTES` | credit_gate_cooldown_minutes | 60.0 |

**Related alert-side gate settings (`AlertThresholds` group `ALERTS`, used to align the buy-side alert floor with the wallet credit gate)**
| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE` | momentum_min_security_score | **0.0 (off)** — set to 50 by the enable script alongside the wallet gate |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS` | opportunity_max_age_hours | 1.0 |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD` / `OPPORTUNITY_MAX_MARKET_CAP_USD` | ceilings | 0.0 (off) |

### Key Mechanisms
- **SubScore evidence discipline** (`common.py:33-86`): deduction-style categories start at 100 and subtract findings; signal-style categories (`requires_signal=True`) average explicit 0-100 signals and return `None` with no signals rather than fabricating a perfect 100 from risk deductions alone. `score()` returns `None` when `known_count == 0`.
- **Confidence formula** (`common.py:89-101`): `ConfidenceLevel.MEDIUM` requires `known/(known+unknown) >= MEDIUM_CONFIDENCE_KNOWN_RATIO` (0.40); otherwise `LOW`. Capped below `HIGH` until multi-source confirmation lands.
- **Security destructive override** (`security_analyzer.py:136-138`): any `DESTRUCTIVE` finding (honeypot, cannot-sell, fake token, airdrop scam, live-sell-route-missing, extreme round-trip loss) forces `overall_score = 0.0` regardless of other sub-scores.
- **Security bands** (`security_analyzer.py:34-40`): 90 Excellent / 75 Good / 50 Moderate Risk / 25 High Risk / 0 Extreme Risk.
- **Security risk tier** (`security_analyzer.py:338-343`): `DESTRUCTIVE` if any destructive finding; `SERIOUS_WARNING` if overall < 50 or any serious-warning finding; else `ACCEPTABLE_UNCERTAINTY`.
- **Community fake-community override** (`community_analyzer.py:150-154`): `bot_follower_percent >= bot_follower_artificial_percent` (default 50%) flags destructive → `overall_score = 0`, `rating = ARTIFICIAL`, which the scoring engine treats as a hard red-flag (`is_artificial`).
- **Master weighted score** (`scoring_engine.py:351-365`): `sum(score*weight for scored categories) / sum(weight for scored categories)`, clamped to [0,100] to guard float overshoot on all-100 inputs (documented bug-hunt fix). Raises `InsufficientDataError` if no category has data.
- **Red-flag overrides → forced Avoid** (`scoring_engine.py:258-267`): any security `destructive_findings`, `community.is_artificial`, or `risk.category is EXTREME`.
- **Six-question decision tree** (`scoring_engine.py:271-347`, Part 10 §7): (1) contract safe? destructive or `contract score < 30` → reject; (2) liquidity healthy? `< 40` → cap at Speculative; (3) community real? artificial → reject; (4) on-chain healthy? `< 40` → cap at Speculative; (5) narrative growth potential? `< 40` → informational only, no cap; (6) risk/reward attractive? `EXTREME` → reject, `HIGH` → cap at Speculative. Unknown answers never pass or fail — they continue "with reduced confidence."
- **Classification bands** (`core/models.py:426-443`, `settings.ClassificationBands`): >=90 Elite, >=80 Strong Candidate, >=70 Watchlist, >=60 Speculative, else Avoid. Applied only after overrides/rejections are cleared, then caps from the decision tree are applied (worst wins, via `_CLASS_ORDER`).
- **Timing heuristic** (`scoring_engine.py:129-161`, `derive_timing_score`): averages pool-age bucket score (24h=90, 7d=70, 30d=50, else 30), market-cap stage score (EARLY 85/GROWTH 60/MATURE 30), and market-phase score (ACCUMULATION 80/EXPANSION 70/UNCLEAR 50/DISTRIBUTION 20); `None` if nothing observable.
- **Momentum entry-zone logic** (`momentum_analyzer.py:284-307`): `LATE` if 24h change >= `late_extension_24h_percent` (100%); else `EARLY` if pool age <= 24h; else `CONFIRMATION` if overall score >= `_ACTION_WAIT_MIN` (45); else `UNCLEAR`. Preferred action: `LATE` zone → `MONITOR` (never chase distribution); overall < 30 → `AVOID`; overall >= 70 and zone in (EARLY, CONFIRMATION) → `CONSIDER_RESEARCH_ENTRY`; overall >= 45 → `WAIT_FOR_CONFIRMATION`; else `MONITOR`.
- **Momentum trend consistency** (`momentum_analyzer.py:192-202`): all of 1h/6h/24h positive → 90 signal; 24h positive but 1h negative ("fading") → 35; else mixed → 60. A +30%/hr spike (`spike_1h_percent`) deducts 15 points as an unsupported-vertical-move warning (Part 26 doctrine: price alone isn't momentum).
- **Momentum volume acceleration** (`momentum_analyzer.py:206-223`): `(volume_1h*24)/volume_24h`; >= 1.5 → 90 signal; <= 0.5 → 30 signal + fading-volume note; between → scaled 30-90.
- **Fake-momentum guard** (`momentum_analyzer.py:256-263`): if the on-chain `volume_quality` sub-score < 40, momentum's on-chain lens deducts 25 points ("momentum is built on suspect volume quality") instead of crediting it.
- **Wallet whale classification** (`wallet_intelligence.py:245-271`, Part 17 §6): holders >= `whale_min_percent` (1.0%) classified — pool address or known-exchange owner → `CUSTODIAL`; >= `risk_whale_percent` → `RISK`; net buy/sell activity → `TRADING`; otherwise `LONG_TERM`.
- **Smart Money Confidence Score** (`wallet_intelligence.py:159-165`, Part 17 §11): five lenses at 20% each — quality_wallets (count of net buyers above dust, scaled to `target_accumulating_wallets`=10), historical_success (requires reputation data from Part 24 outcome tracking; reports "no data" until then), entry_timing (fraction of buy volume in the lower half of the observed price range: >=60% → accumulation signal 80, <=40% → chasing signal 40, else mixed 60), holding_behavior (fraction of personal whales still holding vs. trading/risk-classified), risk_signals (scripted same-size trades >= `artificial_same_size_fraction`=0.30 deducts 30; one wallet >= `dominant_buyer_volume_fraction`=0.60 of buy volume (above `min_buy_volume_for_dominance_usd`=$500 to avoid dust false-positives) deducts 30; >=2 top holders still net-buying deducts 15 as a concentration-growth warning).
- **Pump-and-exit detection** (`wallet_intelligence.py:392-412`, Part 18 §9): price up >= 50% in 24h while personal whales' net outflow >= $500 → `SERIOUS_WARNING` finding "promotion-and-exit pattern".
- **Accumulation verdict** (`wallet_intelligence.py:416-433`, Part 17 §5): `ARTIFICIAL` if any risk_signals destructive-tier finding; `HEALTHY` if accumulating-wallet count >= max(3, target/2) AND total buy $ > total sell $; else `MIXED`; `UNKNOWN` with no trade data.
- **Wallet credit gate** (`workflow/pipeline.py`, settings `WalletIntelSettings`): a wallet lookup only fires when `wallet_service` is configured AND chain is Solana AND (`force_wallet_check` OR `_gate_allows()`). `_gate_allows` = `_worth_wallet_lookup` (candidate not destructive, security score >= `credit_gate_min_security_score`=50, tradeable liquidity & market cap known and non-zero, within the alert engine's max-liquidity/max-mcap ceilings and `opportunity_max_age_hours`=1h freshness window) AND cooldown not active (60 min per token) AND daily budget not exhausted (200/day, resets per UTC day, logs a one-time warning on exhaustion). Operator holdings, `/check`, and plan/report lookups pass `force_wallet_check=True` and bypass the gate entirely (deliberate, rare spend the gate is not meant to block). The identical shape gates the dormant social/LunarCrush lookups (`_social_worth_lookup`/`_gate_allows` mirror pair).
- **Risk category bands** (`risk_analyzer.py:43-51`, Part 9 §7, higher=riskier): >=75 Extreme, >=50 High, >=25 Moderate, else Low-Relative. Coverage guard: if available weight < 0.5 (mostly unverifiable), category floors at `HIGH` even if the computed score would say Low/Moderate — "unknown is not safe."
- **Opportunity rank** (`opportunity_ranker.py`, Part 28 §5): a second axis over the SAME already-computed category scores — growth_potential←narrative (30%), momentum←momentum (25%), foundation←foundation (20%), risk←(100−risk_score) (15%), timing←timing (10%) — renormalized over available factors; never mutates or is read back into the master classification.

### Telegram / CLI Surface
- `/check <address>` (Telegram) and CLI `check` map to `MemeIntelligenceController.check_token`, which calls `pipeline.analyze_pair(..., force_wallet_check=True, force_social_check=True)` — a manual operator lookup deliberately bypasses both the wallet and social credit gates, always running the full analyzer stack including `WalletIntelligenceAnalyzer` when a wallet service is configured.
- CLI `wallets <address> --chain solana` (`meme_intelligence/__main__.py:841, 1160, 1250-1252`) directly drives `WalletIntelligenceAnalyzer` / `WalletDataService` for an on-demand smart-money report — one of the "report, plan, wallets commands" the wallet module's docstring says it runs on-demand for, independent of the continuous-scanner gate.
- Plan/report-style operator-initiated deep research also forces `force_wallet_check=True`/`force_social_check=True` (workflow/controller.py comment at line ~484-486, mirrors `/check`).
- `/status` (Telegram, via `MemeIntelligenceController.status`) surfaces `self._layers["wallet_intel"]` = whether `wallet_service` was actually wired in, so the operator can see if wallet tracking is active without reading logs.
- No standalone Telegram command directly triggers `ScoringEngine`, `RiskAnalyzer`, or `OpportunityRanker` — they run inside the shared pipeline that every scan, `/check`, and report path calls, so their output surfaces through the alert messages and `/check` card rather than a dedicated command.

### Dormant Features Here
1. **Wallet intelligence in the continuous scanner** — `WalletIntelligenceAnalyzer` and its Smart Money Confidence Score are fully built and correct, but OFF by default in the 24/7 monitor (`MEMEINTEL_WALLET_ENABLE_IN_MONITOR=false`). It only runs there when a candidate clears the credit gate (security score >= 50, tradeable, within age/size limits, under the 200/day budget and 60-min cooldown). **To enable**: run `bash deploy/enable-wallet-tracking.sh <PAID_HELIUS_API_KEY>` — sets `MEMEINTEL_HELIUS_API_KEY`, `MEMEINTEL_WALLET_ENABLE_IN_MONITOR=true`, and aligns `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE=50` (also 0/off by default) so momentum alerts share the same security floor as wallet lookups. Requires a PAID Helius plan — the free tier was exhausted in ~3 days when this ran unconditionally on 2026-07-11, which is why the credit gate exists at all. Disable with the same script passing `off`.
2. **Social/LunarCrush intelligence** (`SocialIntelSettings`, feeds `CommunityProfile`'s `social_volume_24h`/`galaxy_score`/etc. and the narrative engine's social-trend inputs) — same dormant-by-default, credit-gated shape as wallet intelligence (`MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR=false`). **To enable**: `bash deploy/enable-x-community-tracking.sh` with a paid LunarCrush key (`MEMEINTEL_LUNARCRUSH_API_KEY`). Until enabled, community engagement/growth signals derived from Twitter (`twitter_engagement_rate_percent`, `bot_follower_percent`, etc.) and momentum's social lens stay `None`/"no data", lowering coverage and confidence rather than being fabricated.
3. **`historical_success` lens of the Smart Money Confidence Score** — always reports "no data" (`wallet_intelligence.py:288-297`) until Part 24's outcome-tracking system (`WalletTrackRecord`/`wallet_reputation` helpers, already built at the bottom of `wallet_intelligence.py`) accumulates enough wallet track records to populate `reputations`. No config flag — it activates automatically once the learning/outcome-tracking layer starts passing a non-empty `reputations` dict into `WalletIntelligenceAnalyzer.assess()`.
4. **On-chain smart-money/whale/token-flow sub-scores without wallet service** — `derive_onchain_profile` only fills holder/developer/trading-behavior fields from market+security data; `smart_wallet_count`, `whale_net_flow_usd`, `exchange_inflow_usd/outflow_usd` stay `None` (sub-scores "no data") until `enrich_onchain_profile(profile, wallet_assessment)` is called after a wallet lookup — i.e., this half of the on-chain score is implicitly gated by the same wallet credit gate.
5. **AI/analyst judgment slots** (Foundation's `meme_strength`/`narrative`/`brand`/`dev_communication`/`long_term`; Narrative's memorability/shareability/emotional_impact/cultural_timing/meme_strength/long_term_strength/catalysts) — the analyzer code accepts these as pre-computed 0-100 inputs from "the AI reasoning layer (Parts 13/23) or a human analyst"; they are `None` by default and every affected sub-score reports "not assessed"/"no data" rather than being invented, until the AI service (`AISettings`, `MEMEINTEL_AI_*`) is wired and populates them.
6. **Multi-source confidence upgrade to HIGH** — `confidence_from_facts` in `common.py` is hard-capped at `MEDIUM` (never `HIGH`) "while data comes from a single source per category," pending the provider-pool/multi-source integration; no config flag, this is a code-level cap tied to future architecture work.

### Integration Points
**Reads from (upstream):**
- Collectors normalize provider payloads (GoPlus, DexScreener/GeckoTerminal, CoinGecko, Helius/Birdeye, PumpPortal/Pump.fun, Jupiter round-trip probe) into the shared `core/models.py` dataclasses (`SecurityProfile`, `DexPair`, `CommunityProfile`, `WalletIntelData`) that every analyzer consumes — analyzers never see raw API responses (Part 32/Rule 3).
- `derive_onchain_profile()` (onchain_analyzer.py) builds a partial `OnChainProfile` directly from a `DexPair` + `SecurityProfile` without needing the wallet collectors.
- `enrich_onchain_profile()` (wallet_intelligence.py) back-fills the wallet-dependent slots of an `OnChainProfile` from a `WalletAssessment`.
- `merge_community_profiles()` (community_analyzer.py) layers a paid social source (e.g. future LunarCrush) on top of a free one (CoinGecko) without regressing coverage.

**Called by (downstream / orchestration):**
- `meme_intelligence/workflow/pipeline.py` is the sole orchestrator that instantiates and sequences every analyzer per candidate pair: security → (gated) wallet → onchain → community → foundation → token → momentum → narrative → risk → `ScoringEngine.evaluate()` → (optional) `OpportunityRanker.rank()`. It owns the wallet/social credit-gate bookkeeping (daily counters, cooldown timestamps) described above.
- `meme_intelligence/workflow/controller.py` exposes the pipeline's results to Telegram/CLI (`/check`, `/status`), and is the source of `force_wallet_check`/`force_social_check` bypass semantics for operator-initiated lookups.
- `meme_intelligence/alerts/` (alert/notification engine) consumes `MasterAssessment.classification`/`.overrides`/`.category_scores`, `SecurityAssessment.findings`, `security_monitor.detect_security_changes()`, and `RiskAnalyzer.emergency_flags()` to decide what fires as a Telegram/Discord alert and at what priority.
- `meme_intelligence/database/` persists analyzer outputs (for `security_monitor`'s fact-diffing across scans, for watchlist tier decisions using `OpportunityRank`, and for backtesting/learning).
- `meme_intelligence/learning/` (mind/rug-engine layer) reads deterministic, zero-API-cost facts (contract facts, deployer blacklist, observed dev outflow) that double as the pre-check gating whether a paid AI verification call is spent — a credit-conservation pattern parallel to, but separate from, the wallet/social credit gates.
- `meme_intelligence/ai/` (Part 23 reasoning layer) is the intended future supplier of the qualitative judgment slots in `FoundationInputs`/`NarrativeInputs` and of `AIJudgmentService` verification, consumed by `TokenAnalyzer.assess(competition=..., catalysts=...)` and `NarrativeAnalyzer.assess(inputs=...)`.

**Never does:** no analyzer or the scoring engine places or signals trades — `RiskAnalyzer`/`PortfolioRiskManager` and `TradingSettings` produce guidance/limits only; live execution (if the operator has explicitly enabled it) lives entirely outside this subsystem in `meme_intelligence/trading/`.

---

## 3. Mind / Learning Layer

### Overview
The Mind / Learning Layer is the bot's self-learning, decision-support-only subsystem (Rule 21 — it never trades). Every coin the scanner surfaces enters a lifecycle: detection -> repeated trajectory snapshots -> outcome resolution at one or more horizons -> a labeled training example. From that growing labeled set, three independent probabilistic opinions are formed and blended:

1. **Analog memory** (`analog.py`) — a FAISS append-only nearest-neighbor index of past resolved coins' fingerprints; a live coin's outcome distribution is a recency-weighted vote of its k most similar historical analogs ("instant learning" — no retraining needed, an insert the moment a coin resolves is immediately usable).
2. **LightGBM classifier** (`classifier.py`) — a warm-started multiclass model over the same fingerprints, capturing nonlinear feature interactions a k-NN lookup misses ("periodic learning" — retrained on a schedule/drift trigger, not per-coin).
3. **Rug engine** (`rug_engine.py`) — a rules-based, explainable 0-100 rug-risk score from hard on-chain/trajectory/reputation facts (contract security flags, LP behavior, dev-wallet outflow, wash-trading heuristics, deployer blacklist), always available (no cold start).

These three are blended by an **adaptive accuracy-weighted ensemble** (`ensemble.py`) whose per-source blend weights track each source's own recent, rolling, Laplace-smoothed accuracy — so whichever source is currently most reliable earns more say, and abstentions (cold-start classifier, too-few analog neighbors) are excluded and the rest renormalize. Layered on top: **archetype discovery + novelty detection** (`archetypes.py`, HDBSCAN over the same fingerprint space) names recurring coin "personalities" and flags coins that resemble nothing seen before. **Self-evaluation metrics** (`metrics.py`) grade the system's own track record (directional hit-rate, rug precision/recall/F1, Brier calibration score, per-archetype accuracy, novelty hit-rate) — these numbers both feed the operator-facing `/mind` report card and gate a P(rug) alert veto that must literally *earn* the authority to block a HIGH alert by clearing a measured rug-precision bar over a minimum sample of graded rug calls (`veto_gate`). Everything (fingerprint version, labeled SQLite records, FAISS index, LightGBM booster, StandardScaler, archetype centroids, ensemble accuracy history) is persisted to a shared `learning_state/` directory so learning compounds across restarts and across the two OS processes that touch it concurrently (the always-on monitor and the periodic backtest/resolution cron) — this cross-process persistence/reload logic was reworked most recently and is the most fragile part of the subsystem, guarded by three independent "dirty" ownership flags plus a feature-version gate.

The whole layer is OFF by default at two independent switches (`learning.enabled` is unused directly by these files but the effective gate the monitor checks is `enable_in_monitor`, and the alert veto has its own separate `veto_enabled` flag) — it was designed to be bolted onto the existing scanner/security/alerts pipeline additively, feeding on data those modules already collect, at zero extra API cost, and failing open (never breaking a scan cycle or blocking an alert) on any internal error.

### Files
| File | Role |
|---|---|
| `meme_intelligence/learning/models.py` | Core dataclasses: `OutcomeBucket` (PUMP/FLAT/DUMP/RUG/UNRESOLVED), `CoinSnapshot` (one trajectory point, all fields optional), `OutcomeLabel`, `RugSignal`/`RugAssessment`, `AnalogNeighbor`, `CoinRecord` (full lifecycle + `final_bucket` logic: RUG always dominates), `CoinVerdict` (the public per-coin API response shape + `to_dict()`), `uniform_distribution()` (0.25 each — the honest cold-start prior). |
| `meme_intelligence/learning/store.py` | `LearningStore` — SQLite persistence (separate DB file `learning.db`, WAL mode) for coin lifecycle rows, snapshot series, resolved labels, fired rug signals, the deployer blacklist, self-evaluation metrics history, and first-evaluation prediction records (insert-once, used for later grading). |
| `meme_intelligence/learning/features.py` | `FingerprintExtractor` — compresses a variable-length snapshot series into a fixed-length "fingerprint" vector (7 summary stats × 13 base metrics + 3 scalar context features + 13 presence-mask flags = `FEATURE_DIM`). Heavy-tailed USD/count metrics go through signed log1p before summarizing (shape, not scale). `StandardScalerBundle` — persisted sklearn `StandardScaler` wrapper; identity passthrough before first fit. `FEATURE_VERSION` = 3 gates cross-version compatibility of all downstream artifacts. |
| `meme_intelligence/learning/analog.py` | `AnalogMemory` — FAISS `IndexFlatIP` (exact cosine-similarity search) over L2-normalized scaled fingerprints; append-only inserts (instant learning), k-NN query + recency-weighted vote, atomic save/load with a feature-version check. |
| `meme_intelligence/learning/classifier.py` | `OutcomeClassifier` — warm-started LightGBM multiclass (fixed 4-class output) model with exponential time-decay + optional class-balance sample weights; falls back to a full retrain if a warm-start fails; returns `None` below `min_train_samples` (cold start). |
| `meme_intelligence/learning/rug_engine.py` | `RugEngine` — 9 independent, positive-evidence-only hard rug signals (unsellable/honeypot, mint/freeze authority, holder concentration, LP lock, sell tax, liquidity removal, dev-wallet dumping, fake/wash volume, deployer blacklist) summed into a clamped 0-100 score with human-readable reasons. |
| `meme_intelligence/learning/archetypes.py` | `ArchetypeModel` — HDBSCAN clustering of the scaled fingerprint set into named archetypes (dominant outcome per cluster) plus an empirical-percentile novelty score for live coins. |
| `meme_intelligence/learning/ensemble.py` | `AdaptiveEnsemble` — accuracy-weighted blend of the 3 sources; Laplace-smoothed rolling per-source accuracy; separate rolling "final" (blended-verdict) accuracy that the drift monitor watches; `rug_score_to_distribution()` turns the rug engine's scalar score into a 4-label distribution. |
| `meme_intelligence/learning/metrics.py` | Pure functions computing the self-evaluation metric set from graded `PredictionRecord`s: directional hit-rate, rug precision/recall/F1, Brier score, confidence-calibration bins, per-archetype accuracy, novelty hit-rate; `veto_gate()` — the earned-authority check for the alert veto. |
| `meme_intelligence/learning/service.py` | `LearningService` — the orchestrator/public entry point (`evaluate_coin`, `record_detection`, `capture_snapshot`, `resolve_outcome`, `retrain_if_due`, `refresh_archetypes`, `get_learning_metrics`) that wires all of the above together, owns the on-disk artifact persistence/reload with cross-process ownership guards, and drives the drift-triggered full rebuild. |

### Configuration
All values below are the literal defaults read from `meme_intelligence/config/settings.py`. Env-var group prefixes confirmed from `Settings.from_env()`'s `_load_group(...)` calls: `LearningSettings` -> `MEMEINTEL_LEARNING_*`, `LightGBMSettings` -> `MEMEINTEL_LIGHTGBM_*`, `RugThresholds` -> `MEMEINTEL_RUG_THRESHOLDS_*`, `RugSignalWeights` -> `MEMEINTEL_RUG_SIGNAL_WEIGHTS_*`.

**LearningSettings (`meme_intelligence/config/settings.py:1196`)**

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_LEARNING_ENABLED` | `enabled` | `False` | Master opt-in flag for the mind layer (currently unused directly inside `learning/`; the monitor's real feed-gate is `enable_in_monitor`). |
| `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` | Whether the always-on scanner feeds coins into the mind layer at all (`LearningService` is constructed either way in `__main__.py`; this flag gates whether the controller wires it into the scan loop). |
| `MEMEINTEL_LEARNING_PUMP_RETURN_PERCENT` | `pump_return_percent` | `50.0` | Forward return %% at/above which a resolved horizon labels PUMP. |
| `MEMEINTEL_LEARNING_DUMP_RETURN_PERCENT` | `dump_return_percent` | `-50.0` | Forward return %% at/below which a resolved horizon labels DUMP (else FLAT). |
| `MEMEINTEL_LEARNING_HORIZONS_HOURS` | `horizons_hours` | `"0.25,1,6,24"` | Comma-separated resolution horizons (+15m/+1h/+6h/+24h), parsed by `horizon_hours()`. |
| `MEMEINTEL_LEARNING_KNN_NEIGHBORS` | `knn_neighbors` | `25` | k for the FAISS analog query. |
| `MEMEINTEL_LEARNING_RECENCY_HALF_LIFE_DAYS` | `recency_half_life_days` | `30.0` | Half-life (days) for analog-neighbor recency decay in the vote. |
| `MEMEINTEL_LEARNING_MIN_ANALOG_NEIGHBORS` | `min_analog_neighbors` | `5` | Below this many neighbors, the analog vote abstains (uniform distribution). |
| `MEMEINTEL_LEARNING_ARCHETYPE_MIN_CLUSTER_SIZE` | `archetype_min_cluster_size` | `15` | HDBSCAN `min_cluster_size` (must be >= 2). |
| `MEMEINTEL_LEARNING_NOVELTY_PERCENTILE` | `novelty_percentile` | `90.0` | Novelty percentile at/above which a coin is flagged "new pattern". |
| `MEMEINTEL_LEARNING_RETRAIN_EVERY_N` | `retrain_every_n` | `200` | Warm-start the classifier after this many newly-resolved coins. |
| `MEMEINTEL_LEARNING_MODEL_HALF_LIFE_DAYS` | `model_half_life_days` | `30.0` | Half-life (days) for LightGBM sample time-decay weighting. |
| `MEMEINTEL_LEARNING_MIN_TRAIN_SAMPLES` | `min_train_samples` | `50` | Below this many resolved coins, the classifier stays untrained (returns `None`). |
| `MEMEINTEL_LEARNING_ACCURACY_WINDOW` | `accuracy_window` | `200` | M — rolling window size for each ensemble source's accuracy history. |
| `MEMEINTEL_LEARNING_MIN_ENSEMBLE_CONFIDENCE` | `min_ensemble_confidence` | `0.0` | Configurable confidence floor (kept for future use). |
| `MEMEINTEL_LEARNING_DRIFT_ACCURACY_FLOOR` | `drift_accuracy_floor` | `0.40` | Blended-verdict rolling accuracy below this triggers a full rebuild. |
| `MEMEINTEL_LEARNING_DRIFT_MIN_SAMPLES` | `drift_min_samples` | `30` | Minimum graded finals needed before drift can fire. |
| `MEMEINTEL_LEARNING_SCALER_REFIT_EVERY_N` | `scaler_refit_every_n` | `500` | Cadence (resolved coins) for refitting the StandardScaler (forces full rebuild). |
| `MEMEINTEL_LEARNING_FAST_SNAPSHOT_SECONDS` | `fast_snapshot_seconds` | `60` | Snapshot cadence during the fast capture window. |
| `MEMEINTEL_LEARNING_FAST_WINDOW_MINUTES` | `fast_window_minutes` | `60` | Duration of the fast snapshot cadence. |
| `MEMEINTEL_LEARNING_SLOW_SNAPSHOT_MINUTES` | `slow_snapshot_minutes` | `60` | Snapshot cadence after the fast window ends. |
| `MEMEINTEL_LEARNING_CAPTURE_UNTIL_HOURS` | `capture_until_hours` | `24.0` | Stop capturing trajectory snapshots after this coin age. |
| `MEMEINTEL_LEARNING_MIN_SNAPSHOTS_FOR_CONFIDENCE` | `min_snapshots_for_confidence` | `3` | Fewer snapshots than this scales down `model_confidence` (`snap_factor`). |
| `MEMEINTEL_LEARNING_COLD_START_SAMPLES` | `cold_start_samples` | `100` | Resolved-coin count below which `cold_factor` discounts confidence. |
| `MEMEINTEL_LEARNING_VETO_ENABLED` | `veto_enabled` | `False` | Master switch for the P(rug) HIGH-alert veto (Project 3 / ROADMAP #3). DORMANT by default. |
| `MEMEINTEL_LEARNING_VETO_MIN_P_RUG` | `veto_min_p_rug` | `0.85` | Ensemble P(rug) at/above this (once authority is earned) vetoes a HIGH alert. |
| `MEMEINTEL_LEARNING_VETO_MIN_ACCURACY` | `veto_min_accuracy` | `0.70` | Measured rug PRECISION floor required to earn veto authority. |
| `MEMEINTEL_LEARNING_VETO_MIN_SAMPLES` | `veto_min_samples` | `10` | Minimum graded rug calls (TP+FP) required before authority can be earned. |
| `MEMEINTEL_LEARNING_VETO_METRICS_TTL_SECONDS` | `veto_metrics_ttl_seconds` | `1800.0` | How long the earned-authority check (`_mind_veto_authority`) is cached in the controller. |
| `MEMEINTEL_LEARNING_STATE_DIR` | `state_dir` | `"learning_state"` | Directory holding `learning.db`, the FAISS index, LightGBM model, scaler, archetypes, and ensemble history. |

**LightGBMSettings (`settings.py:1321`)**

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_LIGHTGBM_FULL_RETRAIN_ROUNDS` | `full_retrain_rounds` | `120` | Tree budget for a from-scratch train. |
| `MEMEINTEL_LIGHTGBM_WARM_START_ROUNDS` | `warm_start_rounds` | `30` | Trees added per warm-start retrain. |
| `MEMEINTEL_LIGHTGBM_LEARNING_RATE` | `learning_rate` | `0.05` | LightGBM learning rate. |
| `MEMEINTEL_LIGHTGBM_NUM_LEAVES` | `num_leaves` | `31` | LightGBM `num_leaves`. |
| `MEMEINTEL_LIGHTGBM_MIN_CHILD_SAMPLES` | `min_child_samples` | `5` | Maps to `min_data_in_leaf`. |
| `MEMEINTEL_LIGHTGBM_BALANCED_CLASS_WEIGHTS` | `balanced_class_weights` | `True` | Upweight rare classes (rugs/pumps) using sklearn's balanced formula. |

**RugThresholds (`settings.py:1478`)**

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_RUG_THRESHOLDS_MIN_LP_LOCKED_PERCENT` | `min_lp_locked_percent` | `50.0` | Below this -> "liquidity not locked" fires. |
| `MEMEINTEL_RUG_THRESHOLDS_TOP_HOLDER_PERCENT_MAX` | `top_holder_percent_max` | `30.0` | Single holder above this -> concentration signal. |
| `MEMEINTEL_RUG_THRESHOLDS_TOP10_HOLDER_PERCENT_MAX` | `top10_holder_percent_max` | `70.0` | Top-10 holders above this -> concentration signal. |
| `MEMEINTEL_RUG_THRESHOLDS_LIQUIDITY_DROP_PERCENT` | `liquidity_drop_percent` | `50.0` | Fall from trajectory peak liquidity -> "liquidity removed". |
| `MEMEINTEL_RUG_THRESHOLDS_LIQUIDITY_REMOVAL_USD` | `liquidity_removal_usd` | `1000.0` | Single LP-remove event magnitude to fire "liquidity removed". |
| `MEMEINTEL_RUG_THRESHOLDS_SELL_TAX_MAX_PERCENT` | `sell_tax_max_percent` | `20.0` | Sell tax at/above this -> "high sell tax". |
| `MEMEINTEL_RUG_THRESHOLDS_DEV_DUMP_USD` | `dev_dump_usd` | `1000.0` | Creator outflow at/above this -> "dev wallet dumping". |
| `MEMEINTEL_RUG_THRESHOLDS_FAKE_VOLUME_PER_HOLDER_USD` | `fake_volume_per_holder_usd` | `5000.0` | Volume/holder ratio above this -> "fake volume". |
| `MEMEINTEL_RUG_THRESHOLDS_FAKE_VOLUME_MIN_VOLUME_USD` | `fake_volume_min_volume_usd` | `1000.0` | Minimum absolute volume required before the fake-volume check is even evaluated. |

**RugSignalWeights (`settings.py:1509`, additive points, clamped to 100 total)**

| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_LIQUIDITY_UNLOCKED` | `liquidity_unlocked` | `20.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_MINT_AUTHORITY_ACTIVE` | `mint_authority_active` | `20.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_FREEZE_AUTHORITY_ACTIVE` | `freeze_authority_active` | `15.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_TOP_HOLDER_CONCENTRATION` | `top_holder_concentration` | `15.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_LIQUIDITY_REMOVED` | `liquidity_removed` | `30.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_UNSELLABLE` | `unsellable` | `30.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_HIGH_SELL_TAX` | `high_sell_tax` | `15.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_DEV_WALLET_DUMPING` | `dev_wallet_dumping` | `20.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_FAKE_VOLUME` | `fake_volume` | `10.0` |
| `MEMEINTEL_RUG_SIGNAL_WEIGHTS_DEPLOYER_BLACKLISTED` | `deployer_blacklisted` | `25.0` |

Note: elsewhere in the pipeline (`workflow/controller.py`), `AISettings.verify_skip_rug_score` (`MEMEINTEL_AI_*` group, not part of this subsystem's own files) is the threshold at which the rug engine's score alone skips paid AI verification — listed here only because `_deterministic_risk_veto` calls the same `RugEngine` this subsystem defines.

### Key Mechanisms
- **Fingerprint vector** (`features.py:107-130`): `FEATURE_DIM` = 13 base metrics × 7 summary stats (`last, mean, min, max, delta, slope, vol`) + 3 scalar features (`age_hours, snapshot_count, price_acceleration`) + 13 presence-mask flags. `FEATURE_VERSION = 3` (v2 added log-domain transform, v3 added presence masks); any artifact saved under a different version is discarded and rebuilt (`service.py:153-186`).
- **Log-domain transform** (`features.py:89-100`): USD/count metrics (`price, liquidity, market_cap, volume_5m, volume_1h, holders, dev_outflow, liquidity_event, tx_count`) go through signed `log1p` before summarizing so trajectory shape, not absolute size, drives similarity.
- **Slope/acceleration** (`features.py:146-195`): least-squares slope per hour via `np.cov`; price acceleration is the slope of consecutive per-snapshot returns (needs >=3 price points).
- **Cosine analog similarity** (`analog.py:76-104`): fingerprints L2-normalized, stored in FAISS `IndexFlatIP` (exact search); similarity clamped to `[0,1]` — a fingerprint pointing the "wrong way" is weight ~0, never a negative vote.
- **Analog recency-weighted vote** (`analog.py:206-248`): `weight = similarity × exp(-age_days / recency_half_life_days)`; abstains (uniform distribution, `abstained=True`) below `min_analog_neighbors` or when total weight <= 0; neighbors resolving to a non-training-label bucket are excluded from both numerator and denominator.
- **LightGBM sample weights** (`classifier.py:94-138`): `exp(-age_days / model_half_life_days)` time decay × optional sklearn balanced-class weight (`N / (n_classes_present × class_count)`), then mean-normalized to average 1.0 (absolute scale = N) so LightGBM's `min_sum_hessian_in_leaf` floor never silently suppresses splits on an all-old batch.
- **Warm-start vs full retrain** (`classifier.py:140-199`, `service.py:579-665`): `retrain_if_due` triggers on (a) first-train threshold `min_train_samples`, (b) cadence `retrain_every_n`, or (c) drift. A scaler refit (drift, first fit, or cadence `scaler_refit_every_n`) forces `full=True`: full classifier retrain from scratch + full analog-index rebuild + archetype refit, so every model shares one feature space. Warm-start failures fall back to a full retrain automatically.
- **Drift detection** (`service.py:632-637`): blended-verdict rolling accuracy (`AdaptiveEnsemble.final_accuracy()`) below `drift_accuracy_floor` over at least `drift_min_samples` graded finals triggers the full rebuild path; `reset_final_history()` clears stale grades afterward so the trigger doesn't keep re-firing against replaced models.
- **Adaptive ensemble weight formula** (`ensemble.py:91-148`): `weight(source) = laplace_accuracy(source) / Σ laplace_accuracy(available sources)`, `laplace_accuracy = (correct+1)/(total+2)` over a rolling `accuracy_window` (M=200) deque per source. Unavailable sources are excluded per-coin and remaining weights renormalize; all-unavailable returns uniform + `abstained=True`.
- **Rug-score-to-distribution** (`ensemble.py:59-74`): `P(rug) = clamp(rug_score/100, 0, 1)`; remaining mass split evenly across PUMP/FLAT/DUMP — the rug engine has an opinion about rug-vs-not only.
- **Archetype novelty** (`archetypes.py:164-194`): novelty score = empirical percentile — fraction of training points whose nearest-centroid distance is smaller than this coin's distance. HDBSCAN noise points (label -1) still feed the novelty distribution, not the archetypes themselves. `novelty_flagged` in the verdict pipeline = `novelty_score*100 >= novelty_percentile` (default 90).
- **Confidence discounting** (`service.py:383-389`): `model_confidence = ensemble.confidence × cold_factor × snap_factor`, where `cold_factor = min(1, resolved_count/cold_start_samples)` and `snap_factor = min(1, len(snapshots)/min_snapshots_for_confidence)` — an explicit Rule-8 "reduce confidence on missing data" mechanism independent of any single source's own abstention.
- **Slow-rug re-grading** (`service.py:528-575`, `_on_rug_upgrade`): when a later horizon upgrades an already-resolved coin's bucket to RUG (the common "slow rug" shape where the +1h window still looks FLAT), the deployer blacklist is grown (once, atomically via `mark_deployer_counted`), the stored prediction is re-graded against RUG exactly once (`rug_regraded` flag) so sources that correctly flagged the slow rug aren't punished for the earlier premature grade, and a corrected-RUG fingerprint is inserted into the analog index immediately (the stale earlier entry is left until the next full rebuild).
- **P(rug) alert veto + earned-authority gate** (`workflow/controller.py:887-952`, `metrics.py:161-177`): `veto_gate(metrics, min_accuracy, min_samples)` returns `(precision, graded_rug_calls)` only when rug precision (TP/(TP+FP)) over at least `veto_min_samples` graded rug calls (TP+FP) clears `veto_min_accuracy`; otherwise `None` (abstain). The controller caches this gate for `veto_metrics_ttl_seconds` (`_mind_veto_authority`). The veto itself (`_mind_layer_veto`) fires only when `veto_enabled` AND the gate is non-`None` AND live ensemble `P(rug) >= veto_min_p_rug`; any exception during evaluation fails open (no veto). This is the 3rd of 3 free deterministic screens a HIGH opportunity must clear before an AI-verified alert fires (`_deterministic_risk_veto`).
- **Cross-process ownership guards** (`service.py:113-146, 202-313`, freshly reworked): three independent boolean "dirty" latches — `_analog_dirty` (this process holds un-flushed analog-index mutations from `_on_resolved`/`_on_rug_upgrade`/a full `_rebuild`; NOT set by a plain warm-start, which leaves the analog index untouched), `_models_dirty` (this process retrained scaler/classifier/archetypes), `_ensemble_dirty` (this process graded an outcome into the ensemble accuracy history). `persist()` writes each artifact group only if its guard is set, so a pure reader (e.g. a monitor process that only calls `evaluate_coin`) never clobbers a peer process's (e.g. the backtest cron's) grown copy. `_maybe_reload_analog()` picks up a peer's on-disk growth via an mtime check on `index_meta.joblib` (written last by `save()`, so a reload never observes a torn index/metadata pair), refusing to reload while local mutations are unflushed or after a feature-version mismatch (`_analog_reload_ok=False`, permanent until process restart).
- **Atomic artifact writes** (`analog.py:264-282`): FAISS index and its joblib metadata are each written to a `.tmp` path then `os.replace`'d, metadata written last, so a concurrent reader never observes a half-written pair.

### Telegram / CLI Surface
`/mind` (`meme_intelligence/alerts/telegram_commands.py:803-857`) — the "learning-layer report card": analog memory size, resolved/graded coin counts, directional hit-rate, rug precision/recall, Brier score (when available), per-source ensemble accuracy with sample sizes, classifier-ready flag, the P(rug) veto status line (`ON`/`off (MEMEINTEL_LEARNING_VETO_ENABLED)` + whether authority is `EARNED` or `not earned yet` with the precision/sample numbers), and operator 👍/👎 feedback tallies (explicitly labeled advisory-only, never a training label). If the learning service isn't wired in (`enable_in_monitor` off), it reports "Mind layer is off (MEMEINTEL_LEARNING_ENABLE_IN_MONITOR)" plus the feedback tallies alone.

No other learning-layer-specific Telegram commands or CLI subcommands exist in the read files; `resolve_outcome`/`retrain_if_due`/`refresh_archetypes` are driven programmatically (backtest cron and monitor cycle respectively — see Integration Points), not by direct operator command.

### Dormant Features Here
- **P(rug) alert veto** (`MEMEINTEL_LEARNING_VETO_ENABLED`, default `False`): fully built (`_mind_layer_veto`/`_mind_veto_authority` in `workflow/controller.py`, `veto_gate` in `metrics.py`) but OFF by default. Enable with `MEMEINTEL_LEARNING_VETO_ENABLED=true`; even then it stays inert (abstains) until the earned-authority bar is cleared — measured rug precision >= `MEMEINTEL_LEARNING_VETO_MIN_ACCURACY` (default 0.70) over >= `MEMEINTEL_LEARNING_VETO_MIN_SAMPLES` (default 10) graded rug calls. The code comment in `settings.py` states the standing plan is to review the `/mind` report card with the operator before flipping this on.
- **Mind layer participation in the live scan loop** (`MEMEINTEL_LEARNING_ENABLE_IN_MONITOR`, default `False`): without it, `LearningService` is still constructed (`__main__.py:206`) but the controller does not feed live coins into it or call `retrain_if_due` each cycle (`workflow/controller.py:318-324`). A `LearningService` can still be driven purely by the backtest/resolution cron in this state.
- **`min_ensemble_confidence`** (default `0.0`): declared and validated in `LearningSettings` but not referenced anywhere as a gate in the read files (`ensemble.py`, `service.py`) — a configurable floor that currently has no consuming code path, i.e. dormant/unused rather than off-by-default-but-wired.
- **`unsellable_override` seam in `RugEngine.assess`**: designed to accept a live sell-simulation result (Solana Jupiter round-trip / EVM `eth_call`); the engine itself only ever receives `None` or `True` from callers seen in this codebase (`workflow/controller.py:869-872` passes `True` only when a live buy route succeeded and sell route failed, never `False`) — the "active" half of the unsellable check is a plug-in point, not built rug_engine-side logic; whether a full active simulator exists is outside this subsystem's files.
- **Class-balance weighting** (`MEMEINTEL_LIGHTGBM_BALANCED_CLASS_WEIGHTS`, default `True`) is ACTIVE by default, not dormant — flagging for completeness since it's easy to mistake for opt-in.

### Integration Points
**Reads from elsewhere in the codebase:**
- `meme_intelligence.core.models.SecurityProfile` (GoPlus collector's normalized contract facts: honeypot flags, mint/freeze authority, holder concentration, LP lock %, sell tax %, same-creator honeypot count, live buy/sell route flags) — consumed by `RugEngine.assess()`, never re-collected.
- `meme_intelligence.core.models.TokenIdentity` — the chain/address/symbol/name identity type shared with the rest of the pipeline.
- `meme_intelligence.config.settings.Settings` (specifically `.learning`, `.lightgbm`, `.rug_thresholds`, `.rug_signal_weights`) — injected into `LearningService.__init__`.
- `meme_intelligence.core.logging_setup.get_logger` — house logging convention (Rule 13).
- `meme_intelligence.workflow.controller.py` builds `CoinSnapshot`-shaped dicts (`_learning_snapshot`) purely from data the scan pipeline already fetched (pair price/liquidity/market-cap/volume/buys/sells, GoPlus holder concentration, wallet-analyzer creator net outflow) — zero extra API calls to feed the mind layer.
- `meme_intelligence.analytics.backtesting` calls `LearningService.resolve_outcome(...)` when a tracked coin's forward outcome at a horizon becomes measurable — this is the primary "grading" owner process (the cron), distinct from the monitor.

**Writes to / is called from elsewhere:**
- `meme_intelligence/__main__.py:206` — constructs the single `LearningService(settings)` instance for CLI/cron entry points.
- `meme_intelligence/workflow/controller.py` — the monitor process: `_feed_learning()` calls `record_detection` + `capture_snapshot` + `evaluate_coin` on every analyzed coin (when `enable_in_monitor`); `_deterministic_risk_veto()`/`_mind_layer_veto()` calls `evaluate_coin` again for the veto decision and `store.deployer_rug_count()` for the free rug-engine pre-screen; the monitor's cycle loop calls `retrain_if_due()` each pass (via `asyncio.to_thread`, since LightGBM/HDBSCAN/FAISS calls are synchronous/CPU-bound).
- `meme_intelligence/alerts/telegram_commands.py` — `_cmd_mind` calls `get_learning_metrics(persist=False)` for the `/mind` report card; also reads `storage.feedback_summary()` (👍/👎 operator feedback — explicitly advisory, never fed back as a training label into this subsystem).
- The rug engine (`RugEngine`) and its settings (`RugSignalWeights`, `RugThresholds`) are directly re-used (not just referenced) by `workflow/controller.py._deterministic_risk_veto` as a zero-API-cost free screen, independent of whether the full mind layer/veto is enabled.

**Shared on-disk state (`learning_state/` by default, `MEMEINTEL_LEARNING_STATE_DIR`):** `learning.db` (SQLite, WAL), `index.faiss` + `index_meta.joblib` (FAISS analog memory), `scaler.joblib` (StandardScaler), `classifier.txt` (LightGBM booster, native format), `archetypes.joblib` (HDBSCAN-derived centroids/novelty distribution), `ensemble.joblib` (per-source + final rolling accuracy history), `state.joblib` (retrain/scaler-fit counters + `feature_version` marker). This directory is read/written concurrently by (at minimum) the always-on monitor process and the periodic backtest/resolution cron process — see Key Mechanisms for the ownership-guard scheme that makes this safe.

---

## 4. Workflow / Orchestration

### Overview
The Workflow subsystem is the conductor that ties every analyzer/collector/alert module into one operating loop. It has four pieces, all in `meme_intelligence/workflow/`:

1. **`pipeline.py` (`ResearchPipeline`)** — the single, shared per-token analysis chain (security → wallet intel → community/social → on-chain → token structure → momentum → narrative → risk → master score → opportunity rank → optional AI enrichment). Every entry point (continuous scanner, daily routine, `/check`, watchlist review, CLI) calls the same `analyze_pair()` so scoring is identical everywhere (Rule 4/18).
2. **`controller.py` (`ContinuousScanner`)** — the 24/7 fast-layer loop: discovery → per-candidate throttling/dedup → `analyze_pair` → free deterministic risk vetoes (rug engine, mind-layer P(rug), copycat search) → optional paid AI verification gate → persistence/tiering/archiving → alert rule evaluation → cross-source verification → dispatch. Also runs the pump.fun launch funnel, the watchlist recheck cadence, an "insufficient-data" retry mechanism for young pools, and periodic mind-layer retraining.
3. **`daily_routine.py` (`DailyRoutine`)** — the slower, once-a-day desk routine (Part 11): market regime check → discovery/analysis intake → watchlist re-review (delegated to `watchlist_review.py`) → a rendered `DailyReport` journaled to the DB.
4. **`watchlist_review.py` (`review_entries`)** — the one shared "re-check tracked tokens" routine used by both `DailyRoutine` and the `watchlist --refresh` CLI command (the continuous scanner keeps its own inlined variant in `controller.py` because it must also run alert verification per result).

The design goal stated in the code comments is quality over volume: most tokens are meant to end up rejected/ignored, expensive layers (wallet intel, social intel, AI) are gated behind free deterministic screens, and one failing cycle/provider must never take down the whole loop (Rule 7/9).

### Files
| File | Role |
|---|---|
| `meme_intelligence/workflow/controller.py` | `ContinuousScanner` — the 24/7 fast-layer scan-cycle loop: discovery, throttling/dedup, pump.fun launch funnel, watchlist recheck cadence, insufficient-data retry, deterministic risk vetoes (rug engine, mind-layer veto, copycat search), AI verification gate, tiering/archiving (incl. token-death detection), alert evaluation + cross-source verification, dispatch, mind-layer feed, `/status` and `/check` support for Telegram. |
| `meme_intelligence/workflow/pipeline.py` | `ResearchPipeline` — the one shared per-token analysis chain (`analyze_pair`) plus the wallet-intel and social-intel metered credit gates and the AI-enrichment re-scoring path (`enrich_with_ai` / `_enrich_with_ai`). |
| `meme_intelligence/workflow/daily_routine.py` | `DailyRoutine` — the once-daily orchestration: market regime, discovery+analysis intake, watchlist review delegation, `DailyReport` rendering/journaling. |
| `meme_intelligence/workflow/watchlist_review.py` | `review_entries()` + `TIER_FOR_CLASSIFICATION` — the shared "re-score tracked tokens, re-tier or archive" routine used by the daily routine and the CLI `watchlist --refresh` command. |

### Configuration
All values are the literal defaults in `meme_intelligence/config/settings.py`; env override pattern is `MEMEINTEL_<GROUP>_<FIELD>` (`_ENV_PREFIX = "MEMEINTEL"`, loaded via `_load_group(SettingsClass, "GROUP", env)`).

**`WorkflowSettings` — group `WORKFLOW`** (drives the scan-cycle loop and daily routine)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_WORKFLOW_NETWORKS` | `networks` | `"solana"` | comma-separated network ids to scan |
| `MEMEINTEL_WORKFLOW_TOP_CANDIDATES` | `top_candidates` | `5` | discovery candidates deep-analyzed per cycle/run |
| `MEMEINTEL_WORKFLOW_WATCHLIST_REVIEW_LIMIT` | `watchlist_review_limit` | `10` | existing watchlist entries re-checked per pass |
| `MEMEINTEL_WORKFLOW_RISK_ON_BTC_CHANGE_PERCENT` | `risk_on_btc_change_percent` | `2.0` | BTC 24h gain above this = risk-on regime |
| `MEMEINTEL_WORKFLOW_RISK_OFF_BTC_DROP_PERCENT` | `risk_off_btc_drop_percent` | `3.0` | BTC 24h drop beyond this = risk-off regime |
| `MEMEINTEL_WORKFLOW_MONITOR_INTERVAL_SECONDS` | `monitor_interval_seconds` | `45.0` | continuous-scanner cycle cadence (fast layer) |
| `MEMEINTEL_WORKFLOW_WATCHLIST_RECHECK_CYCLES` | `watchlist_recheck_cycles` | `10` | re-check tracked tokens every N cycles (secondary cadence) |
| `MEMEINTEL_WORKFLOW_MAX_TRACKED_KEYS` | `max_tracked_keys` | `50000` | cap on the scanner's in-memory dedupe/verified/copycat FIFO caches |
| `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_ENABLED` | `insufficient_data_retry_enabled` | `True` | give young/data-starved pools a second look instead of a permanent AVOID |
| `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_MIN_COVERAGE` | `insufficient_data_min_coverage` | `0.5` | below this coverage an AVOID is treated as "too early to judge" |
| `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_MINUTES` | `insufficient_data_retry_minutes` | `15.0` | wait time before a retry |
| `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_MAX_AGE_MINUTES` | `insufficient_data_max_age_minutes` | `120.0` | give up retrying once the pool itself is this old |

**`AISettings` — group `AI`** (drives the AI verification gate)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_AI_MODEL` | `model` | `"claude-opus-4-8"` | reasoning-layer model |
| `MEMEINTEL_AI_MAX_TOKENS` | `max_tokens` | `4096` | |
| `MEMEINTEL_AI_EFFORT` | `effort` | `"high"` | low/medium/high/xhigh/max |
| `MEMEINTEL_AI_REQUESTS_PER_MINUTE` | `requests_per_minute` | `10.0` | rate limit |
| `MEMEINTEL_AI_TIMEOUT_SECONDS` | `timeout_seconds` | `120.0` | |
| `MEMEINTEL_AI_MIN_CONFIDENCE` | `min_confidence` | `20.0` | judgment discarded below this confidence |
| `MEMEINTEL_AI_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` (**DORMANT**) | AI judges EVERY analyzed token in the 24/7 loop (expensive) |
| `MEMEINTEL_AI_VERIFY_OPPORTUNITIES` | `verify_opportunities` | `True` (**ACTIVE**) | AI judges ONLY gate-passing HIGH-tier candidates (Part 32.5 §8 middle mode) |
| `MEMEINTEL_AI_VERIFY_SKIP_RUG_SCORE` | `verify_skip_rug_score` | `10.0` | if the free rug-engine score is ≥ this, the paid AI call is skipped entirely (credit conservation) |

**`WalletIntelSettings` — group `WALLET`** (wallet-intel credit gate in `pipeline.py`)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_WALLET_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` (**DORMANT**) | run wallet lookups in the continuous scanner |
| `MEMEINTEL_WALLET_CREDIT_GATE_MIN_SECURITY_SCORE` | `credit_gate_min_security_score` | `50.0` | security-score floor a candidate must clear before spending a wallet lookup |
| `MEMEINTEL_WALLET_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` | `credit_gate_max_lookups_per_day` | `200` | daily gated-lookup budget (0 = unlimited) |
| `MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES` | `credit_gate_cooldown_minutes` | `60.0` | per-token cooldown between gated lookups (0 = off) |

**`SocialIntelSettings` — group `SOCIAL`** (LunarCrush credit gate in `pipeline.py`)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` (**DORMANT** — built 2026-07-20, kept off until a paid LunarCrush key + enable script is run) | LunarCrush calls in the continuous scanner |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_MIN_SECURITY_SCORE` | `credit_gate_min_security_score` | `50.0` | |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` | `credit_gate_max_lookups_per_day` | `200` | |
| `MEMEINTEL_SOCIAL_CREDIT_GATE_COOLDOWN_MINUTES` | `credit_gate_cooldown_minutes` | `60.0` | |

**`LiquidityProbeSettings` — group `LIQUIDITY_PROBE`** (Jupiter round-trip sell test, feeds `pipeline.py` and the rug veto)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_LIQUIDITY_PROBE_ENABLED` | `enabled` | `True` (**ACTIVE**, but only fires when a Jupiter client is wired AND chain is solana/sol) | live buy/sell round-trip probe |
| `MEMEINTEL_LIQUIDITY_PROBE_PROBE_SOL_AMOUNT` | `probe_sol_amount` | `0.3` (~$50) | probe size in SOL |
| `MEMEINTEL_LIQUIDITY_PROBE_SLIPPAGE_BPS` | `slippage_bps` | `500` | 5% tolerance |
| `MEMEINTEL_LIQUIDITY_PROBE_SELL_CONFIRM_FRACTION` | `sell_confirm_fraction` | `0.05` | fallback tiny-sell fraction before declaring non-sellable |

**`LearningSettings` — group `LEARNING`** (mind layer feed + veto; only fields relevant to workflow shown — see full class for the rest)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_LEARNING_ENABLED` | `enabled` | `False` (**DORMANT**) | master opt-in for the mind layer overall |
| `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` (**DORMANT**) | feed the 24/7 scanner's analyzed coins into the mind layer |
| `MEMEINTEL_LEARNING_VETO_ENABLED` | `veto_enabled` | `False` (**DORMANT** — Project 3 / Roadmap #3) | let the mind layer's P(rug) veto a HIGH-tier alert once authority is earned |
| `MEMEINTEL_LEARNING_VETO_MIN_P_RUG` | `veto_min_p_rug` | `0.85` | ensemble P(rug) at/above this vetoes (once armed) |
| `MEMEINTEL_LEARNING_VETO_MIN_ACCURACY` | `veto_min_accuracy` | `0.70` | measured rug-precision floor required to "earn" veto authority |
| `MEMEINTEL_LEARNING_VETO_MIN_SAMPLES` | `veto_min_samples` | `10` | graded rug calls needed before any authority |
| `MEMEINTEL_LEARNING_VETO_METRICS_TTL_SECONDS` | `veto_metrics_ttl_seconds` | `1800.0` | cache TTL for the earned-authority check (`_mind_veto_authority`) |
| `MEMEINTEL_LEARNING_RETRAIN_EVERY_N` | `retrain_every_n` | `200` | warm-start LightGBM retrain cadence (drives `retrain_if_due` called each cycle) |

**`AlertThresholds` — group `ALERTS`** (copycat veto + buy-side gates read in `controller.py`)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_ALERTS_COPYCAT_VETO_ENABLED` | `copycat_veto_enabled` | `True` (**ACTIVE**) | enable the knock-off/duplicate-name screen on HIGH-tier candidates |
| `MEMEINTEL_ALERTS_COPYCAT_LIQUIDITY_RATIO` | `copycat_liquidity_ratio` | `10.0` | an "established" duplicate must hold ≥ this multiple of the candidate's liquidity |
| `MEMEINTEL_ALERTS_COPYCAT_MIN_LIQUIDITY_USD` | `copycat_min_liquidity_usd` | `100000.0` | AND at least this absolute liquidity |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD` | `opportunity_max_liquidity_usd` | `0.0` (OFF) | buy-side ceiling; 0 disables |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_MARKET_CAP_USD` | `opportunity_max_market_cap_usd` | `0.0` (OFF) | buy-side ceiling; 0 disables |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS` | `opportunity_max_age_hours` | `1.0` (**ACTIVE**) | buy-side freshness gate; also used by the wallet/social "worth a lookup" gates in `pipeline.py` |
| `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE` | `momentum_min_security_score` | `0.0` (**DORMANT** — part of the dormant wallet-tracking kit; enable to 50 together with the wallet monitor flag) | security floor for momentum buy-side alerts |

**`AlertEngineSettings` — group `ALERT_ENGINE`** (token-death detection)
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_ALERT_ENGINE_DEAD_LIQUIDITY_USD` | `dead_liquidity_usd` | `500.0` | liquidity below this = token treated as dead (never re-enters the watchlist; existing entries archived) |
| `MEMEINTEL_ALERT_ENGINE_COOLDOWN_SECONDS` | `cooldown_seconds` | `900.0` | same token+alert-type suppression window |
| `MEMEINTEL_ALERT_ENGINE_SCORE_DROP_REVIEW_POINTS` | `score_drop_review_points` | `15.0` | score-drop-vs-last-snapshot trigger |
| `MEMEINTEL_ALERT_ENGINE_RISK_ALERTS_REQUIRE_INTEREST` | `risk_alerts_require_interest` | `True` | demote protective alerts on tokens the operator never showed interest in |

**Related but owned elsewhere (referenced by controller.py's `_layers` status dict and gating logic, cataloged fully by other subsystems):** `RugThresholds`/`RugSignalWeights` (group `RUG_THRESHOLDS`/`RUG_SIGNAL_WEIGHTS`, feed `_deterministic_risk_veto`), `ExecutionSettings` (group `EXECUTION`; `buy_button_enabled=False`, `live_enabled=False` — both dormant/off by default, never auto-trades), `PumpFunSettings` (group `PUMPFUN`, launch monitor tracking window).

### Key Mechanisms
- **Scan-cycle order** (`controller.py:_run_cycle`, line 495): (1) `scan_new_pools` discovery (isolated try/except so a GeckoTerminal outage never aborts the cycle, line 503-509); (2) per-candidate loop over `candidates[:top_candidates]` with dedup via `_seen`/`_retry_pending` bounded FIFO sets (line 514-543); (3) pump.fun launch funnel (`_process_launches`, line 582) if wired; (4) watchlist recheck every `watchlist_recheck_cycles` cycles (line 553-557); (5) insufficient-data retry pass every cycle (line 562-563); (6) mind-layer `retrain_if_due` run off-loop via `asyncio.to_thread` (line 573-578, CPU-bound, must not block the Telegram poller/PumpPortal websocket).
- **Candidate throttling/dedup**: `_BoundedKeySet` (line 158) is an insertion-ordered dict-backed FIFO set capped at `workflow.max_tracked_keys` (default 50000) — used for `_seen`, `_retry_pending`, `_ai_verified`, `_copycat_verdicts`. Prevents unbounded memory growth over weeks on the pump.fun firehose.
- **Insufficient-data retry** (`_finalize_or_reschedule`, line 1111; `_repace_retry`, line 1149; `_retry_insufficient_data`, line 1166): an AVOID verdict with no `overrides` (no confirmed red flag) and `coverage < insufficient_data_min_coverage` on a pool younger than `insufficient_data_max_age_minutes` is rescheduled rather than finalized, paced by `insufficient_data_retry_minutes`, each entry self-pacing so provider outages don't cause every-cycle hammering.
- **`analyze_pair` chain** (`pipeline.py:141`): GoPlus security fetch (returns `None` → candidate dropped, not "seen" — line 528-535 in controller) → optional Jupiter live round-trip probe (merges `live_buy_route_found`/`live_sell_route_found`/`live_round_trip_loss_percent` into the security profile before scoring) → `SecurityAnalyzer.assess` (raises `InsufficientDataError` → `None`) → wallet intelligence (Solana-only, credit-gated) → community (free CoinGecko) → social (LunarCrush, credit-gated) → on-chain/token/momentum/narrative assessments (each independently `InsufficientDataError`-tolerant) → `RiskAnalyzer` → `ScoringEngine.evaluate` (master score) → `OpportunityRanker.rank` → optional AI enrichment if `security.is_destructive` is False and an `ai_service` is wired for per-token judging.
- **Wallet/social credit gates** (`pipeline.py:_gate_allows`/`_social_gate_allows`, mirrored implementations by design, Rule 21): candidate must not be destructive, must clear `credit_gate_min_security_score`, must have finite positive liquidity+mcap, must be under `opportunity_max_liquidity_usd`/`opportunity_max_market_cap_usd` ceilings (if set) and under `opportunity_max_age_hours` (if set) — then a per-token cooldown (`credit_gate_cooldown_minutes`) and a daily budget (`credit_gate_max_lookups_per_day`, rolled at UTC midnight) apply. `force_wallet_check`/`force_social_check` (holdings, `/check`, plan/report) bypass the gate entirely but still stamp the cooldown.
- **Deterministic risk vetoes** (`controller.py:_process_result`, line 636, and `_deterministic_risk_veto`, line 827): run only when `not result.security.is_destructive` AND a provisional buy-side alert type is about to fire (`_BUY_SIDE_ALERT_TYPES`). Zero-API-cost checks, in order: (1) any risk alert already firing (`emergency_review`/`risk_warning`) → veto; (2) `RugEngine.assess` using `rug_signal_weights`/`rug_thresholds`, deployer rug-count from the learning store, observed creator wallet net outflow (`_creator_outflow_usd`), and the Jupiter unsellable override — veto if `rug.score >= ai.verify_skip_rug_score` (default 10.0); (3) `_mind_layer_veto` — the learning layer's P(rug), gated by earned authority (`_mind_veto_authority`, cached `veto_metrics_ttl_seconds`) requiring `veto_min_accuracy`/`veto_min_samples` graded rug calls before it can fire at all; abstains (returns `None`) on any error, cold start, or disabled flag. A veto on a HIGH-tier alert additionally triggers a copycat search (`_copycat_veto`, line 954) — one cached provider search per token symbol/name, comparing against `_find_established_duplicate` (line 90) which requires a same-symbol-or-name match on a pool holding ≥ `copycat_min_liquidity_usd` AND ≥ `copycat_liquidity_ratio`× the candidate's liquidity.
- **AI verification gate** (`controller.py:_process_result`, line 681-711): only runs when `fires_high_tier` (i.e. `high_priority_opportunity`/`strong_candidate` about to fire), the deterministic veto above was clean, `self._ai_verifier` is configured (`ai.verify_opportunities=True`), and the token has not already been AI-verified this process lifetime (`_ai_verified` cache — a persistent gate-passer is judged exactly once). Calls `pipeline.enrich_with_ai`, which re-scores through the same locked `ScoringEngine.evaluate` weighting; `ai_inconclusive` (judgment below `ai.min_confidence` or call failure) is cached so later rechecks still treat the token as unconfirmed rather than re-spending or silently clearing.
- **Alert evaluation and cross-verification**: `AutomationRules.evaluate` produces provisional/final `AlertEvent`s; `_verify_events` (line 1233) cross-checks market-data-based alert types (`_VERIFIABLE_ALERT_TYPES = {high_priority_opportunity, strong_candidate, early_opportunity, momentum}`) against a second market source via `cross_check_liquidity` — disagreement downgrades HIGH→MEDIUM/LOW with a reason, no second source annotates as "unverified" (never silently treated as confirmed). Security-change events bypass this (they rest on contract facts, not market data) but pass an interest gate (`gate_events_by_interest`, `alert_engine.risk_alerts_require_interest`). Operator `/mute` suppresses delivery only, never analysis (fails OPEN on lookup errors, Rule 6).
- **Token-death detection** (`controller.py:_process_result`, line 725-750): `is_dead = liquidity is not None and liquidity < alert_engine.dead_liquidity_usd` (default $500). A dead token gets `tier=None` regardless of its master score — it can never (re-)enter the watchlist. If it (or any token whose score fell to AVOID) was previously tracked/scored, it is archived via `storage.archive()` with reason `"liquidity collapsed to $X: token appears dead"` or `"re-assessment fell to Avoid (score X)"`. The same dead-pool detection recurs in watchlist rechecks (`_recheck_watchlist`, `_retry_insufficient_data`) and in `watchlist_review.review_entries` as `"no active trading pairs remain"` when `get_token_pairs` returns empty (a genuinely empty result, not a provider outage — outages raise `CollectorError`/`AllProvidersFailedError` and are skipped, never archived, per Rule 8).
- **Watchlist tiering** (`watchlist_review.py:TIER_FOR_CLASSIFICATION`, line 22): `ELITE_OPPORTUNITY`/`STRONG_CANDIDATE` → `TIER_1_HIGH_PRIORITY`; `WATCHLIST` → `TIER_2_DEVELOPING`; `SPECULATIVE` → `TIER_3_RESEARCH_ONLY`; `AVOID` → no tier (archived/never added).
- **Watchlist recheck cadence**: fast-layer scanner rechecks tracked tokens every `watchlist_recheck_cycles` cycles (default 10, so ~7.5 min at the default 45s cycle interval), visiting entries **least-recently-updated first** (sorted by `updated_at`) up to `watchlist_review_limit` per pass so the limit rotates through the whole list rather than starving low-ranked entries forever (documented bug-hunt fix). `TIER_3_RESEARCH_ONLY` entries are skipped by the fast recheck and handled only by the daily routine. Uses `get_token_pairs` (raises on outage) rather than `get_best_pair` (which conflates "no pairs" with "all providers down") — archives only on a genuinely empty successful result.
- **Daily routine order** (`daily_routine.py:DailyRoutine.run`, line 205): market check (`assess_market_environment` maps BTC 24h change via `risk_on_btc_change_percent`/`risk_off_btc_drop_percent` to BULL/BEAR/NEUTRAL/UNKNOWN) → `_discover_and_analyze` (discovery + `analyze_pair` + intake into watchlist, journaling AVOID rejections) → `_review_watchlist` (delegates to `watchlist_review.review_entries`, skipping tokens already analyzed this run) → `DailyReport` rendered and journaled (`storage.add_journal(None, "daily_report", ...)`).
- **Reliability backstop** (`controller.py:run`, line 430): any exception escaping `_run_cycle` (including raw, unwrapped errors like `sqlite3.OperationalError`) is caught, logged, and followed by an exponential backoff starting at `_ERROR_BACKOFF_START=5.0`s doubling to `_ERROR_BACKOFF_MAX=300.0`s, reset to the start value on the next healthy cycle. `CancelledError`/`KeyboardInterrupt`/`SystemExit` (BaseException) still propagate for clean shutdown. SIGINT/SIGTERM set a stop flag (`request_stop`) checked between cycles; the current cycle always finishes first.

### Telegram / CLI Surface
- `/status` (Telegram, via `ContinuousScanner.status_snapshot()`, line 382): uptime, cycles run, last-cycle stats (pools/candidates/analyzed/launches/learned/alerts), configured networks, the `_layers` dict (which optional layers are actually active: `wallet_intel`, `social_intel`, `ai`, `learning`, `learning_veto`, `pumpfun`, `jupiter_probe`, `buy_button`, `trading_live`), and DB table counts (degrades to `{}` on failure rather than raising).
- `/check <address>` (Telegram, via `ContinuousScanner.check_token()`, line 412): on-demand single-token analysis through the identical `analyze_pair` pipeline, with `force_wallet_check=True, force_social_check=True` — deliberately bypasses the credit gates since a manual operator lookup is the rare, intentional spend the gate exists to allow, not block.
- `watchlist --refresh` (CLI): calls `watchlist_review.review_entries()` directly — the same shared re-check routine the daily routine uses.
- The daily routine itself is invoked by a scheduled job (outside this subsystem's files, likely cron/systemd on the droplet — not shown in these four files) and its output is journaled via `storage.add_journal(None, "daily_report", report.render())`, retrievable through whatever Telegram report command surfaces the journal (owned by another subsystem).

### Dormant Features Here
- **Wallet intelligence in the monitor loop** — `MEMEINTEL_WALLET_ENABLE_IN_MONITOR=False` by default. Even with a `wallet_service` wired into `ContinuousScanner`, `controller.py` explicitly discards it (sets to `None`, logs an info line) unless this flag is `True`. Enable by setting the env var true AND wiring a `WalletDataService`-compatible client; also consider raising `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE` to 50 in tandem (comment at settings.py:247-251 says the two floors should move together).
- **Social intelligence (LunarCrush) in the monitor loop** — `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR=False` by default, built 2026-07-20. Comment says it is "kept OFF until [the operator] supplies a paid LunarCrush key and runs deploy/enable-x-community-tracking.sh." Same on/off gating pattern as wallet intel.
- **AI judging every analyzed token** — `MEMEINTEL_AI_ENABLE_IN_MONITOR=False` by default (the narrower `verify_opportunities` middle mode, which only judges gate-passing HIGH candidates, is ACTIVE by default at `True`). Enabling `enable_in_monitor` makes the AI service run per-token inside `pipeline.analyze_pair` for every analyzed coin — expensive; flip the env var and ensure `MEMEINTEL_ANTHROPIC_API_KEY` is set.
- **Self-learning "mind layer" feed** — `MEMEINTEL_LEARNING_ENABLED=False` and `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR=False` by default. Even with a `learning_service` wired in, `controller.py` discards it unless `enable_in_monitor` is true. Needed before the mind-layer veto (below) can ever arm, since the veto reads accumulated graded predictions.
- **Mind-layer P(rug) veto on HIGH alerts** — `MEMEINTEL_LEARNING_VETO_ENABLED=False` by default (Project 3 / Roadmap item 3). Docstring at settings.py:1250-1254 says "the standing plan is to read the `/mind` report card with the operator before flipping `veto_enabled`." Even once enabled, it self-gates further: it only actually vetoes once measured rug precision (`veto_min_accuracy`, default 0.70) is proven over at least `veto_min_samples` (10) graded rug calls — otherwise it silently abstains (logged as "authority not earned yet").
- **Momentum security floor** — `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE=0.0` (off) by default. Comment (settings.py:240-251) frames this as "part of the dormant wallet-tracking kit" — the operator asked for it built but not to change behavior until he enables it (set to 50, matching the wallet credit-gate floor).
- **Buy-side liquidity/mcap ceilings** — `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD` and `..._MAX_MARKET_CAP_USD` both default to `0.0` (OFF). The age ceiling (`opportunity_max_age_hours=1.0`) is ACTIVE by default per an explicit operator request, but the size ceilings are not.
- **Trading execution** (`ExecutionSettings`, referenced only via the `_layers["buy_button"]`/`_layers["trading_live"]` status flags in `controller.py`, fully owned by the trading subsystem) — `buy_button_enabled=False` and `live_enabled=False` by default; the system architecture guarantees it never auto-trades regardless.
- **Jupiter liquidity probe** is technically `enabled=True` by default, but functionally dormant unless a `jupiter_client` is wired AND the chain is Solana — treat as "on if configured."

### Integration Points
**Reads from:**
- `scanners/discovery.py` (`DiscoveryEngine`, `scan_new_pools`) and `scanners/launch_monitor.py` (`LaunchMonitor`, `collect_launch_candidates`) — candidate discovery.
- `analyzers/*` (security, onchain, token, momentum, narrative, foundation, community, wallet_intelligence, risk, scoring_engine, opportunity_ranker) — the entire per-token scoring chain, invoked inside `pipeline.analyze_pair`.
- `analyzers/security_monitor.py` (`detect_security_changes`, `extract_facts`, `merge_facts`) — contract-change diffing against the stored baseline each cycle/day.
- `learning/models.py` (`CoinSnapshot`), `learning/rug_engine.py` (`RugEngine`), `learning/metrics.py` (`veto_gate`) — the deterministic and mind-layer risk vetoes.
- `alerts/notification_engine.py` (`AutomationRules`, `NotificationEngine`, `events_from_security_changes`, `gate_events_by_interest`, `INTEREST_ALERT_TYPES`, `_BUY_SIDE_ALERT_TYPES`) — alert rule evaluation and dispatch.
- `database/storage.py` (`Storage`) — score history, snapshots, watchlist get/update/archive, security-facts baseline, journal, mute/holding lookups, alert history, table counts.
- `ai/reasoning.py` (`AIJudgmentService`, `AIJudgment`) — the paid verification/enrichment layer.
- External clients injected by the caller (not owned here): GeckoTerminal (`gecko_client`), GoPlus (`goplus_client`), Jupiter (`jupiter_client`), a market-data service exposing `get_best_pair`/`get_token_pairs`/`cross_check_liquidity`/`search_pairs` (`market_service`), CoinGecko-compatible community client, PumpPortal/PumpFun clients, wallet/social data services, Telegram two-way listener (`set_telegram_listener`).

**Writes to / calls into:**
- `Storage.record_snapshot`, `record_security_facts`, `update_watchlist`, `archive`, `add_journal`, `record_alert` — every analyzed result and decision is persisted for later backtesting/learning (Part 24) and operator visibility.
- `NotificationEngine.dispatch` — final alert delivery to Telegram/other sinks.
- `LearningService.record_detection` / `capture_snapshot` / `evaluate_coin` / `retrain_if_due` — feeds the mind layer's trajectory memory and periodic retraining (when enabled).
- Telegram command listener (`set_telegram_listener`) reads back `status_snapshot()` and calls `check_token()` for `/status` and `/check`.
- The CLI's `watchlist --refresh` command calls `watchlist_review.review_entries()` directly, sharing code with `DailyRoutine._review_watchlist`.

---

## 5. Alerts & Telegram Control

### Overview
This subsystem turns a `PipelineResult` (scanner output) into operator-facing notifications and lets the operator talk back to the bot from a phone. It has three layers, one per file:

1. **`notification_engine.py`** — `AutomationRules.evaluate()` is the decision engine: it reads one pipeline result (plus the previous score, an AI-inconclusive flag, a deterministic rug veto, and an "operator interest" flag) and returns zero or more `AlertEvent`s (opportunity/momentum/risk/etc.), each carrying evidence, a "why it matters" line, a rendered safety checklist, and a priority. `NotificationEngine.dispatch()` ranks those events, applies a per-token/type cooldown, and pushes them through one or more sinks, tracking which ones were actually *delivered* vs merely attempted.
2. **`sinks.py`** — renders an `AlertEvent` into the Part 29 Section 7 message format and delivers it via `ConsoleSink` (always on, local log only), `TelegramSink` (bot `sendMessage`, active once `telegram_bot_token`+`telegram_chat_id` are set), and `DiscordSink` (webhook, active once `discord_webhook_url` is set). Telegram messages carry inline buttons: 👍/👎 feedback, one-tap copy-address, and — only when trading is armed — percentage-of-balance Buy buttons and a Dump button.
3. **`telegram_commands.py`** — `TelegramCommandListener` long-polls Telegram's `getUpdates` (the one and only consumer of that endpoint for the bot token) and answers operator slash-commands (`/status`, `/why`, `/check`, `/holding`, `/watchlist`, `/mind`, `/mute`, `/buy`, `/dump`, etc.), plus routes inline-button callbacks (feedback, buy, dump) with double-tap dedup and a startup backlog-discard so a redeploy never replays a buffered trade command.

The operator never gets auto-traded for: `/buy` and `/dump` (text command or inline button) are the only paths into `trading/execution.py`, and both are refused unless `execution.buy_button_enabled`/`live_enabled` are explicitly turned on (Project 6's hard safety model). Everything else in this subsystem is read-only analysis/notification.

### Files
| File | Role |
|---|---|
| `meme_intelligence/alerts/notification_engine.py` | `AutomationRules` (gate/veto/checklist logic producing `AlertEvent`s from a `PipelineResult`), `AlertEvent` dataclass, `NotificationEngine` (cooldown + ranked dispatch + delivery accounting), `events_from_security_changes`, `rank_alert`, `gate_events_by_interest`, `ConsoleSink` |
| `meme_intelligence/alerts/sinks.py` | `TelegramSink`, `DiscordSink`, `format_alert()` (Section 7 message renderer), inline-keyboard builders (`feedback_keyboard`, `copy_keyboard`, `copy_address_button`), `ALERT_CHANNELS` channel routing table, `parse_routes()` |
| `meme_intelligence/alerts/telegram_commands.py` | `TelegramCommandListener` (long-poll loop, backlog discard, command dispatch table, callback-query handling), `CommandContext` (DI surface into scanner/storage/executor/learning), `classify_address()` address validation |
| `meme_intelligence/config/settings.py` | `AlertThresholds` (group `ALERTS`), `AlertEngineSettings` (group `ALERT_ENGINE`), `AlertDeliverySettings` (group `ALERT_DELIVERY`), `TelegramCommandSettings` (group `TELEGRAM_COMMANDS`), `ExecutionSettings` (group `EXECUTION`), relevant `LearningSettings` veto fields (group `LEARNING`) |
| `meme_intelligence/__main__.py` | `build_sinks()` wires Telegram/Discord sinks from secrets; wires `NotificationEngine`, `TelegramCommandListener`, `CommandContext`, and the trading executor together in the monitor command |
| `meme_intelligence/workflow/controller.py` | Calls `AutomationRules.evaluate()` per pipeline result, computes `deterministic_risk_veto` (free rug-engine/copycat screens) and `operator_interest`, calls `events_from_security_changes()`, applies operator mute before dispatch |

### Configuration
| MEMEINTEL_ env var | field | default | purpose |
|---|---|---|---|
| `MEMEINTEL_ALERTS_SECURITY` | `AlertThresholds.security` | `80.0` | min security score gate for opportunity alerts |
| `MEMEINTEL_ALERTS_COMMUNITY` | `.community` | `70.0` | min community score gate |
| `MEMEINTEL_ALERTS_LIQUIDITY` | `.liquidity` | `70.0` | min liquidity sub-score gate |
| `MEMEINTEL_ALERTS_ONCHAIN` | `.onchain` | `75.0` | min on-chain score gate |
| `MEMEINTEL_ALERTS_OVERALL` | `.overall` | `85.0` | min master score gate (also min bar for the "unverified" MEDIUM tier) |
| `MEMEINTEL_ALERTS_MOMENTUM` | `.momentum` | `70.0` | min momentum score to fire a momentum alert |
| `MEMEINTEL_ALERTS_STRONG_CANDIDATE_OVERALL` | `.strong_candidate_overall` | `88.0` | raised overall bar for the community-unverified "strong_candidate" HIGH tier |
| `MEMEINTEL_ALERTS_STRONG_CANDIDATE_MIN_LIQUIDITY_USD` | `.strong_candidate_min_liquidity_usd` | `25000.0` | depth veto below which a strong candidate downgrades to MEDIUM |
| `MEMEINTEL_ALERTS_STRONG_CANDIDATE_MIN_AI_CONFIDENCE` | `.strong_candidate_min_ai_confidence` | `40.0` | AI-confidence veto floor for the strong-candidate tier |
| `MEMEINTEL_ALERTS_COPYCAT_VETO_ENABLED` | `.copycat_veto_enabled` | `True` | enable the copycat-symbol downgrade before HIGH opportunity alerts |
| `MEMEINTEL_ALERTS_COPYCAT_LIQUIDITY_RATIO` | `.copycat_liquidity_ratio` | `10.0` | how much bigger an established token must be to count as the "real" original |
| `MEMEINTEL_ALERTS_COPYCAT_MIN_LIQUIDITY_USD` | `.copycat_min_liquidity_usd` | `100000.0` | min liquidity for a token to count as "established" for the copycat veto |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MIN_LIQUIDITY_USD` | `.opportunity_min_liquidity_usd` | `0.0` (OFF) | comfort floor; below it the checklist liquidity line reads ⚠ but the alert still sends |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MIN_MARKET_CAP_USD` | `.opportunity_min_market_cap_usd` | `0.0` (OFF) | comfort floor for market cap (checklist annotation only) |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD` | `.opportunity_max_liquidity_usd` | `0.0` (OFF) | ceiling; above it buy-side alerts are suppressed outright (already ran) |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_MARKET_CAP_USD` | `.opportunity_max_market_cap_usd` | `0.0` (OFF) | ceiling for market cap |
| `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS` | `.opportunity_max_age_hours` | `1.0` (ON) | freshness gate; pools older than this suppress buy-side alerts |
| `MEMEINTEL_ALERTS_CHECKLIST_SELL_TAX_MAX_PERCENT` | `.checklist_sell_tax_max_percent` | `15.0` | sell-tax comfort ceiling shown on the safety checklist |
| `MEMEINTEL_ALERTS_CHECKLIST_NEW_LAUNCH_MINUTES` | `.checklist_new_launch_minutes` | `60.0` | pool age under which top-holder concentration is annotated "normal for a new launch" |
| `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE` | `.momentum_min_security_score` | `0.0` (OFF) | security-score floor for momentum alerts; part of a dormant wallet-tracking kit, set to `50` together with the wallet-intel monitor flag to enable |
| `MEMEINTEL_ALERTS_CHECK_DUMPED_DROP_PERCENT` | `.check_dumped_drop_percent` | `80.0` | display-only: `/check` card shows "STATUS: DUMPED" above this 24h price drop |
| `MEMEINTEL_ALERT_ENGINE_COOLDOWN_SECONDS` | `AlertEngineSettings.cooldown_seconds` | `900.0` | per (chain, address, alert_type, priority) suppression window |
| `MEMEINTEL_ALERT_ENGINE_SCORE_DROP_REVIEW_POINTS` | `.score_drop_review_points` | `15.0` | score-fall threshold triggering `score_drop_review` and the decline-suppression gate |
| `MEMEINTEL_ALERT_ENGINE_DEAD_LIQUIDITY_USD` | `.dead_liquidity_usd` | `500.0` | liquidity floor below which a token is ruled dead (one `token_death` post-mortem, archived) |
| `MEMEINTEL_ALERT_ENGINE_RISK_ALERTS_REQUIRE_INTEREST` | `.risk_alerts_require_interest` | `True` | enables the interest gate (demotes protective alerts to LOW on tokens the operator was never pointed at) |
| `MEMEINTEL_ALERT_DELIVERY_TELEGRAM_ROUTES` | `AlertDeliverySettings.telegram_routes` | `""` | `category=chat_id[,...]` per-channel Telegram routing |
| `MEMEINTEL_ALERT_DELIVERY_DISCORD_ROUTES` | `.discord_routes` | `""` | `category=webhook_url[,...]` per-channel Discord routing |
| `MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY` | `.external_min_priority` | `"medium"` | min priority (critical/high/medium/low) Telegram/Discord will deliver; console always shows all |
| `MEMEINTEL_ALERT_DELIVERY_REQUESTS_PER_MINUTE` | `.requests_per_minute` | `20.0` | rate limit applied to each external sink |
| `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED` | `TelegramCommandSettings.enabled` | `False` (OFF) | master switch for the two-way command listener (DORMANT until set) |
| `MEMEINTEL_TELEGRAM_COMMANDS_POLL_TIMEOUT_SECONDS` | `.poll_timeout_seconds` | `25.0` | Telegram long-poll wait, must be in [1,50] |
| `MEMEINTEL_TELEGRAM_COMMANDS_IDLE_DELAY_SECONDS` | `.idle_delay_seconds` | `2.0` | pause between successful polls |
| `MEMEINTEL_TELEGRAM_COMMANDS_ERROR_BACKOFF_MAX_SECONDS` | `.error_backoff_max_seconds` | `60.0` | cap on poll-error exponential backoff (starts at 2s) |
| `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED` | `ExecutionSettings.buy_button_enabled` | `False` (OFF) | shows Buy/Dump buttons on alerts and unlocks `/buy`/`/dump` commands |
| `MEMEINTEL_EXECUTION_LIVE_ENABLED` | `.live_enabled` | `False` (OFF) | actually sign+send trades; otherwise every buy/dump is dry-run |
| `MEMEINTEL_EXECUTION_MAX_BUY_SOL` | `.max_buy_sol` | `0.15` | per-trade SOL ceiling; `0` = no ceiling (wallet balance is the only limit) |
| `MEMEINTEL_EXECUTION_SLIPPAGE_BPS` | `.slippage_bps` | `500` | base slippage for trade quotes |
| `MEMEINTEL_EXECUTION_PRIORITY_FEE_MAX_LAMPORTS` | `.priority_fee_max_lamports` | `1000000` | cap on priority fee per trade |
| `MEMEINTEL_EXECUTION_CONFIRM_TIMEOUT_SECONDS` | `.confirm_timeout_seconds` | `45.0` | on-chain confirmation wait |
| `MEMEINTEL_EXECUTION_PREFLIGHT_RETRIES` | `.preflight_retries` | `2` | re-quote retries when preflight simulation rejects a trade |
| `MEMEINTEL_EXECUTION_BUY_BUTTON_PERCENTS` | `.buy_button_percents` | `"20,50,75,100"` | percentages of live wallet balance shown as one-tap buy buttons |
| `MEMEINTEL_LEARNING_VETO_ENABLED` | `LearningSettings.veto_enabled` | `False` (OFF) | mind-layer P(rug) veto on HIGH opportunities; shown as "off"/"ON" in `/mind` |
| `MEMEINTEL_LEARNING_VETO_MIN_P_RUG` | `.veto_min_p_rug` | `0.85` | ensemble P(rug) threshold to veto once authority is earned |
| `MEMEINTEL_LEARNING_VETO_MIN_ACCURACY` | `.veto_min_accuracy` | `0.70` | measured rug precision floor to "earn" veto authority |
| `MEMEINTEL_LEARNING_VETO_MIN_SAMPLES` | `.veto_min_samples` | `10` | graded rug calls needed before any veto authority |
| `MEMEINTEL_TELEGRAM_BOT_TOKEN` / `MEMEINTEL_TELEGRAM_CHAT_ID` | `Settings.telegram_bot_token` / `.telegram_chat_id` | `""` | secrets; presence alone activates `TelegramSink` and is required (with `TELEGRAM_COMMANDS_ENABLED=true`) for the command listener |
| `MEMEINTEL_DISCORD_WEBHOOK_URL` | `Settings.discord_webhook_url` | `""` | secret; presence alone activates `DiscordSink` |

### Key Mechanisms
- **Alert types produced** (`notification_engine.py`): `token_death` (MEDIUM, one-time post-mortem, `_token_death_rule` L530), `emergency_review` (CRITICAL, `_emergency_rule` L560-576), `risk_warning` (HIGH, same method, L577-587), `high_priority_opportunity` (HIGH, every gate verified, `_opportunity_rule` L617-635), `strong_candidate` (HIGH, community-unverified fresh launch, L648-678), `early_opportunity` (MEDIUM, provisional/vetoed opportunity, L636-646 and L680-692), `momentum` (MEDIUM, `_momentum_rule` L732-765), `smart_money_accumulation` (MEDIUM, `_smart_money_rules` L775-792), `whale_exit` (HIGH, L794-812), `insider_risk` (HIGH, L814-824), `community_fake` (HIGH, `_community_rule` L830-845), `score_drop_review` (HIGH, `_score_drop_rule` L847-863), `security_change` (CRITICAL/HIGH/MEDIUM per severity, `events_from_security_changes` L879-917, emitted from `workflow/controller.py` off contract-fact diffs, not from `AutomationRules`).
- **Token-death short-circuit** (L244-251): if liquidity < `alert_engine.dead_liquidity_usd` (NaN/None excluded, Rule 8), the ONLY events emitted are the death post-mortem plus any still-firing CRITICAL emergency; every buy-side/momentum/drop signal on the corpse is dropped.
- **Suppression gates that drop a buy-side alert entirely** (L318-321, applied to `_BUY_SIDE_ALERT_TYPES` = high_priority_opportunity/strong_candidate/early_opportunity/momentum/smart_money_accumulation):
  1. `deterministic_risk_veto is not None` — the rug engine's combined verdict / risk alert / honeypot / earned mind-layer P(rug), computed upstream in `workflow/controller.py::_deterministic_risk_veto`.
  2. `_untradeable()` (L341-355) — liquidity or market cap is `None`/NaN/`<=0`; unknown never counts as tradeable.
  3. `_oversized()` (L357-375) — liquidity or market cap above the operator ceiling (`opportunity_max_liquidity_usd`/`opportunity_max_market_cap_usd`), both default `0.0`=OFF; unknown never trips it.
  4. `_too_old()` (L377-395) — pool age above `opportunity_max_age_hours` (default `1.0`, ON); unknown/bad timestamp never trips it.
- **Decline-suppression** (L322-324, `_score_declining` L865-876): if score fell ≥ `score_drop_review_points` since last look, `_DECLINE_SUPPRESSED_TYPES` (early_opportunity/momentum/smart_money_accumulation) are dropped, but `strong_candidate`/`high_priority_opportunity` are exempt (rare enough to show the contradiction), and `score_drop_review` (protective) always fires regardless.
- **Safety checklist** (`_safety_checklist` L397-473): only rendered when none of the 4 hard vetoes above fired; 7 lines — sellable, mint authority, freeze authority, sell tax vs `checklist_sell_tax_max_percent`, deployer honeypot history, liquidity vs `opportunity_min_liquidity_usd`, market cap vs `opportunity_min_market_cap_usd` (shown only if a floor is set) — plus an informational (never-fail) top-holder-concentration note. Status icons: ✅ pass, ⚠️ warn (annotates, never suppresses), ℹ️ note, ❔ unknown (Rule 8). Header shows "passed X/Y" counting only pass/warn lines.
- **Interest gate** (`gate_events_by_interest` L134-158, controlled by `risk_alerts_require_interest`, default True): when the operator has no interest in a token (no prior HIGH `high_priority_opportunity`/`strong_candidate`, and none firing in this same batch), `_PROTECTIVE_ALERT_TYPES` (emergency_review, risk_warning, score_drop_review, token_death, whale_exit, insider_risk, community_fake, security_change) are demoted to LOW — still logged/recorded, but below the external sinks' `external_min_priority` floor, so the phone stays quiet.
- **Cooldown + delivery accounting** (`NotificationEngine.dispatch` L984-1064): cooldown key is `(chain, address.lower(), alert_type, priority)` — priority is part of the key so a lower-priority alert's cooldown can never eat a later higher-priority one. Events are ranked by `rank_alert()` (impact 40% + confidence 30% + urgency 20% + novelty 10%, weights fixed) before sending, most decision-relevant first. A sink's `send()` returns `True` (delivered), `False` (failed — logged, not raised), or `None` (filtered by min-priority / legacy sink, treated as success). `has_external = any sink has .external=True`; if any external sink exists, only external sinks' outcomes count toward `any_delivered` — the always-succeeding `ConsoleSink` (`external=False`) can no longer make a lost phone alert look delivered. Cooldown is stamped ONLY when `any_delivered`; otherwise the event is dropped from the returned `delivered` list so it retries once the cooldown window elapses. Exceptions from any single sink are caught per-sink and never abort the batch. The `_last_sent` map is pruned once it exceeds 256 entries.
- **Message rendering** (`sinks.py::format_alert`, L155-187): fixed Section 7 layout — header (`_HEADER_FOR_PRIORITY`), alert type, Token block (name/symbol sanitized via `_sanitize_identity`, contract address exact, chain), time detected, event summary, why it matters, evidence (reasons), checklist, current scores, risk assessment (`_RISK_FOR_PRIORITY`), recommended monitoring. Telegram truncates to 4000 chars (hard limit 4096); Discord wraps in a code fence, truncates to 1900, and disables `@everyone`/`@here` parsing.
- **Channel routing** (`ALERT_CHANNELS`, L40-56): discoveries (opportunity tiers, new_token_discovery), smart_money (accumulation, whale_exit), security (emergency/risk/security_change/token_death/insider_risk/community_fake), momentum, reports (score_drop_review, default fallback for unknown types), boosts (kept isolated from discoveries so paid DexScreener boosts never drown out vetted alerts).
- **Inline buttons** (`feedback_keyboard`, L128-152): 👍/👎 (`fb:1:<addr>` / `fb:0:<addr>`), one-tap copy-address (Telegram `copy_text`, 256-char cap), and — only when `buy_percents` non-empty (i.e. `execution.buy_button_enabled`) — a row of `Buy N%` buttons (`buy:<addr>:pct:<pct>`, resolved against LIVE wallet balance at tap time, not baked in) plus a `💥 Dump all` button (`dump:<addr>`). All `callback_data` capped at Telegram's 64-byte limit — an address that doesn't fit simply gets no buttons, never truncated (would misroute).
- **Command listener safety** (`telegram_commands.py`): single `getUpdates` consumer per bot; `_discard_backlog()` drains and discards ALL buffered updates on every startup before live polling begins (retried until it provably succeeds) so a redeploy can never replay a buffered `/buy`/`/dump`; only `message` (not `edited_message`) is routed to command dispatch (editing a `/buy` message must not fire a second trade); `_claim_button()` dedups a (message_id, callback_data) pair so a double-tap of the same inline button fires at most one trade; unauthorized chat ids are logged (id only, never text) and silently ignored; all replies are plain text (no `parse_mode`), previews disabled, bot token redacted from logs.
- **Address validation** (`classify_address`, L72-82): strict allow-list — Solana base58 32-44 chars, or EVM `0x`+40 hex; anything else rejected before any use (D3).
- **/check protections** (Rule 11): a single `asyncio.Lock` serializes concurrent `/check` runs (a second concurrent call is told to retry rather than queued); a 60-second, 64-entry result cache prevents button-mashing from re-running the full multi-provider pipeline.
- **/check lifecycle overlay** (`_lifecycle_line`, L612-647; display-only, never read by the alert pipeline): "RUGGED" when liquidity < `alert_engine.dead_liquidity_usd` AND a destructive finding or failed live-sell probe confirms it; "DEAD" when only the liquidity floor is crossed (no confirmed cause, honest per Rule 8); "DUMPED" when liquidity is still intact but 24h price change ≤ `-alerts.check_dumped_drop_percent`.

### Telegram / CLI Surface
All commands require `TELEGRAM_COMMANDS_ENABLED=true` plus the bot secrets; only messages from the configured `chat_id` are processed.

- `/help`, `/start` — prints the fixed command list (`_HELP_TEXT`).
- `/status` — scanner uptime, cycle count, last-cycle stats (pools seen/candidates/analyzed/launches tracked/learned/alerts), networks scanned, per-layer ON/off state (wallet intel, AI, learning, mind veto, pump.fun, jupiter probe, trading off/dry-run/LIVE), and DB counts (tokens/alerts/watchlist/holdings). Degrades to an empty snapshot if the status provider throws.
- `/why <address>` — latest ≤3 alerts with reasons, latest score snapshot + classification, watchlist tier if any, holding/muted flags, and red flags on record (honeypot, cannot-sell, cannot-buy, mint/freeze authority live, or a failed Jupiter live-sell probe). Returns a copy-address button. Replies "never analyzed" if the token has no history.
- `/check <address> [chain]` — runs the full pipeline right now (single-flight lock, 60s cache) and returns a phone card: price/liquidity/market cap, security score+band+tier, RUGGED/DEAD/DUMPED lifecycle line when applicable, red-flag findings (never suppressed), overall score+classification+coverage, momentum score+entry zone (with a "stale" note if the coin is dead/dumped), plus an advisory mind-layer p(rug)/confidence/n line when the learning service is wired.
- `/holding <address>` — marks a token as held (protective alerts stay full-priority regardless of the interest gate); shows active-holdings count.
- `/unhold <address>` — releases a holding.
- `/holdings` (alias `/positions`) — lists active holdings with symbol/chain/acquired date/last score.
- `/watchlist` — top 8 tracked tokens by tier with score.
- `/boost <address> [chain]` — DexScreener paid-boost amount for a coin (advisory framing: "paid promotion... rugs buy boosts too"); reports "unavailable in this build" if no boost-lookup wired.
- `/mind` — mind-layer report card: memory size, resolved/graded counts, hit rate, rug precision/recall, brier score, per-source ensemble accuracy, classifier-ready flag, P(rug) veto state+earned-authority line, and operator 👍/👎 feedback tallies (always advisory).
- `/mute <address>` — suppresses ALL alert delivery for a token (analysis continues).
- `/unmute <address>` — restores alert delivery.
- `/buy <address> <sol_amount>` — buys that many SOL of a token; refused unless `execution.buy_button_enabled` is on and an executor is wired; dry-run unless `execution.live_enabled` is also on.
- `/dump` (alias `/sell`) `<address>` — sells the full position back to SOL; same guard as `/buy`.
- Inline buttons on alerts: 👍/👎 feedback (advisory, recorded, never a training label), 📋 copy-address, `Buy N%` (percent of live wallet balance, resolved at tap time) and `💥 Dump all` — both trading buttons only rendered when `execution.buy_button_enabled` is on; both apply the same guard and a double-tap dedup as the text commands.

### Dormant Features Here
- **Two-way Telegram command listener** — entirely off by default (`MEMEINTEL_TELEGRAM_COMMANDS_ENABLED=False`). Enable by setting that env var to `true` AND having `MEMEINTEL_TELEGRAM_BOT_TOKEN`/`MEMEINTEL_TELEGRAM_CHAT_ID` set (same secrets the alert sink uses); without both secrets present the monitor prints a note and skips starting the listener even with the flag on.
- **Trading buttons / execution** — off by default on two independent switches: `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=False` (no Buy/Dump buttons shown, `/buy`/`/dump` refused with "Trading buttons are off") and `MEMEINTEL_EXECUTION_LIVE_ENABLED=False` (even with buttons on, every trade routes to the dry-run executor which signs nothing). A trading private key (`MEMEINTEL_EXECUTION_PRIVATE_KEY`) is also required for a real executor to build at all — its absence or an invalid key silently falls back to dry-run (never crashes the scanner).
- **Mind-layer P(rug) veto** — off by default (`MEMEINTEL_LEARNING_VETO_ENABLED=False`), and even when turned on it self-gates on measured authority: it only actually vetoes once rug precision ≥ `veto_min_accuracy` (0.70) over ≥ `veto_min_samples` (10) graded rug calls, checked live in `/mind`'s "p(rug) veto: ... | authority: ..." line.
- **Momentum security floor (`momentum_min_security_score`)** — defaults to `0.0` (off). Comment in `settings.py` (2026-07-20) says it is deliberately part of a larger dormant "wallet-tracking kit" the operator asked built but not yet turned on; enabling it means setting this to `50` together with the wallet-intel monitor flag so the two floors stay aligned.
- **Opportunity size ceilings (`opportunity_max_liquidity_usd`, `opportunity_max_market_cap_usd`)** — both default `0.0` = OFF; set either to a positive USD value to suppress buy-side alerts on coins that already grew past that size.
- **Opportunity comfort floors (`opportunity_min_liquidity_usd`, `opportunity_min_market_cap_usd`)** — default `0.0` = OFF (annotate-only checklist lines are omitted/informational until a floor is set).
- **Copycat veto** — `copycat_veto_enabled` defaults `True` (ACTIVE, not dormant) but only fires on the rare HIGH-tier candidates since it costs a real market-search API call.
- Everything else described above (interest gate, freshness gate at 1h, dead-liquidity floor, cooldown, decline suppression, safety checklist) is ACTIVE by default.

### Integration Points
**Reads from elsewhere:**
- `workflow.pipeline.PipelineResult` — the full per-token analysis (pair, security, security_profile, onchain, community, momentum, wallet, master, ai_judgment) that `AutomationRules.evaluate()` consumes.
- `analyzers.risk_analyzer.emergency_flags()` — supplies the critical/high destructive-risk reason lists behind `emergency_review`/`risk_warning`.
- `workflow/controller.py` is the sole caller of `AutomationRules.evaluate()`: it computes `previous_score` from `storage.score_history()`, `deterministic_risk_veto` from a zero-cost rug-engine/copycat screen (`_deterministic_risk_veto`/`_copycat_veto`), `ai_verification_inconclusive` from a per-token cache of AI-verification outcomes, and `operator_interest` from `_operator_interest(token)` (has this token ever earned a HIGH opportunity alert). It also calls `events_from_security_changes()` off `analyzers.security_monitor` contract-fact diffs, then applies `storage.is_muted(token)` (fail-open) before handing events to `NotificationEngine.dispatch()`.
- `meme_intelligence/__main__.py::build_sinks()` builds `[ConsoleSink(), TelegramSink?, DiscordSink?]` from `Settings` secrets and wires `NotificationEngine(sinks, settings.alert_engine)`; it also builds `TelegramCommandListener` with a `CommandContext` closing over `scanner.status_snapshot`, `scanner.check_token`, the learning service, the trade executor, and `dex.get_token_boost`.
- `collectors.base.BaseCollector` — both sinks and the command listener inherit rate limiting, retries, timeouts, and secret redaction from it (Rules 6/7/11/16).
- `trading.execution.TradeIntent` / `DryRunExecutor` / live executor — `/buy`, `/dump`, and their inline-button equivalents are the only entry points that construct a `TradeIntent` and call `execute_buy`/`execute_sell_all`.
- `learning.metrics.veto_gate` — used by `/mind`'s `_veto_status_line` to report earned P(rug) veto authority.
- `core.models.TokenIdentity`, `core.enums.AlertPriority`/`AccumulationVerdict`/`EntryZone` — shared value types.

**Writes to elsewhere:**
- `storage.record_feedback()` (👍/👎, advisory only, linked to the most recent alert within 7 days if any).
- `storage.set_holding()` / `release_holding()` / `mute_token()` / `unmute_token()` — operator state mutated only via `/holding`, `/unhold`, `/mute`, `/unmute`.
- Alert history / score snapshots are recorded upstream in `workflow/controller.py` (not in this subsystem), and `/why` and `/check` read them back via `storage.alert_history()` / `storage.score_history()` / `storage.latest_security_facts()`.
- Delivered `AlertEvent`s stamp `NotificationEngine._last_sent` (in-process cooldown state only, not persisted to DB).

---

## 6. Trading & Execution

### Overview
This subsystem covers two related but distinct things that share the Jupiter integration:

1. **Operator-initiated buy/dump execution** (`meme_intelligence/trading/execution.py` + `meme_intelligence/trading/solana_rpc.py`) — "Project 6". The bot NEVER auto-trades. A buy or a "dump 100%" is only ever a button/command the human operator taps in Telegram (`/buy`, `/dump`, or inline callback buttons `buy:<addr>:<sol>`, `buy:<addr>:pct:<pct>`, `dump:<addr>`, wired in `meme_intelligence/alerts/telegram_commands.py`). Behind one interface (`execute_buy(TradeIntent)` / `execute_sell_all(mint, chain)` / `get_spendable_balance_sol()`) there are two executors: `DryRunExecutor` (signs nothing, journals the intent, always used unless live trading is explicitly armed) and `LiveExecutor` (builds a Jupiter swap, signs it with a dedicated low-balance Solana keypair loaded from an env var, submits via a minimal Solana JSON-RPC client, and polls for on-chain confirmation). Buys and full-position dumps only — no partial sells, no limit orders, no automation of any kind.

2. **Trade planning** (`meme_intelligence/trading/trade_planner.py`) — a separate, purely advisory engine (Spec Part 8/Part 9) that turns the analyzers' outputs (security/community/on-chain/token assessments) into a `TradePlan`: a 0-100 trade score, setup classification, conviction level, a *guidance* max-position-percent (never enforced, never wired to the executor), a pre-entry checklist, required confirmations for unknown fields, invalidation conditions, and fixed FOMO-prevention questions / profit-discipline reminders. It is display/journal-only text (`TradePlan.render()`) and has no code path into `execution.py` — it never sizes or triggers a real trade itself.

Both consume analysis assessments (`SecurityAssessment`, `OnChainAssessment`, `CommunityAssessment`, `TokenAssessment`) but the planner is read-only research output while the executor is the only place that moves real SOL. A third piece threads through both: the **Jupiter live round-trip sell-test probe** (`meme_intelligence/collectors/jupiter_data.py::JupiterClient.check_round_trip_liquidity`), which is a *read-only* quote-only diagnostic run automatically during normal scanning (independent of live trading being armed) and whose result (`live_sell_route_found` / `live_round_trip_loss_percent`) feeds the security analyzer as a destructive-honeypot signal; `LiveExecutor` separately calls the same `JupiterClient.get_quote`/`build_swap_transaction` methods, but for real (uncached) trade-time quotes rather than the probe.

### Files
| File | Role |
|---|---|
| `meme_intelligence/trading/execution.py` | `TradeIntent`, `TradeError`, `DryRunExecutor`, `LiveExecutor` — the operator-tapped buy/dump execution path; owns the safety gates (per-trade cap, live balance re-check, slippage, retry-with-fresh-quote, staged broadcast-safe error reporting). |
| `meme_intelligence/trading/solana_rpc.py` | `SolanaRpcClient` (thin JSON-RPC client over Helius), `TransactionRejectedError` — `getBalance`, `getTokenAccountsByOwner`, `sendTransaction`, `getSignatureStatuses`; distinguishes preflight-rejected (nothing spent, safe to retry) from ambiguous submission errors (never auto-retried). |
| `meme_intelligence/trading/trade_planner.py` | `TradePlanner`, `TradePlan`, `ChecklistItem`, `TradeJournalEntry` — advisory research-plan generator (score, setup, conviction, guidance sizing, checklist, confirmations, invalidations, FOMO questions). No execution capability. |
| `meme_intelligence/collectors/jupiter_data.py` | `JupiterClient` — Jupiter Swap API wrapper: `get_quote` / `build_swap_transaction` (used by `LiveExecutor`) and `check_round_trip_liquidity` (the read-only rug-check probe, used by the scanning pipeline, not by trading). Defines `SOL_MINT`. |
| `meme_intelligence/__main__.py` (`build_jupiter`, `build_executor`) | Wiring: constructs the shared `JupiterClient`; picks `LiveExecutor` vs `DryRunExecutor` based on `settings.execution.live_enabled` + presence of `trading_private_key` + Jupiter/Helius keys; prints `"LIVE TRADING ARMED"` or falls back to dry-run on any construction failure. |
| `meme_intelligence/alerts/telegram_commands.py` | `/buy`, `/dump` commands and inline callback handlers (`_handle_buy`, `_handle_dump`, `_do_buy`, `_do_dump`, `_trading_guard`); percent-of-balance buy buttons; `/status` "trading: off/dry-run/LIVE" line; `/why` surfaces `live_sell_route_found is False` as a red flag. |
| `meme_intelligence/analyzers/security_analyzer.py` (contract sub-score) | Consumes `live_buy_route_found`/`live_sell_route_found`/`live_round_trip_loss_percent` from the probe as a destructive-honeypot override, independent of GoPlus static analysis (Rule 9). |
| `meme_intelligence/analyzers/security_monitor.py` | Fires a CRITICAL alert if a token's `live_sell_route_found` flips from true to false between checks (sell route disappearing after the fact). |
| `meme_intelligence/core/models.py` | `LiquidityProbeResult` dataclass (probe output shape) and its fields on `SecurityProfile`. |

### Configuration
All defaults pulled verbatim from `meme_intelligence/config/settings.py`. Env vars follow `MEMEINTEL_<GROUP>_<FIELD_UPPER>` via `_load_group()`.

**`ExecutionSettings`, group `EXECUTION`** (operator buy/dump — Project 6):

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED` | `buy_button_enabled` | `False` | Show Buy/Dump buttons on Telegram alerts; also gates `/buy` and `/dump` text commands via `_trading_guard()`. |
| `MEMEINTEL_EXECUTION_LIVE_ENABLED` | `live_enabled` | `False` | Actually sign+send trades; off (or no private key) forces `DryRunExecutor`. |
| `MEMEINTEL_EXECUTION_MAX_BUY_SOL` | `max_buy_sol` | `0.15` | Per-trade SOL ceiling; **0 = no ceiling** (operator request, 2026-07-20) — with 0, the live wallet-balance re-check is the only automatic limit. |
| `MEMEINTEL_EXECUTION_SLIPPAGE_BPS` | `slippage_bps` | `500` (5%) | Base slippage bound for quotes/swaps; must be in (0, 10000]. |
| `MEMEINTEL_EXECUTION_PRIORITY_FEE_MAX_LAMPORTS` | `priority_fee_max_lamports` | `1_000_000` (0.001 SOL) | Cap on the Jupiter dynamic priority fee per trade. |
| `MEMEINTEL_EXECUTION_CONFIRM_TIMEOUT_SECONDS` | `confirm_timeout_seconds` | `45.0` | How long `LiveExecutor._confirm` polls `getSignatureStatuses` before reporting "pending". |
| `MEMEINTEL_EXECUTION_PREFLIGHT_RETRIES` | `preflight_retries` | `2` | Extra attempts (fresh quote each time) after a preflight-rejected (never-broadcast) transaction; 0 = never retry; range [0,10]. Ambiguous submission errors are never auto-retried regardless of this setting. |
| `MEMEINTEL_EXECUTION_BUY_BUTTON_PERCENTS` | `buy_button_percents` | `"20,50,75,100"` | Comma list of percent-of-spendable-balance buy buttons; each value must be in (0,100]. |

**Secrets (root `Settings`, not a `_load_group` block — read directly by field name):**

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_EXECUTION_PRIVATE_KEY` | `trading_private_key` | `""` | Base58 Solana private key for the DEDICATED trading wallet; never the operator's main wallet; never logged. Required (with `live_enabled`) for `LiveExecutor`. |
| `MEMEINTEL_EXECUTION_HELIUS_API_KEY` | `trading_helius_api_key` | `""` | Optional separate Helius key for the trading RPC client, so it draws its own rate-limit budget instead of sharing the scanner's Helius bucket. Falls back to `MEMEINTEL_HELIUS_API_KEY` (root `helius_api_key`) if unset. |
| `MEMEINTEL_JUPITER_API_KEY` | `jupiter_api_key` | `""` | Jupiter Developer Platform key (`x-api-key` header), required for `JupiterClient` to exist at all — required for both the live executor and the read-only sell-route probe. |

**`LiquidityProbeSettings`, group `LIQUIDITY_PROBE`** (the rug-check probe, runs independent of trading):

| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_LIQUIDITY_PROBE_ENABLED` | `enabled` | `True` | Turns the automatic live round-trip sell-test on/off during normal scanning. |
| `MEMEINTEL_LIQUIDITY_PROBE_PROBE_SOL_AMOUNT` | `probe_sol_amount` | `0.3` | Probe buy size in SOL (~$50 at time of writing); must be re-tuned in `.env` as SOL price moves — not USD-denominated to avoid a live price dependency. |
| `MEMEINTEL_LIQUIDITY_PROBE_SLIPPAGE_BPS` | `slippage_bps` | `500` (5%) | Slippage tolerance for the probe quotes. |
| `MEMEINTEL_LIQUIDITY_PROBE_SELL_CONFIRM_FRACTION` | `sell_confirm_fraction` | `0.05` | Fraction of the bought amount re-probed if the full-size sell finds no route, to distinguish "pool too thin" from a true honeypot. |

**`TradingSettings`, group `TRADING`** (trade-planner discipline; guidance only, never enforced by the executor):

| Env var | Field | Default |
|---|---|---|
| `MEMEINTEL_TRADING_HIGH_CONVICTION_MIN_SCORE` | `high_conviction_min_score` | `80.0` |
| `MEMEINTEL_TRADING_MEDIUM_CONVICTION_MIN_SCORE` | `medium_conviction_min_score` | `65.0` |
| `MEMEINTEL_TRADING_HIGH_CONVICTION_MIN_SECURITY` | `high_conviction_min_security` | `75.0` |
| `MEMEINTEL_TRADING_MIN_CONFIRMATION_COVERAGE` | `min_confirmation_coverage` | `0.5` |
| `MEMEINTEL_TRADING_HIGH_CONVICTION_MAX_POSITION_PERCENT` | `high_conviction_max_position_percent` | `5.0` |
| `MEMEINTEL_TRADING_MEDIUM_CONVICTION_MAX_POSITION_PERCENT` | `medium_conviction_max_position_percent` | `2.0` |
| `MEMEINTEL_TRADING_SPECULATIVE_MAX_POSITION_PERCENT` | `speculative_max_position_percent` | `0.5` |

**`TradeScoreWeights`, group `TRADE_WEIGHTS`** (must sum to 1.0): `setup_quality` 0.20, `security` 0.20, `community` 0.15, `onchain` 0.15, `market_conditions` 0.15, `risk_reward` 0.15 (env vars `MEMEINTEL_TRADE_WEIGHTS_<FIELD>`).

**Relevant `ProviderSettings` (group `PROVIDERS`)** consumed by the Jupiter/Helius clients used here: `jupiter_base_url` (`https://api.jup.ag`, `MEMEINTEL_PROVIDERS_JUPITER_BASE_URL`), `jupiter_requests_per_minute` (`50.0`, `MEMEINTEL_PROVIDERS_JUPITER_REQUESTS_PER_MINUTE` — Jupiter free-tier documented limit is 60/min = 1 rps), `helius_rpc_url` (`https://mainnet.helius-rpc.com`, `MEMEINTEL_PROVIDERS_HELIUS_RPC_URL`), `helius_requests_per_minute` (`120.0`, `MEMEINTEL_PROVIDERS_HELIUS_REQUESTS_PER_MINUTE`).

### Key Mechanisms
- **Executor selection** (`__main__.py:build_executor`, lines ~209-271): live trading requires ALL of `execution.live_enabled=True` AND `trading_private_key` set AND a Jupiter client (i.e. `jupiter_api_key` set) AND a resolvable Helius RPC key (`trading_helius_api_key` or fallback to root `helius_api_key`). Any single missing piece, or an exception constructing `LiveExecutor` (bad base58 key, `solders` import failure), silently downgrades to `DryRunExecutor` and prints a `"Note: ..."` line to stdout rather than crashing the 24/7 loop (Rule 7).
- **Money gate ordering in `execute_buy`** (`execution.py:170-203`): (1) chain must be `solana`/`sol`; (2) `math.isfinite(sol_amount) and sol_amount > 0` — explicit NaN/Inf rejection, called out as a 2026-07-11 bug-hunt finding since `nan <= 0` and `nan > cap` are both `False`; (3) per-trade cap check only if `max_buy_sol > 0` (0 = disabled, 2026-07-20 change); (4) under an `asyncio.Lock` (one trade at a time per wallet), a **live** `getBalance` re-check against `lamports + _FEE_BUFFER_LAMPORTS` (7,000,000 lamports ≈ 0.007 SOL fee/rent buffer) — this is the only limit left when `max_buy_sol=0`.
- **`_FEE_BUFFER_LAMPORTS = 7_000_000`** — hardcoded headroom reserved on every buy and subtracted in `get_spendable_balance_sol()` so a "100% of balance" percentage-buy button computes an amount that will actually clear the balance re-check.
- **Quote/swap/retry loop** (`_quote_and_swap`, lines 225-267): fetches a *fresh, uncached* quote (`use_cache=False`) each attempt, up to `preflight_retries + 1` attempts. Only `TransactionRejectedError` (preflight simulation definitively refused, nothing broadcast) triggers a retry with a brand-new quote at the current price — this is the mechanism that survives fast-moving meme-coin price action (2026-07-19 operator-reported finding). Any other `CollectorError` from the quote call aborts immediately with "nothing was spent."
- **Broadcast-safety staging in `_execute_swap`** (lines 269-355): the transaction's signature is derived deterministically from the signed bytes (`_signature_of`) *before* sending, so even if `send_raw_transaction` is cancelled mid-flight (shutdown), the signature is journaled and logged at ERROR before re-raising `CancelledError` — the operator is told to verify on Solscan and explicitly told NOT to re-tap. Once a signature is returned, it is the source of truth: pre-broadcast failures say "nothing was spent"; post-broadcast outcomes are always framed around checking the signature, never blind retry.
- **Confirmation polling `_confirm`** (lines 384-399): polls `getSignatureStatuses` every 2s until `confirm_timeout_seconds` elapses; `status["err"] is not None` raises `TradeError` (confirmed on-chain revert — "only the network fee was spent, safe to retry"); timeout returns `False` ("submitted — confirmation still pending. Do NOT retry").
- **Rejection classification** (`solana_rpc.py:_describe_send_rejection`): RPC code `-32002` ("Transaction simulation failed") = preflight-rejected, never broadcast → `TransactionRejectedError`, safe to retry with a fresh quote. Program error code `0x1771` (Jupiter's slippage-exceeded error) gets a specific human message. All other errors are ambiguous `CollectorError`s — never auto-retried.
- **Secret redaction**: `SolanaRpcClient` passes the Helius API key to `BaseCollector(redact=(api_key,))` so it never appears in logs; `LiveExecutor._safe()` replaces the wallet's own pubkey with a truncated form (`pubkey[:4] + "…"`) before any error text reaches the operator.
- **Live round-trip sell-route probe** (`jupiter_data.py:check_round_trip_liquidity`, lines 155-220): buy-quote SOL→mint at `probe_sol_amount`; if no buy route, return inconclusive (not destructive). If buy route exists, quote a full-size sell mint→SOL; if that finds no route, re-probe with `sell_confirm_fraction` (5%) of the bought amount before concluding non-sellable — this avoids condemning thin-but-legitimate pools as honeypots. Result feeds `SecurityAssessment` via `security_analyzer.py` contract sub-score: `live_sell_route_found is False` (after a buy route was found) → `flag_destructive` (honeypot-equivalent verdict, independent of GoPlus static analysis per Rule 9); `live_round_trip_loss_percent` above `extreme_round_trip_loss_percent` → destructive, above `max_round_trip_loss_percent` → SERIOUS_WARNING deduction of 25 points (both thresholds live in `SecurityThresholds`, not read in this pass). `security_monitor.py` additionally fires a CRITICAL alert if `live_sell_route_found` flips true→false between two checks on the same token.
- **Trade planner conviction gating** (`trade_planner.py:_conviction`): `security.is_destructive` forces `NO_TRADE` outright, overriding score. Evidence `coverage < min_confirmation_coverage` caps conviction at `SPECULATIVE` (discovery ≠ confirmation). `MarketRegime.BEAR` downgrades one level via `_DOWNGRADE` map. `WATCH_ONLY` setups get `max_position_percent=None` unconditionally, regardless of conviction's guidance value.

### Telegram / CLI Surface
- `/buy <address> <sol>` — text-command buy, routes to `_do_buy` → `TradeIntent` → `executor.execute_buy`.
- `/dump <address>` — text-command full-position sell, routes to `_do_dump` → `executor.execute_sell_all`.
- Inline callback `buy:<address>:<sol>` — legacy fixed-SOL buy button (kept working on already-delivered alerts per Rule 3/18).
- Inline callback `buy:<address>:pct:<percent>` — current percent-of-live-balance buy button; resolves against `executor.get_spendable_balance_sol()` at tap time (never at alert-render time).
- Inline callback `dump:<address>` — dump/sell-all button.
- Both callback handlers are idempotent per `dedup_id` (`_claim_button`) to reject a double-tap of the same button, and both send the full result as a chat message while returning only a short "Done — see chat for the result" popup ack (a popup can't safely assert success/failure without contradicting the real outcome).
- `_trading_guard()` — common precondition check before either buy or dump: refuses with "Trading buttons are off (MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED)" if `buy_button_enabled` is false, or "Trading is not wired up in this process" if no executor was constructed.
- `/status` — reports a `trading:` line as `off` / `dry-run` / `LIVE` based on `layers["buy_button"]` and `layers["trading_live"]`, plus a `jupiter probe` on/off flag for the read-only rug-check probe.
- `/why <address>` — surfaces `live_sell_route_found is False` as an explicit red flag line ("live sell route NOT found (Jupiter probe)").
- CLI: none of these three files register a separate CLI subcommand; wiring happens through `__main__.py:build_executor`/`build_jupiter` called by the main scan/serve entrypoint, and all operator interaction is via the Telegram commands/buttons above.

### Dormant Features Here
- **Live trading itself is OFF by default and double-gated**: `MEMEINTEL_EXECUTION_LIVE_ENABLED=false` (default) forces `DryRunExecutor` regardless of any key configuration. Even with it `true`, `LiveExecutor` only activates if `MEMEINTEL_EXECUTION_PRIVATE_KEY` is also set (a dedicated, low-balance wallet's base58 key) AND `MEMEINTEL_JUPITER_API_KEY` is set AND a Helius RPC key is resolvable (`MEMEINTEL_EXECUTION_HELIUS_API_KEY` or fallback `MEMEINTEL_HELIUS_API_KEY`). To enable: set all three env vars, restart the process; `__main__.py` prints `"LIVE TRADING ARMED — trading wallet <pubkey>. Per-trade cap <cap>."` to confirm.
- **Buy/Dump Telegram buttons are OFF by default**: `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=false`. This is independent of `live_enabled` — buttons can be shown while trading stays dry-run (buttons then just produce dry-run journal replies), or trading can be armed live while buttons stay hidden (only `/buy`/`/dump` text commands would work, gated by the same `_trading_guard`).
- **Per-trade buy cap is effectively dormant/no-op at its current default in the "no ceiling" sense only if explicitly set to 0** — the shipped default `MEMEINTEL_EXECUTION_MAX_BUY_SOL=0.15` IS active and enforced; setting it to `0` (documented, operator-requested 2026-07-20 change) disables the ceiling entirely and leaves the live wallet-balance check as the only automatic limit. This is a currently-active low default, not a dormant feature — flagging because the 0-disables-it semantics could surprise an operator raising the cap.
- **The live round-trip sell-route probe (`LiquidityProbeSettings.enabled`) is ON by default** (`True`) and is NOT part of the trading path — it always runs during normal scanning as long as `MEMEINTEL_JUPITER_API_KEY` is set (same `JupiterClient` instance is shared read-only-probe + live-executor use), independently of `live_enabled`/`buy_button_enabled`. If no Jupiter API key is configured at all, `build_jupiter()` returns `None` and both the probe and live trading go dark simultaneously — this is the one shared single point of failure between the two.
- **Legacy fixed-SOL buy-button callback format** (`buy:<address>:<sol>`) is dormant in the sense that new alerts never render it (superseded by `buy:<address>:pct:<percent>` on 2026-07-18), but the handler still accepts it for old already-delivered Telegram messages — not user-enablable, just backward-compatibility code that will naturally stop being exercised as old alerts scroll out of chat history.
- **Trade planner's position-size guidance is purely advisory and never wired to enforce anything** on the executor — there is no dormant "auto-size the buy from the trade plan" feature; the two systems are fully decoupled by design (confirmed by absence of any planner import in `execution.py`/`__main__.py`'s executor-building code).

### Integration Points
**Reads from:**
- `meme_intelligence/config/settings.py` — `ExecutionSettings`, `LiquidityProbeSettings`, `TradingSettings`, `TradeScoreWeights`, `ProviderSettings` (Jupiter/Helius URLs & rate limits), and root `Settings.trading_private_key` / `trading_helius_api_key` / `jupiter_api_key` / `helius_api_key`.
- `meme_intelligence/collectors/jupiter_data.py::JupiterClient` — `get_quote`, `build_swap_transaction` (live executor); `check_round_trip_liquidity` (probe, consumed elsewhere, not by the executor).
- `meme_intelligence/collectors/base.py::BaseCollector` — both `JupiterClient` and `SolanaRpcClient` inherit rate limiting, retries, timeout handling, and secret redaction from this shared base (Rules 6/7/11/16).
- `meme_intelligence/core/errors.py` — `CollectorError`, `MemeIntelError` base classes for all trading errors.
- `meme_intelligence/core/models.py` — `TokenIdentity` (journal keys), `LiquidityProbeResult` (probe output type, not directly touched by execution.py but same data family).
- `meme_intelligence/analyzers/{security_analyzer,onchain_analyzer,community_analyzer,token_analyzer}.py` — `TradePlanner.build_plan` consumes `SecurityAssessment`, `OnChainAssessment`, `CommunityAssessment`, `TokenAssessment`.
- `meme_intelligence/scanners/discovery.py::TokenCandidate` — feeds `setup_quality` score component in the trade planner.
- `meme_intelligence/core/enums.py` — `CheckStatus`, `ConvictionLevel`, `MarketCapStage`, `MarketPhase`, `MarketRegime`, `RiskTier`, `SetupType` used throughout the planner.
- `solders` package (lazy-imported inside `LiveExecutor.__init__`/`_sign`/`_signature_of`) — Keypair and VersionedTransaction handling; its absence or an ABI mismatch is caught and downgrades to dry-run rather than crashing.

**Writes to / calls into:**
- `storage.add_journal(TokenIdentity, kind, content)` — both executors journal every buy/dump intent and outcome (dry-run intents tagged `"trade_intent"`; live trades tagged `"trade_buy"`/`"trade_sell"` with the tx signature or Solscan-verification note). Journaling failures are caught and logged, never allowed to break the operator-facing reply.
- Solana mainnet via the operator's Helius RPC endpoint — `sendTransaction`, `getBalance`, `getTokenAccountsByOwner`, `getSignatureStatuses`.
- Jupiter Swap API (`api.jup.ag`) — `swap/v1/quote`, `swap/v1/swap`.
- `meme_intelligence/alerts/telegram_commands.py` — the only caller of `execute_buy`/`execute_sell_all`/`get_spendable_balance_sol` in the whole codebase (confirmed by grep); this is the sole trading entry surface, consistent with the "operator-tapped, never automatic" invariant.
- `meme_intelligence/analyzers/security_analyzer.py` + `security_monitor.py` — consume the probe's `LiquidityProbeResult` fields (via `SecurityProfile`) as an independent (Rule 9) destructive-honeypot signal and a regression alert, entirely separate from anything in `execution.py`.
- `meme_intelligence/workflow/pipeline.py` and `workflow/controller.py` — populate `live_sell_route_found` onto the security profile from the probe result and gate the `jupiter_probe` status layer on `jupiter_client is not None and settings.liquidity_probe.enabled`.

---

## 7. Database, Analytics & AI Reasoning

### Overview
This subsystem is the system's memory and its self-grading loop, plus the one place an LLM (Claude) is allowed to touch the pipeline.

**Storage** (`meme_intelligence/database/storage.py`) is a single synchronous SQLite wrapper (WAL mode, 30s busy timeout) that every other subsystem writes through: every assessment the scoring engine produces is persisted as a `snapshots` row (a "prediction record"), every delivered alert as an `alerts` row, every operator Telegram action (`/holding`, `/mute`, thumbs up/down) as its own table. Nothing here computes a score — it only remembers what was computed and, later, what actually happened to the price/liquidity.

**Backtesting** (`meme_intelligence/analytics/backtesting.py`, Spec Part 24) closes the loop: it walks every first snapshot per token (`storage.predictions()`), waits for fixed time windows (1h/24h/7d/30d) to elapse, then measures price change and liquidity survival against later snapshots or a live fetch. It grades each prediction as correct/incorrect/undetermined against the classification the framework gave it, rolls that into accuracy/opportunity-detection/false-positive/risk-detection metrics (with regime and confidence-calibration breakdowns), tests four alternate category-weight variants for report-only discrimination power, mines which category scores were "confidently wrong" on failures, and finally labels each historical alert `useful`/`noise`/`correct_warning`. It never rewrites the shipped scoring weights itself (Part 31 lock) — it only reports what a human could change and provides `record_strategy_change()` to journal that change.

**AI layer** (`meme_intelligence/ai/`) is the only place Claude is invoked. It is deliberately kept out of the deterministic scoring math: the pipeline's category scores, security tier, decision-tree classification are all computed before the AI ever sees the token, and the AI's structured judgment is only allowed to fill two *specific, otherwise-empty* judgment slots (Foundation and Narrative inputs) that the deterministic engines cannot compute from market data alone (dev communication quality, meme strength, cultural timing, etc.) plus a bull/bear case and a confidence score. If the AI is unavailable, refuses, times out, or returns a judgment scored below a confidence floor, the pipeline silently continues on deterministic evidence alone — this is Rule 9 degrade-gracefully applied to the one non-deterministic component in the system. A banned-language guard (`ai/prompts.py`) additionally screens all AI (and deterministic) report prose for hype/guarantee phrasing before it can reach the operator.

### Files
| File | Role |
|---|---|
| `meme_intelligence/database/storage.py` | Single SQLite persistence layer: schema, migrations, and every read/write method (tokens, snapshots, watchlist, journal, security_facts, wallet_sightings, alerts, outcomes, holdings, muted_tokens, operator_feedback) |
| `meme_intelligence/analytics/backtesting.py` | Outcome measurement (`refresh_outcomes`), prediction grading (`evaluate_predictions`), performance metrics, signal/weight-variant/failure-pattern analysis, alert-outcome labeling, strategy-change journaling, and the plain-text backtest report renderer |
| `meme_intelligence/ai/prompts.py` | `ANALYST_SYSTEM_PROMPT` (the Claude system prompt / analyst identity + rules) and `check_language()` (banned hype/guarantee-phrase guard with negation handling) |
| `meme_intelligence/ai/reasoning.py` | `AIJudgmentService`: builds the condensed intelligence snapshot, calls the Anthropic API with a JSON-schema-constrained request, validates/parses the response into an `AIJudgment`, enforces the confidence floor |
| `meme_intelligence/ai/report_generator.py` | Deterministic (non-AI) final report renderer — assembles every engine's output plus evidence-derived bull/bear bullets into the canonical text report; this is what the AI's bull/bear prose supplements, never replaces |
| `meme_intelligence/ai/comparison.py` | Multi-token comparison table + explicit ranking (Avoid-with-overrides always sinks below non-overridden tokens regardless of score) |
| `meme_intelligence/workflow/pipeline.py` (integration point, not assigned but read for context) | `ResearchPipeline.enrich_with_ai()` / `_enrich_with_ai()` — where an `AIJudgment` is folded into Foundation/Narrative assessments and the master score is recomputed through the same locked weighting |
| `meme_intelligence/workflow/controller.py` (integration point) | Wires `enable_in_monitor` vs `verify_opportunities` AI modes into the scan loop; runs the gate-passing AI verification call before a high-priority alert fires |
| `meme_intelligence/__main__.py` (integration point) | `backtest` CLI subcommand (`--refresh`) that drives the whole backtesting module end to end |

### Configuration
| Env var | Field | Default | Purpose |
|---|---|---|---|
| `MEMEINTEL_DATABASE_PATH` | `DatabaseSettings.path` | `"data/meme_intelligence.sqlite3"` | SQLite file location; WAL mode, `busy_timeout=30000`ms so the 24/7 monitor and cron jobs can share it |
| `MEMEINTEL_BACKTEST_WINDOWS_HOURS` | `BacktestSettings.windows_hours` | `"1,24,168,720"` (1h/24h/7d/30d) | Comma-separated outcome-measurement windows |
| `MEMEINTEL_BACKTEST_WINDOW_TOLERANCE_FRACTION` | `window_tolerance_fraction` | `0.35` | A stored snapshot within ±35% of a window's target time counts as that window's measurement |
| `MEMEINTEL_BACKTEST_SUCCESS_PRICE_CHANGE_PERCENT` | `success_price_change_percent` | `50.0` | Best-window gain that counts a positive call as "succeeded" |
| `MEMEINTEL_BACKTEST_FAILURE_PRICE_CHANGE_PERCENT` | `failure_price_change_percent` | `-50.0` | Worst-window loss (or liquidity death) that counts as "failed" |
| `MEMEINTEL_BACKTEST_SURVIVAL_MIN_LIQUIDITY_USD` | `survival_min_liquidity_usd` | `1000.0` | Liquidity floor below which a token is graded dead (`survived=False`) |
| `MEMEINTEL_BACKTEST_SIGNAL_HIGH_SCORE` | `signal_high_score` | `70.0` | "High" bucket threshold for per-category signal-performance analysis |
| `MEMEINTEL_BACKTEST_SIGNAL_LOW_SCORE` | `signal_low_score` | `50.0` | "Low" bucket threshold (must stay below `signal_high_score`) |
| `MEMEINTEL_BACKTEST_ALERT_USEFUL_DRIFT_POINTS` | `alert_useful_drift_points` | `10.0` | Master-score drift magnitude that labels an alert `useful`/`correct_warning` vs `noise` |
| `MEMEINTEL_BACKTEST_ALERT_OUTCOME_MIN_HOURS` | `alert_outcome_min_hours` | `24.0` | Alerts younger than this stay unlabeled (label is permanent once set) |
| `MEMEINTEL_BACKTEST_MIN_PREDICTIONS_FOR_WEIGHTS` | `min_predictions_for_weights` | `10` | Minimum graded predictions before weight-variant experiments run at all |
| `MEMEINTEL_ANTHROPIC_API_KEY` | `Settings.anthropic_api_key` | `""` (unset) | Anthropic API key; AI layer is fully dormant (`build_judgment_service` returns `None`) without it |
| `MEMEINTEL_AI_MODEL` | `AISettings.model` | `"claude-opus-4-8"` | Model used for judgment calls |
| `MEMEINTEL_AI_MAX_TOKENS` | `max_tokens` | `4096` | Response token cap |
| `MEMEINTEL_AI_EFFORT` | `effort` | `"high"` | Reasoning effort (`low`\|`medium`\|`high`\|`xhigh`\|`max`) |
| `MEMEINTEL_AI_REQUESTS_PER_MINUTE` | `requests_per_minute` | `10.0` | Rate limiter (Rule 11) |
| `MEMEINTEL_AI_TIMEOUT_SECONDS` | `timeout_seconds` | `120.0` | Per-call timeout |
| `MEMEINTEL_AI_MIN_CONFIDENCE` | `min_confidence` | `20.0` | Judgments below this confidence are discarded entirely (Part 23 S6) |
| `MEMEINTEL_AI_ENABLE_IN_MONITOR` | `enable_in_monitor` | `False` (**DORMANT**) | Judges EVERY analyzed token in the continuous scanner — expensive |
| `MEMEINTEL_AI_VERIFY_OPPORTUNITIES` | `verify_opportunities` | `True` (**ACTIVE** default, but only fires when `MEMEINTEL_ANTHROPIC_API_KEY` is set and an AI service is wired in) | Middle mode: one AI call only on tokens that already passed every deterministic gate (high-priority opportunity), right before the alert dispatches |
| `MEMEINTEL_AI_VERIFY_SKIP_RUG_SCORE` | `verify_skip_rug_score` | `10.0` | If the deterministic rug engine already scores at/above this, the paid AI verification call is skipped entirely and the alert is downgraded instead (credit conservation — smallest rug signal weight is 10, so any fired signal blocks the spend by default) |

### Key Mechanisms
- **Snapshot-as-prediction**: every `record_snapshot()` call (`storage.py:344`) stores `final_score`, `classification`, `confidence`, `coverage`, per-category `category_scores` (JSON), `overrides` (JSON), plus market facts (`price_usd`, `liquidity_usd`, `market_cap`, `regime`, `opportunity_rank`) at prediction time — these market facts (added via the `_MIGRATIONS` dict, `storage.py:189-195`) are what make later outcome grading possible.
- **First-snapshot-is-the-prediction rule**: `storage.predictions()` (`storage.py:834`) selects only the row with `MIN(id)` per token — later re-assessments are not new predictions, so a token isn't graded twice for one call.
- **Outcome measurement precedence** (`backtesting.py:_nearest_snapshot`, line 149): prefers a later *stored* snapshot within `window_hours * window_tolerance_fraction` of the target time (free, already collected) over a live fetch (`_live_measurement`, line 165); a pair that has vanished entirely from DexScreener/GeckoTerminal is scored `price=0.0, liquidity=0.0` — token death IS the outcome, not a null.
- **Grading logic** (`evaluate_predictions`, line 198): positive classes (`elite_opportunity`, `strong_candidate`) succeed when best-window change ≥ `success_price_change_percent`; fail on liquidity death or worst-window change ≤ `failure_price_change_percent`; `avoid` calls grade inverted; `watchlist`/`speculative` are always `ungraded` (assert neither outcome). Anything with measurements that hit neither threshold is `undetermined`, never forced into correct/incorrect (Rule 8).
- **Weight experiments are report-only** (`weight_experiments`, line 348): re-scores every usable prediction under 4 fixed alternate `ScoringWeights` variants (`security_heavy`, `community_heavy`, `narrative_heavy` vs `locked_baseline`), splits into top/bottom half by that variant's weighted score, and reports the discrimination (top-half avg change − bottom-half avg change). Requires ≥`min_predictions_for_weights` (10) usable rows or returns `None`. The Part 31 Framework Consistency Lock means nothing here writes back to the live weights — a human must apply via `MEMEINTEL_WEIGHTS_*` env vars and record the change via `record_strategy_change()`.
- **Alert outcome labeling maturity gate** (`label_alert_outcomes`, line 413): compares master-score drift from alert time to the latest later snapshot; opportunity-type alerts (`high_priority_opportunity`, `strong_candidate`, `early_opportunity`, `momentum`, `smart_money_accumulation`) want positive drift ≥ `alert_useful_drift_points` to be `useful`; risk-type alerts want drift ≤ `-alert_useful_drift_points` to be `correct_warning`; alerts younger than `alert_outcome_min_hours` are skipped because the label is **permanent** once written (a documented bug-hunt fix: `strong_candidate` was previously missing from the opportunity set, so that tier was graded by the inverted risk rule).
- **AI never sees raw data**: `build_intelligence_snapshot()` (`reasoning.py:143`) condenses a `PipelineResult` into a fixed dict (token identity, market numbers, security score/flags/findings, community section with an explicit "not tracked" note for Twitter/Discord/bot-detection, wallet/onchain findings, momentum) before it ever reaches the prompt — no raw provider payloads.
- **Structured output, validated twice**: the Anthropic call is constrained by `JUDGMENT_SCHEMA` (JSON schema, all 12 numeric judgment slots + 3 risk-flag booleans nullable) at the API level (`output_config.format`), then `AIJudgmentService._parse()` (`reasoning.py:343`) re-validates ranges (0-100, explicit `bool`-is-not-`int` guard), rejects non-string/non-list types, and finally runs `check_language()` over all prose (bull/bear/summary/confidence_reason) — a banned-phrase hit anywhere discards the *entire* judgment.
- **Confidence floor**: `judge()` (`reasoning.py:287`) discards the judgment if `confidence < min_confidence` (default 20) — logged, not raised.
- **Two independent AI modes, never conflated** (`controller.py:285-300`): `enable_in_monitor` = per-token judgment inside the continuous scanner (dormant by default); `verify_opportunities` = one judgment only on tokens that already passed every deterministic alert gate, run as the *last* screen before a high-priority/strong-candidate alert dispatches (`controller.py:682-711`). A documented bug-hunt fix removed an `or` that let `verify_opportunities=false` be silently overridden by `enable_in_monitor=true`.
- **Credit conservation gate before verification**: if the deterministic rug engine score ≥ `verify_skip_rug_score` (default 10 — the smallest individual rug-signal weight), the paid AI call is skipped outright and the alert is downgraded on rug evidence alone (`controller.py:878`).
- **AI judgment folds into — never overrides — deterministic scoring** (`pipeline.py:_enrich_with_ai`, line 510): the AI's `foundation_inputs`/`narrative_inputs` are only used to build Foundation/Narrative assessments if evidence actually exists (all-null judgment slots are explicitly detected and skipped — two documented bug-hunt fixes prevent an all-null judgment from fabricating a score via fallback defaults); the resulting Foundation/Narrative scores are then fed back through the *same locked* `ScoringEngine.evaluate()` weighting used everywhere else, and `enrich_with_ai` is idempotent (a result that already carries `ai_judgment` is returned unchanged).
- **AI failure is invisible to the pipeline, but not to the alert gate**: an API error, refusal, invalid JSON, or sub-floor confidence all return `None` from `judge()`, and `enrich_with_ai` then returns the original deterministic `result` unchanged (Rule 9). In the verification path specifically, controller.py tracks `ai_inconclusive` separately so `AutomationRules` can treat "AI looked and found nothing usable" differently from "AI never ran" (Rule 8).
- **Banned-language guard** (`prompts.py:check_language`, line 146): matches 15 hype/guarantee phrases case-insensitively with word boundaries; a same-clause negation window (split on `.!?\n;`, last 4 words) whitelists phrases like "not guaranteed"; a lookahead exception whitelists "no risk ASSESSMENT/DATA/..." as a cautionary disclosure rather than a hype claim. Applied to both AI judgment prose and (implicitly, by design intent) deterministic report text.
- **Comparison ranking** (`comparison.py:rank_results`, line 27): sorts by `(classification is not AVOID, overrides-empty, final_score, coverage)` descending — an Avoid classification always sinks below every non-Avoid token regardless of raw score, fixing a prior bug where a decision-tree-rejected token with an empty `overrides` tuple could rank first on a high number alone.

### Telegram / CLI Surface
- CLI: `python -m meme_intelligence backtest [--refresh]` (`__main__.py:670`, registered at line 1159/1232) — runs `refresh_outcomes` (with `--refresh`, fetches live prices for windows with no stored snapshot near the target; without it, only measures from snapshots already collected), then `label_alert_outcomes`, `evaluate_predictions`, `performance_metrics`, `signal_performance`, `weight_experiments`, `failure_success_patterns`, and prints `render_backtest_report()`. Scheduled 6-hourly via cron on the droplet (`handoff/OPERATIONS.md`: "daily routine + 6-hourly `backtest --refresh` + DB backup").
- No dedicated Telegram command for backtesting is defined in these files; `storage.table_counts()`, `alert_history()`, `feedback_summary()`, `get_holdings()`, `muted_list()` back the `/status`, `/why`, `/mind`, `/holding`/`/unhold`, `/mute`/`/unmute` Telegram commands (implemented elsewhere in `telegram_commands`/alert delivery code, not in these files) by reading from the tables this subsystem owns.
- The AI reasoning layer has no direct Telegram/CLI entrypoint of its own — it is invoked only from inside the pipeline (`enrich_with_ai`) during the scan loop or gate-passing verification.

### Dormant Features Here
- **`MEMEINTEL_AI_ENABLE_IN_MONITOR`** (default `False`): per-token AI judgment for every analyzed coin in the continuous scanner. DORMANT by design — comment in `AISettings` explicitly calls this "AI judges EVERY analyzed token (expensive)" and Rule 10 (expensive analysis only after filtering) keeps it off by default. To enable: set `MEMEINTEL_AI_ENABLE_IN_MONITOR=true` (and `MEMEINTEL_ANTHROPIC_API_KEY` must be set, plus an `ai_service` must actually be wired into `WorkflowController` by the entrypoint code — not shown in these 6 files).
- **The whole AI layer** is dormant until `MEMEINTEL_ANTHROPIC_API_KEY` is set — `build_judgment_service()` (`reasoning.py:454`) returns `None` without it, and every other AI setting is then moot. `verify_opportunities` defaults to `True` but is inert without the key.
- **Weight-variant experiments** (`weight_experiments`) are effectively dormant until ≥10 graded predictions with a measured best-window change exist (`min_predictions_for_weights`, default 10) — on a fresh install `render_backtest_report` prints "sample too small — experiments run once enough predictions have outcomes." This is not a config flag to flip but a natural data-volume gate.
- **Weight adoption itself is manual, never automatic**: even once `weight_experiments` shows a discriminating variant, nothing in this code applies it — the report text says explicitly "to adopt a change: set `MEMEINTEL_WEIGHTS_*` env overrides and record it (strategy journal, Section 11)." `record_strategy_change()` exists purely to journal that human decision.
- **Backtest live-price refresh** (`--refresh` flag) is off unless explicitly passed on the CLI invocation; without it, `refresh_outcomes` only measures windows a stored snapshot already happens to cover, never reaching out to DexScreener/GeckoTerminal.
- **Learning-service integration in backtesting** (`learning_service` param to `refresh_outcomes`) is a no-op unless the caller passes one; `__main__.py`'s `backtest` command only builds it when `settings.learning.enabled` — the mind/learning subsystem itself is a separately-gated, off-by-default feature (not covered by these files but referenced here as duck-typed and best-effort).

### Integration Points
**Reads from / is fed by:**
- `analyzers.scoring_engine.MasterAssessment` — `Storage.record_snapshot()` persists the master score/classification/confidence/coverage/category scores/overrides every time the pipeline finishes an assessment.
- `alerts.notification_engine.AlertEvent` (duck-typed, avoids an alerts→database→alerts import cycle) — `Storage.record_alert()` persists every delivered alert.
- `workflow.pipeline.PipelineResult` — `ai.reasoning.build_intelligence_snapshot()` condenses this into the AI's input; `ai.report_generator.build_report()` and `ai.comparison.render_comparison()` render it into operator-facing text.
- `config.settings.AISettings` / `BacktestSettings` / `DatabaseSettings` — all thresholds and toggles described above.
- `core.rate_limiter.RateLimiter` — throttles AI API calls to `requests_per_minute`.
- `analyzers.foundation_analyzer.FoundationInputs` / `analyzers.narrative_analyzer.NarrativeInputs` — the exact dataclasses an `AIJudgment` produces, fed back into `FoundationAnalyzer.assess()` / `NarrativeAnalyzer.assess()`.

**Writes to / is called by:**
- `workflow.pipeline.ResearchPipeline._enrich_with_ai()` / `enrich_with_ai()` — the sole caller of `AIJudgmentService.judge()`; recomputes `MasterAssessment` via `ScoringEngine.evaluate()` after folding in AI-derived Foundation/Narrative scores.
- `workflow.controller.WorkflowController` — gates both AI modes (`enable_in_monitor`, `verify_opportunities`) behind settings flags regardless of what the entrypoint wires in; runs the gate-passing AI verification immediately before a high-priority/strong-candidate alert would fire, and can downgrade/suppress that alert based on the outcome; calls `storage.record_snapshot()` after every assessment and (elsewhere) `storage.record_alert()` when alerts dispatch.
- `analytics.backtesting.refresh_outcomes()` — reads `storage.predictions()`/`snapshots_for_token()`/`outcomes_for_snapshot()`, writes `storage.record_outcome()`, and optionally calls `learning_service.resolve_outcome()` (mind layer, best-effort, exceptions swallowed).
- `analytics.backtesting.label_alert_outcomes()` — reads `storage.alerts_with_drift()`, writes `storage.set_alert_outcome()`.
- `__main__.py`'s `backtest` CLI command — the only orchestrator that runs the full backtesting pipeline end to end (also used by cron per `handoff/OPERATIONS.md`).
- Telegram command handlers (not in these files) — read `storage.table_counts()`, `alert_history()`, `feedback_summary()`, `get_holdings()`, `muted_list()`, `journal_entries()`, `score_history()`, `top_opportunities()` to answer `/status`, `/why`, `/mind`, `/holding`, `/mute`, watchlist, and history-style commands, and write `set_holding()`, `mute_token()`, `record_feedback()` on operator actions.
- `handoff/DECISIONS_LOG.md` / journal — `record_strategy_change()` is the intended write path whenever a human applies a weight-experiment finding via env override.

---

## 8. Config, Core Infra & CLI

### Overview
This subsystem is the load-bearing skeleton the rest of the bot stands on: it holds every tunable number the spec defines (`meme_intelligence/config/settings.py`), the shared low-level plumbing every collector/analyzer/alert sink is built from (`meme_intelligence/core/`: cache, rate limiter, retry, provider-pool failover, error hierarchy, logging), and the operator-facing entry point (`meme_intelligence/__main__.py`) that wires all of it together into runnable commands.

`settings.py` implements Rule 17 (no hardcoded values) literally: ~44 frozen dataclasses, one per functional group (scoring weights, alert thresholds, provider endpoints/rate limits, discovery filters, pump.fun discovery, security thresholds, wallet/social credit gates, the self-learning "mind" layer, LightGBM hyperparameters, rug-signal scoring, trade execution safety, backtesting, etc.), each with a `__post_init__` that validates ranges/sums/orderings and raises `ConfigurationError` on anything invalid (fail loud, never silently misconfigure — Rule 6). Every field can be overridden by an env var named `MEMEINTEL_<GROUP>_<FIELD>`, loaded generically by `_load_group()`/`Settings.from_env()` with type coercion matching the dataclass field's default type. `get_settings()` lazily loads `.env` (real env vars win) then builds the singleton `Settings` object every other module imports.

`core/` supplies the shared infrastructure every provider-facing collector composes: `TTLCache` (async-safe, LRU-bounded, per-entry TTL — first line of defense against API abuse, Rules 10/11), `RateLimiter` (async token bucket, `per_minute()` convenience constructor matching how providers document limits), `retry_async` (exponential backoff + jitter, only for `TransientCollectorError`, honors a 429's `Retry-After` hint), `ProviderPool` (ordered failover across interchangeable providers with per-provider consecutive-failure cooldown — Rule 9's "degrade gracefully" made concrete), the `MemeIntelError` exception hierarchy that lets callers distinguish permanent vs. transient vs. "all providers exhausted" vs. "not enough data to judge" (Rule 8: never fabricate), and `logging_setup` (console + rotating file handler under the `meme_intelligence` logger namespace, Rule 13).

`__main__.py` is the argparse-based CLI: it builds every collector/service from `Settings` via a set of `build_*` factory functions (each wiring the right base URL, rate limiter, and shared HTTP kwargs from `settings.http`/`settings.providers`), then dispatches to one of 16 subcommands, from one-shot lookups (`search`, `token`, `security`) through the full research pipeline (`plan`, `report`, `quick`, `compare`) to the 24/7 `monitor` loop that is the bot's actual production mode on the operator's droplet.

### Files
| File | Role |
|---|---|
| `meme_intelligence/config/settings.py` | Every tunable setting as frozen dataclasses grouped by function; env-var override loader (`MEMEINTEL_<GROUP>_<FIELD>`); `.env` loader; process-wide `get_settings()` singleton + `reset_settings()` for tests |
| `meme_intelligence/core/cache.py` | `TTLCache` — async-safe, LRU-bounded, per-entry-TTL in-memory cache used by every collector to avoid re-requesting unchanged data |
| `meme_intelligence/core/errors.py` | Exception hierarchy: `MemeIntelError` → `ConfigurationError`, `CollectorError` → `TransientCollectorError` → `RateLimitedError`; plus `AllProvidersFailedError`, `InsufficientDataError` |
| `meme_intelligence/core/logging_setup.py` | Configures the root `meme_intelligence` logger: console handler + 5MB x3-backup rotating file handler under `log_dir`; `get_logger(name)` for child loggers |
| `meme_intelligence/core/provider_pool.py` | `ProviderPool` — ordered multi-provider failover with consecutive-failure cooldown tracking and per-provider health snapshots |
| `meme_intelligence/core/rate_limiter.py` | `RateLimiter` — async token-bucket limiter with a `per_minute()` convenience constructor |
| `meme_intelligence/core/retry.py` | `retry_async()` — exponential backoff + jitter retry helper, retries only `TransientCollectorError` (and subtypes), honors `Retry-After` |
| `meme_intelligence/__main__.py` | CLI entry point: argparse subcommands, `build_*` factory functions that assemble collectors/services from `Settings`, and the async command handlers |

### Configuration
Format: **Field** — default — `ENV VAR`. Grouped exactly as declared in `settings.py`; group prefix shown once per section.

### `weights` — `ScoringWeights` (group `WEIGHTS`) — Part 31 Framework Consistency Lock, must sum to 1.0
- foundation — 0.15 — `MEMEINTEL_WEIGHTS_FOUNDATION`
- security — 0.15 — `MEMEINTEL_WEIGHTS_SECURITY`
- community — 0.15 — `MEMEINTEL_WEIGHTS_COMMUNITY`
- blockchain — 0.15 — `MEMEINTEL_WEIGHTS_BLOCKCHAIN`
- momentum — 0.15 — `MEMEINTEL_WEIGHTS_MOMENTUM`
- narrative — 0.15 — `MEMEINTEL_WEIGHTS_NARRATIVE`
- timing — 0.10 — `MEMEINTEL_WEIGHTS_TIMING`

### `security_weights` — `SecuritySubWeights` (group `SECURITY_WEIGHTS`) — Part 33 S11, must sum to 1.0
- contract — 0.25 — `MEMEINTEL_SECURITY_WEIGHTS_CONTRACT`
- liquidity — 0.20 — `MEMEINTEL_SECURITY_WEIGHTS_LIQUIDITY`
- distribution — 0.20 — `MEMEINTEL_SECURITY_WEIGHTS_DISTRIBUTION`
- developer — 0.20 — `MEMEINTEL_SECURITY_WEIGHTS_DEVELOPER`
- manipulation — 0.15 — `MEMEINTEL_SECURITY_WEIGHTS_MANIPULATION`

### `community_weights` — `CommunitySubWeights` (group `COMMUNITY_WEIGHTS`) — 5x0.20, must sum to 1.0
- engagement / growth / loyalty / creativity / dev_relationship — all 0.20 — `MEMEINTEL_COMMUNITY_WEIGHTS_ENGAGEMENT` / `_GROWTH` / `_LOYALTY` / `_CREATIVITY` / `_DEV_RELATIONSHIP`

### `onchain_weights` — `OnChainSubWeights` (group `ONCHAIN_WEIGHTS`) — must sum to 1.0
- holder_health — 0.20 — `MEMEINTEL_ONCHAIN_WEIGHTS_HOLDER_HEALTH`
- smart_money — 0.20 — `MEMEINTEL_ONCHAIN_WEIGHTS_SMART_MONEY`
- whale_behavior — 0.15 — `MEMEINTEL_ONCHAIN_WEIGHTS_WHALE_BEHAVIOR`
- developer_activity — 0.15 — `MEMEINTEL_ONCHAIN_WEIGHTS_DEVELOPER_ACTIVITY`
- volume_quality — 0.15 — `MEMEINTEL_ONCHAIN_WEIGHTS_VOLUME_QUALITY`
- token_flow — 0.15 — `MEMEINTEL_ONCHAIN_WEIGHTS_TOKEN_FLOW`

### `foundation_weights` — `FoundationSubWeights` (group `FOUNDATION_WEIGHTS`) — must sum to 1.0
- meme_strength — 0.20 — `MEMEINTEL_FOUNDATION_WEIGHTS_MEME_STRENGTH`
- narrative — 0.20 — `MEMEINTEL_FOUNDATION_WEIGHTS_NARRATIVE`
- brand — 0.15 — `MEMEINTEL_FOUNDATION_WEIGHTS_BRAND`
- community_quality — 0.20 — `MEMEINTEL_FOUNDATION_WEIGHTS_COMMUNITY_QUALITY`
- dev_communication — 0.15 — `MEMEINTEL_FOUNDATION_WEIGHTS_DEV_COMMUNICATION`
- long_term — 0.10 — `MEMEINTEL_FOUNDATION_WEIGHTS_LONG_TERM`

### `bands` — `ClassificationBands` (group `BANDS`) — Parts 10/12/20, must be strictly descending
- elite — 90.0 — `MEMEINTEL_BANDS_ELITE`
- strong_candidate — 80.0 — `MEMEINTEL_BANDS_STRONG_CANDIDATE`
- watchlist — 70.0 — `MEMEINTEL_BANDS_WATCHLIST`
- speculative — 60.0 — `MEMEINTEL_BANDS_SPECULATIVE`

### `alerts` — `AlertThresholds` (group `ALERTS`) — Part 2 S4 human-review gates
- security — 80.0 — `MEMEINTEL_ALERTS_SECURITY`
- community — 70.0 — `MEMEINTEL_ALERTS_COMMUNITY`
- liquidity — 70.0 — `MEMEINTEL_ALERTS_LIQUIDITY`
- onchain — 75.0 — `MEMEINTEL_ALERTS_ONCHAIN`
- overall — 85.0 — `MEMEINTEL_ALERTS_OVERALL`
- momentum — 70.0 — `MEMEINTEL_ALERTS_MOMENTUM`
- strong_candidate_overall — 88.0 — `MEMEINTEL_ALERTS_STRONG_CANDIDATE_OVERALL` (must be ≥ overall)
- strong_candidate_min_liquidity_usd — 25000.0 — `MEMEINTEL_ALERTS_STRONG_CANDIDATE_MIN_LIQUIDITY_USD` (depth veto for HIGH opportunity alerts)
- strong_candidate_min_ai_confidence — 40.0 — `MEMEINTEL_ALERTS_STRONG_CANDIDATE_MIN_AI_CONFIDENCE` (AI veto; no-op if no AI ran)
- copycat_veto_enabled — True — `MEMEINTEL_ALERTS_COPYCAT_VETO_ENABLED`
- copycat_liquidity_ratio — 10.0 — `MEMEINTEL_ALERTS_COPYCAT_LIQUIDITY_RATIO`
- copycat_min_liquidity_usd — 100000.0 — `MEMEINTEL_ALERTS_COPYCAT_MIN_LIQUIDITY_USD`
- opportunity_min_liquidity_usd — 0.0 (OFF) — `MEMEINTEL_ALERTS_OPPORTUNITY_MIN_LIQUIDITY_USD`
- opportunity_min_market_cap_usd — 0.0 (OFF) — `MEMEINTEL_ALERTS_OPPORTUNITY_MIN_MARKET_CAP_USD`
- opportunity_max_liquidity_usd — 0.0 (OFF) — `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD`
- opportunity_max_market_cap_usd — 0.0 (OFF) — `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_MARKET_CAP_USD`
- opportunity_max_age_hours — **1.0 (ACTIVE)** — `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS` (buy-side alerts suppressed for pools older than this; 0 = off; unknown age never trips it)
- checklist_sell_tax_max_percent — 15.0 — `MEMEINTEL_ALERTS_CHECKLIST_SELL_TAX_MAX_PERCENT`
- checklist_new_launch_minutes — 60.0 — `MEMEINTEL_ALERTS_CHECKLIST_NEW_LAUNCH_MINUTES`
- momentum_min_security_score — 0.0 (OFF/dormant) — `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE` (part of the dormant wallet-credit-gate kit; set to 50 to enable, alongside `wallet.credit_gate_min_security_score`)
- check_dumped_drop_percent — 80.0 — `MEMEINTEL_ALERTS_CHECK_DUMPED_DROP_PERCENT` (display-only, `/check` card)

### `intervals` — `ScanIntervals` (group `INTERVALS`) — Part 21 S4, seconds
- ultra_fast — 7.0 — `MEMEINTEL_INTERVALS_ULTRA_FAST`
- fast — 45.0 — `MEMEINTEL_INTERVALS_FAST`
- research — 600.0 — `MEMEINTEL_INTERVALS_RESEARCH`
- historical — 86400.0 — `MEMEINTEL_INTERVALS_HISTORICAL`

### `http` — `HttpSettings` (group `HTTP`) — shared client behavior, Rules 6/7/11
- timeout_seconds — 10.0 — `MEMEINTEL_HTTP_TIMEOUT_SECONDS`
- retry_attempts — 4 — `MEMEINTEL_HTTP_RETRY_ATTEMPTS`
- retry_base_delay — 0.5 — `MEMEINTEL_HTTP_RETRY_BASE_DELAY`
- retry_max_delay — 8.0 — `MEMEINTEL_HTTP_RETRY_MAX_DELAY`
- cache_ttl_seconds — 30.0 — `MEMEINTEL_HTTP_CACHE_TTL_SECONDS`
- cache_max_entries — 2048 — `MEMEINTEL_HTTP_CACHE_MAX_ENTRIES`

### `providers` — `ProviderSettings` (group `PROVIDERS`) — endpoints + rate limits + failover
- dexscreener_base_url — `https://api.dexscreener.com` — `MEMEINTEL_PROVIDERS_DEXSCREENER_BASE_URL`
- dexscreener_requests_per_minute — 240.0 — `MEMEINTEL_PROVIDERS_DEXSCREENER_REQUESTS_PER_MINUTE` (doc limit 300/min)
- geckoterminal_base_url — `https://api.geckoterminal.com` — `MEMEINTEL_PROVIDERS_GECKOTERMINAL_BASE_URL`
- geckoterminal_requests_per_minute — 25.0 — `MEMEINTEL_PROVIDERS_GECKOTERMINAL_REQUESTS_PER_MINUTE` (doc free limit 30/min)
- goplus_base_url — `https://api.gopluslabs.io` — `MEMEINTEL_PROVIDERS_GOPLUS_BASE_URL`
- goplus_requests_per_minute — 20.0 — `MEMEINTEL_PROVIDERS_GOPLUS_REQUESTS_PER_MINUTE`
- coingecko_base_url — `https://api.coingecko.com` — `MEMEINTEL_PROVIDERS_COINGECKO_BASE_URL`
- coingecko_requests_per_minute — 10.0 — `MEMEINTEL_PROVIDERS_COINGECKO_REQUESTS_PER_MINUTE`
- helius_rpc_url — `https://mainnet.helius-rpc.com` — `MEMEINTEL_PROVIDERS_HELIUS_RPC_URL`
- helius_api_url — `https://api.helius.xyz` — `MEMEINTEL_PROVIDERS_HELIUS_API_URL`
- helius_requests_per_minute — 120.0 — `MEMEINTEL_PROVIDERS_HELIUS_REQUESTS_PER_MINUTE`
- birdeye_base_url — `https://public-api.birdeye.so` — `MEMEINTEL_PROVIDERS_BIRDEYE_BASE_URL`
- birdeye_requests_per_minute — 20.0 — `MEMEINTEL_PROVIDERS_BIRDEYE_REQUESTS_PER_MINUTE`
- jupiter_base_url — `https://api.jup.ag` — `MEMEINTEL_PROVIDERS_JUPITER_BASE_URL`
- jupiter_requests_per_minute — 50.0 — `MEMEINTEL_PROVIDERS_JUPITER_REQUESTS_PER_MINUTE` (doc limit 60/min)
- pumpportal_ws_url — `wss://pumpportal.fun/api/data` — `MEMEINTEL_PROVIDERS_PUMPPORTAL_WS_URL`
- pumpfun_base_url — `https://frontend-api-v3.pump.fun` — `MEMEINTEL_PROVIDERS_PUMPFUN_BASE_URL` (unofficial, can change)
- pumpfun_requests_per_minute — 30.0 — `MEMEINTEL_PROVIDERS_PUMPFUN_REQUESTS_PER_MINUTE`
- lunarcrush_base_url — `https://lunarcrush.com/api4` — `MEMEINTEL_PROVIDERS_LUNARCRUSH_BASE_URL`
- lunarcrush_requests_per_minute — 10.0 — `MEMEINTEL_PROVIDERS_LUNARCRUSH_REQUESTS_PER_MINUTE`
- failure_threshold — 3 — `MEMEINTEL_PROVIDERS_FAILURE_THRESHOLD` (consecutive failures before cooldown)
- cooldown_seconds — 60.0 — `MEMEINTEL_PROVIDERS_COOLDOWN_SECONDS`

### `discovery` — `DiscoverySettings` (group `DISCOVERY`) — Part 3 / Part 15 S6 / Part 27
- min_liquidity_usd — 5000.0 — `MEMEINTEL_DISCOVERY_MIN_LIQUIDITY_USD`
- target_liquidity_usd — 50000.0 — `MEMEINTEL_DISCOVERY_TARGET_LIQUIDITY_USD`
- min_volume_24h_usd — 1000.0 — `MEMEINTEL_DISCOVERY_MIN_VOLUME_24H_USD`
- target_volume_24h_usd — 50000.0 — `MEMEINTEL_DISCOVERY_TARGET_VOLUME_24H_USD`
- max_age_hours — 24.0 — `MEMEINTEL_DISCOVERY_MAX_AGE_HOURS`
- target_txns_24h — 200 — `MEMEINTEL_DISCOVERY_TARGET_TXNS_24H`

### `pumpfun` — `PumpFunSettings` (group `PUMPFUN`) — Part 32.5 S3, **DORMANT in monitor by default**
- enable_in_monitor — False — `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR` (also settable via CLI `monitor --pumpfun`)
- launchpads — `"pump"` — `MEMEINTEL_PUMPFUN_LAUNCHPADS` (comma-separated)
- max_creator_buy_percent — 20.0 — `MEMEINTEL_PUMPFUN_MAX_CREATOR_BUY_PERCENT`
- max_pending — 500 — `MEMEINTEL_PUMPFUN_MAX_PENDING`
- pending_ttl_hours — 24.0 — `MEMEINTEL_PUMPFUN_PENDING_TTL_HOURS`
- recheck_interval_seconds — 120.0 — `MEMEINTEL_PUMPFUN_RECHECK_INTERVAL_SECONDS`
- max_rechecks_per_cycle — 8 — `MEMEINTEL_PUMPFUN_MAX_RECHECKS_PER_CYCLE`
- min_market_cap_growth_ratio — 1.5 — `MEMEINTEL_PUMPFUN_MIN_MARKET_CAP_GROWTH_RATIO`
- min_usd_market_cap — 10000.0 — `MEMEINTEL_PUMPFUN_MIN_USD_MARKET_CAP`
- min_reply_count — 5 — `MEMEINTEL_PUMPFUN_MIN_REPLY_COUNT`
- max_last_trade_age_minutes — 30.0 — `MEMEINTEL_PUMPFUN_MAX_LAST_TRADE_AGE_MINUTES`
- ready_ttl_hours — 72.0 — `MEMEINTEL_PUMPFUN_READY_TTL_HOURS`

### `boost_watcher` — `BoostWatcherSettings` (group `BOOST_WATCHER`) — Project 5, **DORMANT by default**
- enabled — False — `MEMEINTEL_BOOST_WATCHER_ENABLED` (also `monitor --boosts`)
- threshold — 100.0 — `MEMEINTEL_BOOST_WATCHER_THRESHOLD`
- poll_interval_seconds — 30.0 — `MEMEINTEL_BOOST_WATCHER_POLL_INTERVAL_SECONDS`
- chain_filter — `"solana"` — `MEMEINTEL_BOOST_WATCHER_CHAIN_FILTER` (`""` = every chain)
- max_seen_keys — 5000 — `MEMEINTEL_BOOST_WATCHER_MAX_SEEN_KEYS`

### `security` — `SecurityThresholds` (group `SECURITY`) — Parts 4/18/33
- max_tax_percent — 10.0 — `MEMEINTEL_SECURITY_MAX_TAX_PERCENT`
- extreme_tax_percent — 25.0 — `MEMEINTEL_SECURITY_EXTREME_TAX_PERCENT`
- min_liquidity_usd — 5000.0 — `MEMEINTEL_SECURITY_MIN_LIQUIDITY_USD`
- healthy_liquidity_usd — 50000.0 — `MEMEINTEL_SECURITY_HEALTHY_LIQUIDITY_USD`
- min_lp_locked_percent — 50.0 — `MEMEINTEL_SECURITY_MIN_LP_LOCKED_PERCENT`
- good_lp_locked_percent — 80.0 — `MEMEINTEL_SECURITY_GOOD_LP_LOCKED_PERCENT`
- warn_top_holder_percent — 10.0 — `MEMEINTEL_SECURITY_WARN_TOP_HOLDER_PERCENT`
- max_top_holder_percent — 20.0 — `MEMEINTEL_SECURITY_MAX_TOP_HOLDER_PERCENT`
- warn_top10_holder_percent — 50.0 — `MEMEINTEL_SECURITY_WARN_TOP10_HOLDER_PERCENT`
- max_top10_holder_percent — 70.0 — `MEMEINTEL_SECURITY_MAX_TOP10_HOLDER_PERCENT`
- min_holder_count — 50 — `MEMEINTEL_SECURITY_MIN_HOLDER_COUNT`
- warn_creator_percent — 5.0 — `MEMEINTEL_SECURITY_WARN_CREATOR_PERCENT`
- max_creator_percent — 10.0 — `MEMEINTEL_SECURITY_MAX_CREATOR_PERCENT`
- max_round_trip_loss_percent — 50.0 — `MEMEINTEL_SECURITY_MAX_ROUND_TRIP_LOSS_PERCENT`
- extreme_round_trip_loss_percent — 90.0 — `MEMEINTEL_SECURITY_EXTREME_ROUND_TRIP_LOSS_PERCENT`

### `token_weights` — `TokenSubWeights` (group `TOKEN_WEIGHTS`) — Part 7 S13, must sum to 1.0
- valuation — 0.20 — `MEMEINTEL_TOKEN_WEIGHTS_VALUATION`
- liquidity — 0.20 — `MEMEINTEL_TOKEN_WEIGHTS_LIQUIDITY`
- supply — 0.15 — `MEMEINTEL_TOKEN_WEIGHTS_SUPPLY`
- volume — 0.15 — `MEMEINTEL_TOKEN_WEIGHTS_VOLUME`
- competition — 0.15 — `MEMEINTEL_TOKEN_WEIGHTS_COMPETITION`
- catalysts — 0.15 — `MEMEINTEL_TOKEN_WEIGHTS_CATALYSTS`

### `trade_weights` — `TradeScoreWeights` (group `TRADE_WEIGHTS`) — Part 8 S13, must sum to 1.0
- setup_quality — 0.20 — `MEMEINTEL_TRADE_WEIGHTS_SETUP_QUALITY`
- security — 0.20 — `MEMEINTEL_TRADE_WEIGHTS_SECURITY`
- community — 0.15 — `MEMEINTEL_TRADE_WEIGHTS_COMMUNITY`
- onchain — 0.15 — `MEMEINTEL_TRADE_WEIGHTS_ONCHAIN`
- market_conditions — 0.15 — `MEMEINTEL_TRADE_WEIGHTS_MARKET_CONDITIONS`
- risk_reward — 0.15 — `MEMEINTEL_TRADE_WEIGHTS_RISK_REWARD`

### `token` — `TokenThresholds` (group `TOKEN`) — Part 7
- early_stage_mcap_usd — 1,000,000.0 — `MEMEINTEL_TOKEN_EARLY_STAGE_MCAP_USD`
- mature_stage_mcap_usd — 100,000,000.0 — `MEMEINTEL_TOKEN_MATURE_STAGE_MCAP_USD`
- fdv_dilution_warn_ratio — 1.5 — `MEMEINTEL_TOKEN_FDV_DILUTION_WARN_RATIO`
- fdv_dilution_severe_ratio — 3.0 — `MEMEINTEL_TOKEN_FDV_DILUTION_SEVERE_RATIO`
- low_liquidity_to_mcap_percent — 1.0 — `MEMEINTEL_TOKEN_LOW_LIQUIDITY_TO_MCAP_PERCENT`
- healthy_liquidity_to_mcap_percent — 5.0 — `MEMEINTEL_TOKEN_HEALTHY_LIQUIDITY_TO_MCAP_PERCENT`
- min_volume_to_mcap_percent — 1.0 — `MEMEINTEL_TOKEN_MIN_VOLUME_TO_MCAP_PERCENT`
- target_volume_to_mcap_percent — 20.0 — `MEMEINTEL_TOKEN_TARGET_VOLUME_TO_MCAP_PERCENT`
- excessive_volume_to_mcap_percent — 500.0 — `MEMEINTEL_TOKEN_EXCESSIVE_VOLUME_TO_MCAP_PERCENT`
- min_circulating_fraction — 0.3 — `MEMEINTEL_TOKEN_MIN_CIRCULATING_FRACTION`
- healthy_circulating_fraction — 0.9 — `MEMEINTEL_TOKEN_HEALTHY_CIRCULATING_FRACTION`

### `trading` — `TradingSettings` (group `TRADING`) — Part 8/9 S3, guidance ceilings only (never executes)
- high_conviction_min_score — 80.0 — `MEMEINTEL_TRADING_HIGH_CONVICTION_MIN_SCORE`
- medium_conviction_min_score — 65.0 — `MEMEINTEL_TRADING_MEDIUM_CONVICTION_MIN_SCORE`
- high_conviction_min_security — 75.0 — `MEMEINTEL_TRADING_HIGH_CONVICTION_MIN_SECURITY`
- min_confirmation_coverage — 0.5 — `MEMEINTEL_TRADING_MIN_CONFIRMATION_COVERAGE`
- high_conviction_max_position_percent — 5.0 — `MEMEINTEL_TRADING_HIGH_CONVICTION_MAX_POSITION_PERCENT`
- medium_conviction_max_position_percent — 2.0 — `MEMEINTEL_TRADING_MEDIUM_CONVICTION_MAX_POSITION_PERCENT`
- speculative_max_position_percent — 0.5 — `MEMEINTEL_TRADING_SPECULATIVE_MAX_POSITION_PERCENT`

### `risk_weights` — `RiskSubWeights` (group `RISK_WEIGHTS`) — Part 9 S6, must sum to 1.0
- security — 0.25 — `MEMEINTEL_RISK_WEIGHTS_SECURITY`
- market — 0.20 — `MEMEINTEL_RISK_WEIGHTS_MARKET`
- community — 0.15 — `MEMEINTEL_RISK_WEIGHTS_COMMUNITY`
- token — 0.20 — `MEMEINTEL_RISK_WEIGHTS_TOKEN`
- execution — 0.20 — `MEMEINTEL_RISK_WEIGHTS_EXECUTION`

### `risk` — `RiskSettings` (group `RISK`) — Part 9 S2/5/8, portfolio guidance only
- max_open_positions — 10 — `MEMEINTEL_RISK_MAX_OPEN_POSITIONS`
- max_single_position_percent — 10.0 — `MEMEINTEL_RISK_MAX_SINGLE_POSITION_PERCENT`
- max_chain_concentration_percent — 50.0 — `MEMEINTEL_RISK_MAX_CHAIN_CONCENTRATION_PERCENT`
- max_narrative_concentration_percent — 40.0 — `MEMEINTEL_RISK_MAX_NARRATIVE_CONCENTRATION_PERCENT`
- max_total_exposure_percent — 80.0 — `MEMEINTEL_RISK_MAX_TOTAL_EXPOSURE_PERCENT`
- reduced_daily_loss_percent — 5.0 — `MEMEINTEL_RISK_REDUCED_DAILY_LOSS_PERCENT`
- defensive_daily_loss_percent — 10.0 — `MEMEINTEL_RISK_DEFENSIVE_DAILY_LOSS_PERCENT`
- reduced_weekly_loss_percent — 10.0 — `MEMEINTEL_RISK_REDUCED_WEEKLY_LOSS_PERCENT`
- defensive_weekly_loss_percent — 20.0 — `MEMEINTEL_RISK_DEFENSIVE_WEEKLY_LOSS_PERCENT`

### `database` — `DatabaseSettings` (group `DATABASE`) — Part 13 S5
- path — `"data/meme_intelligence.sqlite3"` — `MEMEINTEL_DATABASE_PATH`

### `workflow` — `WorkflowSettings` (group `WORKFLOW`) — Part 11, drives daily/monitor
- networks — `"solana"` — `MEMEINTEL_WORKFLOW_NETWORKS` (comma-separated)
- top_candidates — 5 — `MEMEINTEL_WORKFLOW_TOP_CANDIDATES`
- watchlist_review_limit — 10 — `MEMEINTEL_WORKFLOW_WATCHLIST_REVIEW_LIMIT`
- risk_on_btc_change_percent — 2.0 — `MEMEINTEL_WORKFLOW_RISK_ON_BTC_CHANGE_PERCENT`
- risk_off_btc_drop_percent — 3.0 — `MEMEINTEL_WORKFLOW_RISK_OFF_BTC_DROP_PERCENT`
- monitor_interval_seconds — 45.0 — `MEMEINTEL_WORKFLOW_MONITOR_INTERVAL_SECONDS`
- watchlist_recheck_cycles — 10 — `MEMEINTEL_WORKFLOW_WATCHLIST_RECHECK_CYCLES`
- max_tracked_keys — 50000 — `MEMEINTEL_WORKFLOW_MAX_TRACKED_KEYS`
- insufficient_data_retry_enabled — True — `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_ENABLED`
- insufficient_data_min_coverage — 0.5 — `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_MIN_COVERAGE`
- insufficient_data_retry_minutes — 15.0 — `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_MINUTES`
- insufficient_data_max_age_minutes — 120.0 — `MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_MAX_AGE_MINUTES`

### `momentum_weights` — `MomentumSubWeights` (group `MOMENTUM_WEIGHTS`) — Part 14 S5, 4x0.25
- price / volume / social / onchain — all 0.25 — `MEMEINTEL_MOMENTUM_WEIGHTS_PRICE` / `_VOLUME` / `_SOCIAL` / `_ONCHAIN`

### `opportunity_weights` — `OpportunityWeights` (group `OPPORTUNITY_WEIGHTS`) — Part 28 S5, watchlist ranking axis (never affects master score)
- growth_potential — 0.30 — `MEMEINTEL_OPPORTUNITY_WEIGHTS_GROWTH_POTENTIAL`
- momentum — 0.25 — `MEMEINTEL_OPPORTUNITY_WEIGHTS_MOMENTUM`
- foundation — 0.20 — `MEMEINTEL_OPPORTUNITY_WEIGHTS_FOUNDATION`
- risk — 0.15 — `MEMEINTEL_OPPORTUNITY_WEIGHTS_RISK`
- timing — 0.10 — `MEMEINTEL_OPPORTUNITY_WEIGHTS_TIMING`

### `momentum` — `MomentumThresholds` (group `MOMENTUM`) — Parts 14/26
- target_trend_24h_percent — 30.0 — `MEMEINTEL_MOMENTUM_TARGET_TREND_24H_PERCENT`
- spike_1h_percent — 30.0 — `MEMEINTEL_MOMENTUM_SPIKE_1H_PERCENT`
- late_extension_24h_percent — 100.0 — `MEMEINTEL_MOMENTUM_LATE_EXTENSION_24H_PERCENT`
- volume_acceleration_ratio — 1.5 — `MEMEINTEL_MOMENTUM_VOLUME_ACCELERATION_RATIO`
- volume_fade_ratio — 0.5 — `MEMEINTEL_MOMENTUM_VOLUME_FADE_RATIO`
- buy_ratio_shift — 0.05 — `MEMEINTEL_MOMENTUM_BUY_RATIO_SHIFT`
- target_social_growth_7d_percent — 30.0 — `MEMEINTEL_MOMENTUM_TARGET_SOCIAL_GROWTH_7D_PERCENT`

### `alert_engine` — `AlertEngineSettings` (group `ALERT_ENGINE`) — Part 13 S4, Part 29 S6
- cooldown_seconds — 900.0 — `MEMEINTEL_ALERT_ENGINE_COOLDOWN_SECONDS` (same token+type suppressed within this window)
- score_drop_review_points — 15.0 — `MEMEINTEL_ALERT_ENGINE_SCORE_DROP_REVIEW_POINTS`
- dead_liquidity_usd — 500.0 — `MEMEINTEL_ALERT_ENGINE_DEAD_LIQUIDITY_USD` (below this = token treated as dead/archived)
- risk_alerts_require_interest — True — `MEMEINTEL_ALERT_ENGINE_RISK_ALERTS_REQUIRE_INTEREST` (protective alerts on never-alerted tokens demoted to LOW priority)

### `alert_delivery` — `AlertDeliverySettings` (group `ALERT_DELIVERY`) — Part 29 S8
- telegram_routes — `""` — `MEMEINTEL_ALERT_DELIVERY_TELEGRAM_ROUTES` (`category=chat_id[,...]`)
- discord_routes — `""` — `MEMEINTEL_ALERT_DELIVERY_DISCORD_ROUTES` (`category=webhook_url[,...]`)
- external_min_priority — `"medium"` — `MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY` (critical|high|medium|low)
- requests_per_minute — 20.0 — `MEMEINTEL_ALERT_DELIVERY_REQUESTS_PER_MINUTE`

### `community` — `CommunityThresholds` (group `COMMUNITY`) — Part 5/18 S8
- excellent_engagement_rate_percent — 5.0 — `MEMEINTEL_COMMUNITY_EXCELLENT_ENGAGEMENT_RATE_PERCENT`
- fake_engagement_rate_percent — 0.5 — `MEMEINTEL_COMMUNITY_FAKE_ENGAGEMENT_RATE_PERCENT`
- min_followers_for_fake_check — 10000 — `MEMEINTEL_COMMUNITY_MIN_FOLLOWERS_FOR_FAKE_CHECK`
- bot_follower_warn_percent — 30.0 — `MEMEINTEL_COMMUNITY_BOT_FOLLOWER_WARN_PERCENT`
- bot_follower_artificial_percent — 50.0 — `MEMEINTEL_COMMUNITY_BOT_FOLLOWER_ARTIFICIAL_PERCENT`
- duplicate_message_warn_percent — 20.0 — `MEMEINTEL_COMMUNITY_DUPLICATE_MESSAGE_WARN_PERCENT`
- telegram_active_target_percent — 15.0 — `MEMEINTEL_COMMUNITY_TELEGRAM_ACTIVE_TARGET_PERCENT`
- target_growth_rate_7d_percent — 30.0 — `MEMEINTEL_COMMUNITY_TARGET_GROWTH_RATE_7D_PERCENT`
- target_dev_updates_per_week — 3.0 — `MEMEINTEL_COMMUNITY_TARGET_DEV_UPDATES_PER_WEEK`
- target_user_content_per_day — 20.0 — `MEMEINTEL_COMMUNITY_TARGET_USER_CONTENT_PER_DAY`

### `viral_weights` — `ViralSubWeights` (group `VIRAL_WEIGHTS`) — Part 19 S3, 5x0.20
- memorability / shareability / emotional_impact / cultural_timing / community_participation — all 0.20 — `MEMEINTEL_VIRAL_WEIGHTS_MEMORABILITY` / `_SHAREABILITY` / `_EMOTIONAL_IMPACT` / `_CULTURAL_TIMING` / `_COMMUNITY_PARTICIPATION`

### `narrative_weights` — `NarrativeSubWeights` (group `NARRATIVE_WEIGHTS`) — Part 19 S11, 5x0.20
- meme_strength / cultural_timing / viral_potential / community_creativity / long_term_strength — all 0.20 — `MEMEINTEL_NARRATIVE_WEIGHTS_MEME_STRENGTH` / `_CULTURAL_TIMING` / `_VIRAL_POTENTIAL` / `_COMMUNITY_CREATIVITY` / `_LONG_TERM_STRENGTH`

### `narrative` — `NarrativeThresholds` (group `NARRATIVE`) — Part 19 S6
- positive_sentiment_percent — 60.0 — `MEMEINTEL_NARRATIVE_POSITIVE_SENTIMENT_PERCENT`
- negative_sentiment_percent — 40.0 — `MEMEINTEL_NARRATIVE_NEGATIVE_SENTIMENT_PERCENT`

### `onchain` — `OnChainThresholds` (group `ONCHAIN`) — Part 6 S2-12
- min_holder_count — 50 — `MEMEINTEL_ONCHAIN_MIN_HOLDER_COUNT`
- target_holder_count — 2000 — `MEMEINTEL_ONCHAIN_TARGET_HOLDER_COUNT`
- holder_growth_target_percent_24h — 20.0 — `MEMEINTEL_ONCHAIN_HOLDER_GROWTH_TARGET_PERCENT_24H`
- healthy_trades_per_trader — 3.0 — `MEMEINTEL_ONCHAIN_HEALTHY_TRADES_PER_TRADER`
- wash_trades_per_trader — 10.0 — `MEMEINTEL_ONCHAIN_WASH_TRADES_PER_TRADER`
- volume_per_holder_healthy_usd — 500.0 — `MEMEINTEL_ONCHAIN_VOLUME_PER_HOLDER_HEALTHY_USD`
- volume_per_holder_suspicious_usd — 5000.0 — `MEMEINTEL_ONCHAIN_VOLUME_PER_HOLDER_SUSPICIOUS_USD`
- buy_ratio_weak — 0.35 — `MEMEINTEL_ONCHAIN_BUY_RATIO_WEAK`
- buy_ratio_strong — 0.60 — `MEMEINTEL_ONCHAIN_BUY_RATIO_STRONG`

### `smart_money_weights` — `SmartMoneySubWeights` (group `SMART_MONEY_WEIGHTS`) — Part 17 S11, 5x0.20
- quality_wallets / historical_success / entry_timing / holding_behavior / risk_signals — all 0.20 — `MEMEINTEL_SMART_MONEY_WEIGHTS_QUALITY_WALLETS` / `_HISTORICAL_SUCCESS` / `_ENTRY_TIMING` / `_HOLDING_BEHAVIOR` / `_RISK_SIGNALS`

### `wallet` — `WalletIntelSettings` (group `WALLET`) — Part 17, **off in monitor by default**, credit-gated
- whale_min_percent — 1.0 — `MEMEINTEL_WALLET_WHALE_MIN_PERCENT`
- risk_whale_percent — 5.0 — `MEMEINTEL_WALLET_RISK_WHALE_PERCENT`
- top_holders_limit — 20 — `MEMEINTEL_WALLET_TOP_HOLDERS_LIMIT`
- recent_trades_limit — 50 — `MEMEINTEL_WALLET_RECENT_TRADES_LIMIT`
- target_accumulating_wallets — 10 — `MEMEINTEL_WALLET_TARGET_ACCUMULATING_WALLETS`
- artificial_same_size_fraction — 0.30 — `MEMEINTEL_WALLET_ARTIFICIAL_SAME_SIZE_FRACTION`
- dominant_buyer_volume_fraction — 0.60 — `MEMEINTEL_WALLET_DOMINANT_BUYER_VOLUME_FRACTION`
- min_buy_volume_for_dominance_usd — 500.0 — `MEMEINTEL_WALLET_MIN_BUY_VOLUME_FOR_DOMINANCE_USD`
- enable_in_monitor — False — `MEMEINTEL_WALLET_ENABLE_IN_MONITOR`
- credit_gate_min_security_score — 50.0 — `MEMEINTEL_WALLET_CREDIT_GATE_MIN_SECURITY_SCORE` (a candidate must clear this security score before a metered wallet lookup runs)
- credit_gate_max_lookups_per_day — 200 — `MEMEINTEL_WALLET_CREDIT_GATE_MAX_LOOKUPS_PER_DAY` (0 = unlimited)
- credit_gate_cooldown_minutes — 60.0 — `MEMEINTEL_WALLET_CREDIT_GATE_COOLDOWN_MINUTES` (0 = off)

### `social` — `SocialIntelSettings` (group `SOCIAL`) — Roadmap item 5 (LunarCrush), **off by default**
- enable_in_monitor — False — `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR`
- credit_gate_min_security_score — 50.0 — `MEMEINTEL_SOCIAL_CREDIT_GATE_MIN_SECURITY_SCORE`
- credit_gate_max_lookups_per_day — 200 — `MEMEINTEL_SOCIAL_CREDIT_GATE_MAX_LOOKUPS_PER_DAY`
- credit_gate_cooldown_minutes — 60.0 — `MEMEINTEL_SOCIAL_CREDIT_GATE_COOLDOWN_MINUTES`

### `liquidity_probe` — `LiquidityProbeSettings` (group `LIQUIDITY_PROBE`) — Project 1, Solana only
- enabled — True (setting default; **functionally dormant without `MEMEINTEL_JUPITER_API_KEY`** — `build_jupiter()` returns `None` if the key is empty) — `MEMEINTEL_LIQUIDITY_PROBE_ENABLED`
- probe_sol_amount — 0.3 — `MEMEINTEL_LIQUIDITY_PROBE_PROBE_SOL_AMOUNT` (~$50 at time of writing; tune with SOL price)
- slippage_bps — 500 — `MEMEINTEL_LIQUIDITY_PROBE_SLIPPAGE_BPS`
- sell_confirm_fraction — 0.05 — `MEMEINTEL_LIQUIDITY_PROBE_SELL_CONFIRM_FRACTION`

### `ai` — `AISettings` (group `AI`) — Part 23 / Part 22 S11, activates only with `anthropic_api_key`
- model — `"claude-opus-4-8"` — `MEMEINTEL_AI_MODEL`
- max_tokens — 4096 — `MEMEINTEL_AI_MAX_TOKENS`
- effort — `"high"` — `MEMEINTEL_AI_EFFORT` (low|medium|high|xhigh|max)
- requests_per_minute — 10.0 — `MEMEINTEL_AI_REQUESTS_PER_MINUTE`
- timeout_seconds — 120.0 — `MEMEINTEL_AI_TIMEOUT_SECONDS`
- min_confidence — 20.0 — `MEMEINTEL_AI_MIN_CONFIDENCE` (below this, judgment discarded)
- enable_in_monitor — False (**DORMANT** — full per-token AI judging in the 24/7 monitor) — `MEMEINTEL_AI_ENABLE_IN_MONITOR`
- verify_opportunities — True (**ACTIVE default**) — `MEMEINTEL_AI_VERIFY_OPPORTUNITIES` (one AI judgment when a token clears every review gate, Part 32.5 S8)
- verify_skip_rug_score — 10.0 — `MEMEINTEL_AI_VERIFY_SKIP_RUG_SCORE` (paid verification skipped once the deterministic rug engine already scores at/above this)

### `backtest` — `BacktestSettings` (group `BACKTEST`) — Part 24
- windows_hours — `"1,24,168,720"` — `MEMEINTEL_BACKTEST_WINDOWS_HOURS` (1h/24h/7d/30d)
- window_tolerance_fraction — 0.35 — `MEMEINTEL_BACKTEST_WINDOW_TOLERANCE_FRACTION`
- success_price_change_percent — 50.0 — `MEMEINTEL_BACKTEST_SUCCESS_PRICE_CHANGE_PERCENT`
- failure_price_change_percent — -50.0 — `MEMEINTEL_BACKTEST_FAILURE_PRICE_CHANGE_PERCENT`
- survival_min_liquidity_usd — 1000.0 — `MEMEINTEL_BACKTEST_SURVIVAL_MIN_LIQUIDITY_USD`
- signal_high_score — 70.0 — `MEMEINTEL_BACKTEST_SIGNAL_HIGH_SCORE`
- signal_low_score — 50.0 — `MEMEINTEL_BACKTEST_SIGNAL_LOW_SCORE`
- alert_useful_drift_points — 10.0 — `MEMEINTEL_BACKTEST_ALERT_USEFUL_DRIFT_POINTS`
- alert_outcome_min_hours — 24.0 — `MEMEINTEL_BACKTEST_ALERT_OUTCOME_MIN_HOURS`
- min_predictions_for_weights — 10 — `MEMEINTEL_BACKTEST_MIN_PREDICTIONS_FOR_WEIGHTS`

### `learning` — `LearningSettings` (group `LEARNING`) — self-learning "mind" layer, **entirely DORMANT by default**
- enabled — False — `MEMEINTEL_LEARNING_ENABLED`
- enable_in_monitor — False — `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR` (also `monitor --learn`)
- pump_return_percent — 50.0 — `MEMEINTEL_LEARNING_PUMP_RETURN_PERCENT`
- dump_return_percent — -50.0 — `MEMEINTEL_LEARNING_DUMP_RETURN_PERCENT`
- horizons_hours — `"0.25,1,6,24"` — `MEMEINTEL_LEARNING_HORIZONS_HOURS`
- knn_neighbors — 25 — `MEMEINTEL_LEARNING_KNN_NEIGHBORS`
- recency_half_life_days — 30.0 — `MEMEINTEL_LEARNING_RECENCY_HALF_LIFE_DAYS`
- min_analog_neighbors — 5 — `MEMEINTEL_LEARNING_MIN_ANALOG_NEIGHBORS`
- archetype_min_cluster_size — 15 — `MEMEINTEL_LEARNING_ARCHETYPE_MIN_CLUSTER_SIZE`
- novelty_percentile — 90.0 — `MEMEINTEL_LEARNING_NOVELTY_PERCENTILE`
- retrain_every_n — 200 — `MEMEINTEL_LEARNING_RETRAIN_EVERY_N`
- model_half_life_days — 30.0 — `MEMEINTEL_LEARNING_MODEL_HALF_LIFE_DAYS`
- min_train_samples — 50 — `MEMEINTEL_LEARNING_MIN_TRAIN_SAMPLES`
- accuracy_window — 200 — `MEMEINTEL_LEARNING_ACCURACY_WINDOW`
- min_ensemble_confidence — 0.0 — `MEMEINTEL_LEARNING_MIN_ENSEMBLE_CONFIDENCE`
- drift_accuracy_floor — 0.40 — `MEMEINTEL_LEARNING_DRIFT_ACCURACY_FLOOR`
- drift_min_samples — 30 — `MEMEINTEL_LEARNING_DRIFT_MIN_SAMPLES`
- scaler_refit_every_n — 500 — `MEMEINTEL_LEARNING_SCALER_REFIT_EVERY_N`
- fast_snapshot_seconds — 60 — `MEMEINTEL_LEARNING_FAST_SNAPSHOT_SECONDS`
- fast_window_minutes — 60 — `MEMEINTEL_LEARNING_FAST_WINDOW_MINUTES`
- slow_snapshot_minutes — 60 — `MEMEINTEL_LEARNING_SLOW_SNAPSHOT_MINUTES`
- capture_until_hours — 24.0 — `MEMEINTEL_LEARNING_CAPTURE_UNTIL_HOURS`
- min_snapshots_for_confidence — 3 — `MEMEINTEL_LEARNING_MIN_SNAPSHOTS_FOR_CONFIDENCE`
- cold_start_samples — 100 — `MEMEINTEL_LEARNING_COLD_START_SAMPLES`
- veto_enabled — False (**DORMANT** — mind-layer P(rug) blocking HIGH alerts) — `MEMEINTEL_LEARNING_VETO_ENABLED`
- veto_min_p_rug — 0.85 — `MEMEINTEL_LEARNING_VETO_MIN_P_RUG`
- veto_min_accuracy — 0.70 — `MEMEINTEL_LEARNING_VETO_MIN_ACCURACY`
- veto_min_samples — 10 — `MEMEINTEL_LEARNING_VETO_MIN_SAMPLES`
- veto_metrics_ttl_seconds — 1800.0 — `MEMEINTEL_LEARNING_VETO_METRICS_TTL_SECONDS`
- state_dir — `"learning_state"` — `MEMEINTEL_LEARNING_STATE_DIR`

### `lightgbm` — `LightGBMSettings` (group `LIGHTGBM`) — Section 4, only relevant if `learning.enabled`
- full_retrain_rounds — 120 — `MEMEINTEL_LIGHTGBM_FULL_RETRAIN_ROUNDS`
- warm_start_rounds — 30 — `MEMEINTEL_LIGHTGBM_WARM_START_ROUNDS`
- learning_rate — 0.05 — `MEMEINTEL_LIGHTGBM_LEARNING_RATE`
- num_leaves — 31 — `MEMEINTEL_LIGHTGBM_NUM_LEAVES`
- min_child_samples — 5 — `MEMEINTEL_LIGHTGBM_MIN_CHILD_SAMPLES`
- balanced_class_weights — True — `MEMEINTEL_LIGHTGBM_BALANCED_CLASS_WEIGHTS`

### `telegram_commands` — `TelegramCommandSettings` (group `TELEGRAM_COMMANDS`) — Project 2, **DORMANT by default**
- enabled — False — `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED` (needs `telegram_bot_token`+`telegram_chat_id` too)
- poll_timeout_seconds — 25.0 — `MEMEINTEL_TELEGRAM_COMMANDS_POLL_TIMEOUT_SECONDS`
- idle_delay_seconds — 2.0 — `MEMEINTEL_TELEGRAM_COMMANDS_IDLE_DELAY_SECONDS`
- error_backoff_max_seconds — 60.0 — `MEMEINTEL_TELEGRAM_COMMANDS_ERROR_BACKOFF_MAX_SECONDS`

### `execution` — `ExecutionSettings` (group `EXECUTION`) — Project 6, HARD SAFETY MODEL, **DORMANT (dry-run) by default**
- buy_button_enabled — False — `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED` (shows Buy/Dump buttons on Telegram alerts)
- live_enabled — False — `MEMEINTEL_EXECUTION_LIVE_ENABLED` (actually signs+sends; else dry-run executor which signs nothing)
- max_buy_sol — 0.15 — `MEMEINTEL_EXECUTION_MAX_BUY_SOL` (0 = no ceiling; wallet balance is the only automatic limit)
- slippage_bps — 500 — `MEMEINTEL_EXECUTION_SLIPPAGE_BPS`
- priority_fee_max_lamports — 1,000,000 — `MEMEINTEL_EXECUTION_PRIORITY_FEE_MAX_LAMPORTS` (0.001 SOL)
- confirm_timeout_seconds — 45.0 — `MEMEINTEL_EXECUTION_CONFIRM_TIMEOUT_SECONDS`
- preflight_retries — 2 — `MEMEINTEL_EXECUTION_PREFLIGHT_RETRIES` (0 = never retry; ambiguous errors never auto-retried)
- buy_button_percents — `"20,50,75,100"` — `MEMEINTEL_EXECUTION_BUY_BUTTON_PERCENTS` (percent-of-live-balance buy buttons)
- (secret, not in this group) trading key: `MEMEINTEL_EXECUTION_PRIVATE_KEY` — empty = no live executor
- (secret) dedicated Helius key for trading: `MEMEINTEL_EXECUTION_HELIUS_API_KEY` — empty = shares `MEMEINTEL_HELIUS_API_KEY`

### `rug_thresholds` — `RugThresholds` (group `RUG_THRESHOLDS`) — Section 5a, "does this signal fire?"
- min_lp_locked_percent — 50.0 — `MEMEINTEL_RUG_THRESHOLDS_MIN_LP_LOCKED_PERCENT`
- top_holder_percent_max — 30.0 — `MEMEINTEL_RUG_THRESHOLDS_TOP_HOLDER_PERCENT_MAX`
- top10_holder_percent_max — 70.0 — `MEMEINTEL_RUG_THRESHOLDS_TOP10_HOLDER_PERCENT_MAX`
- liquidity_drop_percent — 50.0 — `MEMEINTEL_RUG_THRESHOLDS_LIQUIDITY_DROP_PERCENT`
- liquidity_removal_usd — 1000.0 — `MEMEINTEL_RUG_THRESHOLDS_LIQUIDITY_REMOVAL_USD`
- sell_tax_max_percent — 20.0 — `MEMEINTEL_RUG_THRESHOLDS_SELL_TAX_MAX_PERCENT`
- dev_dump_usd — 1000.0 — `MEMEINTEL_RUG_THRESHOLDS_DEV_DUMP_USD`
- fake_volume_per_holder_usd — 5000.0 — `MEMEINTEL_RUG_THRESHOLDS_FAKE_VOLUME_PER_HOLDER_USD`
- fake_volume_min_volume_usd — 1000.0 — `MEMEINTEL_RUG_THRESHOLDS_FAKE_VOLUME_MIN_VOLUME_USD`

### `rug_signal_weights` — `RugSignalWeights` (group `RUG_SIGNAL_WEIGHTS`) — Section 5a, additive points (0-100, clamped), NOT normalized
- liquidity_unlocked — 20.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_LIQUIDITY_UNLOCKED`
- mint_authority_active — 20.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_MINT_AUTHORITY_ACTIVE`
- freeze_authority_active — 15.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_FREEZE_AUTHORITY_ACTIVE`
- top_holder_concentration — 15.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_TOP_HOLDER_CONCENTRATION`
- liquidity_removed — 30.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_LIQUIDITY_REMOVED`
- unsellable — 30.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_UNSELLABLE`
- high_sell_tax — 15.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_HIGH_SELL_TAX`
- dev_wallet_dumping — 20.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_DEV_WALLET_DUMPING`
- fake_volume — 10.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_FAKE_VOLUME`
- deployer_blacklisted — 25.0 — `MEMEINTEL_RUG_SIGNAL_WEIGHTS_DEPLOYER_BLACKLISTED`

### Top-level `Settings` scalars (no group prefix — direct `MEMEINTEL_<NAME>`)
- log_level — `"INFO"` — `MEMEINTEL_LOG_LEVEL`
- log_dir — `"logs"` — `MEMEINTEL_LOG_DIR`
- helius_api_key — `""` (empty = off) — `MEMEINTEL_HELIUS_API_KEY`
- birdeye_api_key — `""` — `MEMEINTEL_BIRDEYE_API_KEY`
- lunarcrush_api_key — `""` — `MEMEINTEL_LUNARCRUSH_API_KEY`
- anthropic_api_key — `""` — `MEMEINTEL_ANTHROPIC_API_KEY`
- jupiter_api_key — `""` — `MEMEINTEL_JUPITER_API_KEY`
- coingecko_api_key — `""` (optional demo key, raises CoinGecko rate limit) — `MEMEINTEL_COINGECKO_API_KEY`
- telegram_bot_token — `""` — `MEMEINTEL_TELEGRAM_BOT_TOKEN`
- telegram_chat_id — `""` — `MEMEINTEL_TELEGRAM_CHAT_ID`
- discord_webhook_url — `""` — `MEMEINTEL_DISCORD_WEBHOOK_URL`
- trading_private_key — `""` (empty = dry-run only) — `MEMEINTEL_EXECUTION_PRIVATE_KEY`
- trading_helius_api_key — `""` (empty = shares main Helius key) — `MEMEINTEL_EXECUTION_HELIUS_API_KEY`

All secrets are read only from the environment/`.env` (never hardcoded — Rule 16); every `*_api_key`/`*_token`/`*_webhook_url`/`_private_key` field defaults to `""`, and an empty value is each layer's own "stay off" convention, checked at each `build_*` call site in `__main__.py`.

### Key Mechanisms
- **TTL cache** (`core/cache.py`): `TTLCache` is an `asyncio.Lock`-guarded `OrderedDict` keyed cache; `get()` treats an expired entry as a miss and evicts it lazily; `set()` evicts the least-recently-used entry once `len > max_entries` (bounded memory, Rule 7); `get_or_set()` is the standard "compute on miss" pattern collectors use. Sized per-collector from `settings.http.cache_max_entries` / `cache_ttl_seconds` (`cache.py:26-79`).
- **Token-bucket rate limiting** (`core/rate_limiter.py:15-63`): classic bucket, `capacity=burst` (default 5 via `.per_minute()`), tokens refill continuously as `elapsed * rate_per_second`; `acquire()` sleeps exactly the deficit time under the lock rather than polling. Every provider client is built with `RateLimiter.per_minute(settings.providers.<name>_requests_per_minute)` in `__main__.py`'s `build_*` functions, and Helius traffic from wallet-intel + live trading deliberately **share one limiter instance** (`__main__.py:990-998`, `216-249`) because two independent per-client limiters against the same real Helius account budget could together exceed it (this caused a live 429 incident on 2026-07-11 per the code comments).
- **Retry with backoff+jitter** (`core/retry.py:20-61`): retries only `TransientCollectorError` (network/timeout/5xx/429) up to `attempts` (default from `settings.http.retry_attempts=4`), delay = `min(max_delay, base_delay * 2**(attempt-1))` jittered ±25%, and if the exception carries `retry_after_seconds` (a `RateLimitedError`'s 429 `Retry-After` hint) the delay is raised to at least that value — never retries sooner than a provider explicitly asked.
- **Provider failover pool** (`core/provider_pool.py:43-191`): `ProviderPool.call_with_provider()` walks providers in priority order, skipping any on cooldown; a `TransientCollectorError` counts toward `failure_threshold` (default 3) consecutive failures before a `cooldown_seconds` (default 60) cooldown; a plain `CollectorError` (e.g. 404/unindexed token) does **not** count toward health — it's item-specific, not provider health (explicit bug-hunt fix noted in comments); an unexpected non-`CollectorError` exception is logged at ERROR with traceback (treated as a provider bug) and still counts toward cooldown; `AllProvidersFailedError` is raised with all per-provider causes when the whole pool is exhausted, which callers must treat as "unknown", never a fabricated zero (Rule 8). `health()` returns a per-provider snapshot for dashboards/logs.
- **Error hierarchy** (`core/errors.py`): `MemeIntelError` root; `ConfigurationError` (bad settings); `CollectorError(status_code)` → `TransientCollectorError` → `RateLimitedError(retry_after_seconds)`; `AllProvidersFailedError(method, causes)`; `InsufficientDataError` (raised instead of fabricating a value, Rule 8). This hierarchy is the vocabulary every retry/pool/analyzer decision above is keyed on.
- **Logging** (`core/logging_setup.py`): single idempotent `setup_logging()` configures the `meme_intelligence` root logger with a console `StreamHandler` plus a `RotatingFileHandler` (5MB x 3 backups) under `log_dir/meme_intelligence.log`; format `"%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"`; `get_logger(name)` returns `meme_intelligence.<name>` child loggers used throughout the codebase.
- **Config validation** (`config/settings.py`): every "sub-weight" dataclass group (`ScoringWeights`, `SecuritySubWeights`, `CommunitySubWeights`, `OnChainSubWeights`, `FoundationSubWeights`, `TokenSubWeights`, `TradeScoreWeights`, `RiskSubWeights`, `MomentumSubWeights`, `OpportunityWeights`, `NarrativeSubWeights`, `ViralSubWeights`, `SmartMoneySubWeights`) is checked to sum to 1.0 ± 1e-6 via `_check_weight_sum()`, with an explicit `math.isfinite()` guard because NaN silently passes a naive `abs(x-1.0)>tol` check. Most scalar-threshold groups validate positivity/finiteness and cross-field orderings (e.g. `extreme_tax_percent >= max_tax_percent`, `strong_candidate_overall >= overall`, ceilings ≥ floors for the opportunity size gates). `RugSignalWeights` is deliberately **not** normalized — it sums to an additive 0-100 rug score.
- **Env override + type coercion** (`config/settings.py:1680-1711`): `_load_group()` iterates a dataclass's fields, looks up `MEMEINTEL_<GROUP>_<FIELD>` and coerces via `_convert()` — bool is checked before int (bool is an int subclass) with a strict `true/false/yes/no/1/0/on/off` vocabulary that **raises** on an unrecognized string rather than silently defaulting to False (explicit fix noted in comments); otherwise int/float/str follow the field's default type. `load_dotenv()` loads `.env` KEY=VALUE pairs but never overrides a real env var already set. `get_settings()`/`reset_settings()` provide a process-wide singleton with a test-reset hook.
- **CLI → services wiring** (`__main__.py`): `_shared_collector_kwargs()` centralizes cache/timeout/retry kwargs from `settings.http` for every `build_*` factory; `build_market_service()`/`build_wallet_service()`/`build_executor()` are the composition points where `ProviderPool`-backed failover services and gated (key-present-only) services get assembled per command.

### Telegram / CLI Surface
All subcommands are dispatched from `main()`/`_run()` in `__main__.py` via `python -m meme_intelligence <command> [flags]`. (Telegram bot commands like `/status`, `/why`, `/check` live in `alerts/telegram_commands.py`, outside this file's scope, but `monitor` is what starts that listener when `telegram_commands.enabled` is set — see `_cmd_monitor`.)

- **`search <query> [--limit N=5]`** — search DexScreener pairs by name/symbol/address; prints top-N by liquidity.
- **`token <address> [--chain CHAIN] [--limit N=5]`** — fetch DexScreener pairs for a token contract address, optional chain filter.
- **`discover [--network NET ...] [--limit N=10] [--show-rejected]`** — Layer-1 discovery scan for newly launched pools via GeckoTerminal; `--network` repeatable, default `["solana"]`.
- **`security <address> --chain CHAIN`** — one-off GoPlus security assessment; exit code 2 if destructive.
- **`scan [--network NET ...] [--top N=5]`** — Layer 1 discovery → Layer 2 security screen → partial Layer 3 on-chain (derived, no extra calls) for the top N candidates.
- **`plan <address> [--ai] [--ai-mode {standard,...}] [--chain CHAIN] [--regime {bull,neutral,bear,unknown}]`** — full research pass + trade plan for one token; `--ai` requires `MEMEINTEL_ANTHROPIC_API_KEY`; exit code 2 if destructive.
- **`report <address> [--ai] [--ai-mode ...] [--chain CHAIN] [--regime ...]`** — canonical intelligence report (Part 12), persisted to the DB (`storage.record_snapshot`, wallet sightings).
- **`quick <address> [--chain CHAIN] [--regime ...]`** — Level 1 fast-scan compact card; red flags never skipped even in fast mode; exit code 2 if destructive.
- **`compare <addresses...> [--chain CHAIN] [--regime ...]`** — compare 2+ tokens (prefix an address with `chain:` to override per-token); needs ≥2 analyzable tokens.
- **`backtest [--refresh]`** — prediction accuracy / self-improvement report (Part 24); `--refresh` measures due outcome windows via live price fetch; feeds the learning service's instant-resolution hook if `learning.enabled`.
- **`alerts [--limit N=20] [--test]`** — alert history + performance analysis; `--test` sends a synthetic HIGH-priority alert through every configured sink (console always; Telegram/Discord if secrets present) to verify delivery.
- **`watchlist [--refresh] [--include-archived] [--top] [--limit N=10]`** — show tracked tokens; `--refresh` re-scores via the pipeline; `--top` ranks by Part 28 §5 opportunity score instead of showing the raw list.
- **`wallets <address> [--chain solana]`** — smart-money & whale intelligence for one token; requires `MEMEINTEL_HELIUS_API_KEY` and/or `MEMEINTEL_BIRDEYE_API_KEY`; records wallet sightings to storage.
- **`daily [--network NET ...]`** — runs the full daily research routine (Part 11) end-to-end and prints its report.
- **`monitor [--network NET ...] [--cycles N] [--interval SECONDS] [--regime {bull,neutral,bear,unknown}] [--pumpfun] [--learn] [--boosts]`** — the 24/7 continuous scanning loop (production mode on the droplet); Ctrl-C stops gracefully. `--pumpfun` flips `pumpfun.enable_in_monitor` on for this run; `--learn` flips `learning.enable_in_monitor` on; `--boosts` flips `boost_watcher.enabled` on. Wires wallet/social/AI/learning services in only if their `enable_in_monitor` flag AND required API key are both present (missing key with flag set prints a "Note:" instead of silently no-op'ing, Rule 13); also starts the Telegram command listener and/or boost watcher if their settings are enabled.
- **`mind evaluate <address> [--chain solana]`** — analog + LightGBM + rug verdict for one token via the self-learning mind layer; exit code 2 if rug risk score ≥ 70.
- **`mind metrics`** — self-evaluation metrics (accuracy, directional hit-rate, rug precision/recall/F1, Brier score, analog memory size, classifier readiness) over resolved predictions.

Global note: every command builds `Settings` via `get_settings()` then calls `setup_logging(settings.log_level, settings.log_dir)` once in `_run()` before dispatch.

### Dormant Features Here
All of the following are off/inert by default and require an explicit env var, CLI flag, or API key to activate — none change current behavior until touched (Rule 18):

1. **Pump.fun launch discovery** — `pumpfun.enable_in_monitor=False`. Enable: set `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR=true` or pass `monitor --pumpfun`.
2. **DexScreener boost radar** — `boost_watcher.enabled=False`. Enable: `MEMEINTEL_BOOST_WATCHER_ENABLED=true` or `monitor --boosts`.
3. **Wallet/smart-money intelligence in the continuous scanner** — `wallet.enable_in_monitor=False` (still usable on-demand via `wallets`/`plan`/`report`/`/check`, which bypass the gate). Enable: `MEMEINTEL_WALLET_ENABLE_IN_MONITOR=true` plus `MEMEINTEL_HELIUS_API_KEY` and/or `MEMEINTEL_BIRDEYE_API_KEY`. Even then, gated by `credit_gate_min_security_score=50`, `credit_gate_max_lookups_per_day=200`, `credit_gate_cooldown_minutes=60`.
4. **Momentum security floor for buy-side alerts** — `alerts.momentum_min_security_score=0.0` (off). Per code comments, this is deliberately kept aligned with #3's credit gate: set `MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE=50` together with the wallet gate when enabling wallet screening, so a sub-floor coin can't reach a momentum alert while being exempt from wallet screening.
5. **X/Twitter social intelligence (LunarCrush)** — `social.enable_in_monitor=False`; whole layer off without `MEMEINTEL_LUNARCRUSH_API_KEY`. Enable per the README's `enable-x-community-tracking.sh` (referenced in settings.py comments) plus a paid LunarCrush key.
6. **Full per-token AI judging in the monitor** — `ai.enable_in_monitor=False` (the lighter `ai.verify_opportunities=True` already runs one AI check only on tokens clearing every review gate). Enable: `MEMEINTEL_AI_ENABLE_IN_MONITOR=true` plus `MEMEINTEL_ANTHROPIC_API_KEY`.
7. **Self-learning "mind" layer** — `learning.enabled=False` and `learning.enable_in_monitor=False` entirely off by default (built but opt-in, Rule 11 "extra work"). Enable training/backtest-time labeling: `MEMEINTEL_LEARNING_ENABLED=true`. Enable live feed during scanning: `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR=true` or `monitor --learn`.
8. **Mind-layer rug veto on HIGH alerts** — `learning.veto_enabled=False` even with learning enabled; the code comments say the intended workflow is to read the `/mind metrics` report card with the operator before flipping `MEMEINTEL_LEARNING_VETO_ENABLED=true`, since authority (`veto_min_accuracy=0.70` precision over `veto_min_samples=10` graded rug calls) must be earned, not assumed (Rule 8).
9. **Two-way Telegram command listener** — `telegram_commands.enabled=False`. Enable: `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED=true` plus `MEMEINTEL_TELEGRAM_BOT_TOKEN`/`MEMEINTEL_TELEGRAM_CHAT_ID` (same secrets the alert sink uses — only one consumer may call `getUpdates` per bot token).
10. **Trading Buy/Dump buttons on alerts** — `execution.buy_button_enabled=False`. Enable: `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=true`.
11. **Live trade execution (signing real transactions)** — `execution.live_enabled=False`, and even if true, requires `MEMEINTEL_EXECUTION_PRIVATE_KEY` (a dedicated low-balance wallet) — without both, every buy/dump silently routes to the dry-run executor which signs nothing. `MEMEINTEL_EXECUTION_HELIUS_API_KEY` optionally gives trading its own Helius account to avoid budget contention with the scanner.
12. **Operator "don't alert me on old/huge coins" gates** — `alerts.opportunity_min_liquidity_usd`, `opportunity_min_market_cap_usd`, `opportunity_max_liquidity_usd`, `opportunity_max_market_cap_usd` all default to `0.0` (off); `opportunity_max_age_hours` is the one exception and is **ACTIVE by default at 1.0 hour** per an explicit 2026-07-17 operator request (buy-side alerts suppressed for pools older than 1h; protective/warning alerts still fire; unknown age never trips it).
13. **Copycat-symbol veto** — `alerts.copycat_veto_enabled=True` is actually ON by default (listed here for completeness since it's easy to assume off; ratio 10x liquidity, min established-token liquidity $100k).
14. **Live Jupiter round-trip sell-test (honeypot probe)** — setting-level `liquidity_probe.enabled=True`, but functionally inert without `MEMEINTEL_JUPITER_API_KEY` (`build_jupiter()` returns `None` on an empty key, so no client is ever constructed regardless of the `enabled` flag).
15. **Alert delivery to Telegram/Discord** — inert until `MEMEINTEL_TELEGRAM_BOT_TOKEN`+`MEMEINTEL_TELEGRAM_CHAT_ID` or `MEMEINTEL_DISCORD_WEBHOOK_URL` are set; console sink is always active.
16. **CoinGecko elevated rate limit** — `MEMEINTEL_COINGECKO_API_KEY` optional; empty = free-tier ~10/min limit used.

### Integration Points
**Reads from:**
- Environment variables / `.env` file (Rule 16) — the only source of secrets and overrides; `Settings.from_env()` is the sole ingestion point.
- Nothing else upstream — this subsystem is the root of the dependency graph; every other package (`analyzers`, `collectors`, `scanners`, `alerts`, `workflow`, `trading`, `learning`, `ai`, `database`) imports `Settings`/`get_settings()` from `config.settings` and the primitives from `core.*`.

**Writes to / drives:**
- **Collectors** (`collectors/market_data.py`, `security_data.py`, `wallet_data.py`, `social_data.py`, `jupiter_data.py`, `pumpfun.py`): every client is constructed in `__main__.py`'s `build_*` functions with a `RateLimiter.per_minute(settings.providers.<x>_requests_per_minute)`, a `TTLCache(settings.http.cache_max_entries, settings.http.cache_ttl_seconds)`, and retry/timeout kwargs from `settings.http` — i.e. `core/rate_limiter.py`, `core/cache.py`, and `core/retry.py` (used internally by the base collector class, not shown here) are the literal HTTP behavior of every provider integration in the codebase.
- **`MarketDataService`/`WalletDataService`** (`collectors/market_service.py`, `wallet_data.py`): built via `build_market_service()`/`build_wallet_service()` on top of `core.provider_pool.ProviderPool`, using `settings.providers.failure_threshold`/`cooldown_seconds` — this is how Rule 9 ("multi-source, degrade gracefully") is actually implemented for DexScreener+GeckoTerminal and Helius+Birdeye.
- **Analyzers** (`analyzers/security_analyzer.py`, `onchain_analyzer.py`, `wallet_intelligence.py`, etc.): consume `settings.security`, `settings.security_weights`, `settings.onchain`, `settings.wallet`, `settings.smart_money_weights` and raise/consume `core.errors.InsufficientDataError` when data is missing rather than fabricating scores (Rule 8).
- **Scanners** (`scanners/discovery.py`): consumes `settings.discovery` for candidacy gates.
- **Workflow** (`workflow/controller.py` `ContinuousScanner`, `daily_routine.py`, `watchlist_review.py`, `boost_watcher.py`): consume `settings.workflow`, `settings.intervals`, `settings.pumpfun`, `settings.boost_watcher`, `settings.learning` — this is the `monitor`/`daily` command's actual engine, assembled in `_cmd_monitor`/`_cmd_daily`.
- **Alerts** (`alerts/notification_engine.py`, `sinks.py`, `telegram_commands.py`): consume `settings.alert_engine`, `settings.alert_delivery`, `settings.execution` (buy-button wiring), and the Telegram/Discord secrets; `build_sinks()` in `__main__.py` is the composition point.
- **Trading** (`trading/execution.py`, `trade_planner.py`, `solana_rpc.py`): `build_executor()` reads `settings.execution` and the trading secrets to decide dry-run vs. live, and shares (or separates) the Helius rate limiter with wallet intelligence per `trading_helius_api_key`.
- **Learning** (`learning/service.py`): consumes `settings.learning`, `settings.lightgbm`, `settings.rug_thresholds`, `settings.rug_signal_weights`; lazily imported by `build_learning_service()` so numpy/faiss/lightgbm only load when actually used.
- **AI reasoning** (`ai/reasoning.py`, `report_generator.py`, `comparison.py`): consumes `settings.ai` and `settings.anthropic_api_key`; `build_judgment_service()` returns `None` when no key is set, which every caller treats as "AI layer off" per Rule 9.
- **Database** (`database/storage.py`): consumes `settings.database.path` (SQLite file path); every persisting command opens `Storage(settings.database.path)` as a context manager.
- **Logging**: every module across the codebase calls `core.logging_setup.get_logger(__name__-ish)` to log under the shared `meme_intelligence` namespace/handlers configured once at CLI startup.
---

## 9. Dormant Kits — Master Table

Every subsystem above notes its own dormant features inline; this table consolidates all of
them in one place because three separate docs (`STATUS.md`, `OPERATOR.md`, `ROADMAP.md`) had
drifted into disagreeing with each other about which of these are still "paused" vs. already
rebuilt as an enable-on-request kit. Two columns matter differently: **Code Default** is a
fact about the repository (always true, verify with the grep shown); **Operator's Droplet**
is a fact about one running process that changes whenever he edits `.env` — the entries below
marked "confirmed" were read directly off his own `/status`/`/mind` output during this session
(2026-07-20) and can go stale the moment he changes something. When in doubt, ask him to run
`/status` rather than trust this table's right-hand column.

| Feature | Code Default | Operator's Droplet (2026-07-20) | Enable Command |
|---|---|---|---|
| Wallet intelligence (smart-money) in the continuous scanner | OFF (`MEMEINTEL_WALLET_ENABLE_IN_MONITOR=false`) | **OFF, confirmed** (`/status` → `wallet intel off`) | `bash deploy/enable-wallet-tracking.sh <PAID_HELIUS_KEY>` — needs a paid Helius plan; the free tier exhausted in ~3 days when this ran unconditionally on 2026-07-11 |
| Social intelligence (LunarCrush / X-adjacent) | OFF (`MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR=false`) | Not explicitly surfaced by `/status`; presumed OFF (never requested enabled) | `bash deploy/enable-x-community-tracking.sh` — needs a paid LunarCrush key (`MEMEINTEL_LUNARCRUSH_API_KEY`) |
| DexScreener boost radar | OFF (`MEMEINTEL_BOOST_WATCHER_ENABLED=false`) | Not confirmed this session — has a removal/restoration history (see §10) | `MEMEINTEL_BOOST_WATCHER_ENABLED=true` in `.env` + restart |
| Pump.fun launch funnel (PumpPortal stream + frontend rechecks) | OFF (`MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR=false`) | **ON, confirmed** (`/status` → `pump.fun ON`) | `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR=true` in `.env` + restart |
| Jupiter live round-trip liquidity probe (buy/sell honeypot test) | ON (`MEMEINTEL_LIQUIDITY_PROBE_ENABLED=true`) | **ON, confirmed** (`/status` → `jupiter probe ON`) | Already on; needs `MEMEINTEL_JUPITER_API_KEY` present |
| Learning / mind layer feeding the continuous scanner | OFF (`MEMEINTEL_LEARNING_ENABLE_IN_MONITOR=false`) | **ON, confirmed** (`/status` → `learning ON`) | `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR=true` |
| Mind-layer P(rug) alert veto | OFF (`MEMEINTEL_LEARNING_VETO_ENABLED=false`) | **ON, confirmed EARNED authority** (`/mind` → `p(rug) veto: ON \| authority: EARNED`, rug precision 0.96 over 25,527+ graded calls) | `MEMEINTEL_LEARNING_VETO_ENABLED=true` — only meaningful once `veto_min_samples`/`veto_min_accuracy` are cleared; abstains silently until earned |
| AI judging *every* analyzed token (`ai.enable_in_monitor`) | OFF (expensive) | Not confirmed — `/status`'s `AI ON` reflects `verify_opportunities` (below), not this flag | `MEMEINTEL_AI_ENABLE_IN_MONITOR=true` — burns a paid Claude call per analyzed candidate, not just gate-passers |
| AI verification gate on HIGH-tier candidates (`ai.verify_opportunities`) | **ON** by default | **ON, confirmed** (`/status` → `AI ON`) | Already on; disable with `MEMEINTEL_AI_VERIFY_OPPORTUNITIES=false` |
| Telegram two-way command listener | OFF by code default (`TelegramCommandSettings.enabled=false`) | **ON** (obviously — the operator drives the whole bot through it) | Requires `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED=true` + bot token + chat id |
| Live trading (buy/dump buttons actually signing/sending) | OFF by code default (dry-run) — `buy_button_enabled=false`, `live_enabled=false` | **LIVE, confirmed** (`/status` → `trading LIVE`) — armed 2026-07-11, round-trip validated | `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=true` + `MEMEINTEL_EXECUTION_LIVE_ENABLED=true` + a funded dedicated wallet's private key — **never do this on a shared/main wallet** |

## 10. History of Reversals

A reference document is incomplete if it only describes the current state — several "why
doesn't X exist" or "why does Y behave differently than I remember" traps in this codebase
trace back to a deliberate reversal, not a bug. Full detail and exact rationale for each is
in [DECISIONS_LOG.md](./DECISIONS_LOG.md) at the line ranges cited; this is the index.

1. **The 2026-07-17 full snapshot restore** (`DECISIONS_LOG.md` ~L1406-1424). The operator
   ordered the entire tree rolled back to a 2026-07-13 state ("I want it exactly how it was.
   Take everything we did after the snapshot and delete it"), verified byte-for-byte against
   the pre-restore commit before applying. This deleted three days of work in one motion:
   buy-side ceilings + freshness-gate v1, the smart-wallet data clock + reputation connector,
   a watchlist staleness door, an earlier wallet credit-gate design, a threading fix, a
   streaming rebuild, a training-set cap, **a scanner-stall watchdog process, and two "mind
   upgrade" iterations.** If you're looking for a watchdog process and can't find one, this
   is why — it existed for a few days and was removed here, not something that silently
   broke.
2. **The buy-side freshness gate itself was rebuilt on top of that restore**, then tuned live
   against real feedback: built at 1 hour (`DECISIONS_LOG.md` ~L1426-1439, "only send me coins
   less than 1 hour old"), widened to 3 hours the same day when bot/bundler launches dominated
   the sub-1h population (~L1440-1460), then reverted back to 1 hour the next day at the
   operator's direction. **The 1-hour window is his deliberate, current choice** — not a
   regression from any later deploy (this was mis-diagnosed as a regression once already this
   session before the git history was checked directly; see `DECISIONS_LOG.md`'s 2026-07-20
   "zero coins" entry).
3. **Buy-button UX changed from fixed SOL presets to percent-of-balance** (`DECISIONS_LOG.md`
   ~L1511-1552, commit `1e210d5`) — the Telegram buy buttons now offer 20/50/75/100% of
   current wallet balance rather than fixed SOL amounts, because fixed presets stopped making
   sense as the wallet balance itself changed.
4. **The per-trade SOL ceiling was removed entirely** (`DECISIONS_LOG.md` ~L1553-1574, commit
   `a50afe0`) — `execution.max_buy_sol` now defaults to `0.15` in code but the operator's
   `.env` sets it to `0` (= no ceiling) at his explicit request; a doc that states "the
   ceiling is 0.15 SOL" without checking `.env` is describing the code default, not his actual
   running limit.
5. **Fast-mover buyability fix** (`DECISIONS_LOG.md` ~L1575-1620, commit `5827216`) — the live
   executor now retries a preflight-rejected buy with a fresh quote before giving up, and
   shortens the error text shown on failure; fixes coins that were unbuyable simply because
   their price moved between quote and execution.
6. **Wallet intelligence: removed, then rebuilt as a dormant kit** — paused 2026-07-11 when
   the shared Helius account's free-tier credits were exhausted in days; rebuilt 2026-07-20
   behind a credit gate and an explicit enable script (`DECISIONS_LOG.md` ~L1621-1660) rather
   than re-enabled unconditionally. Older docs that say "re-enable only once the bot is
   profitable" describe a *retired* re-enable criterion — the actual current gate is "the
   operator supplies a paid Helius key and runs the script," full stop.
7. **The DexScreener boost radar was removed by operator decision on 2026-07-14, then came
   back to life as a side effect of the 07-17 snapshot restore** (the restore target predates
   the removal). It is currently off by default and its live state on the droplet was not
   re-confirmed this session — check `/status` before assuming either way.
8. **Social/LunarCrush intelligence built from scratch 2026-07-20** (`DECISIONS_LOG.md`
   ~L1661-1769) as a dormant, credit-gated kit mirroring the wallet-intelligence shape —
   this is new, not a restoration, and is easy to miss because `ROADMAP.md`/`OPERATOR.md`/
   `SETUP.md` still describe it (pre-rewrite) as "parked"/"deferred."
9. **2026-07-20, same day, three fix rounds**: (a) 8 confirmed findings from an external code
   review — data-honesty, provider-failover resilience, typing (`DECISIONS_LOG.md`
   ~L1770-1825); (b) the mind-layer "memory frozen" cross-process reload bug — the analog
   index appeared stuck at a fixed coin count for hours because the always-on monitor loaded
   it once at boot and a separate grading process kept growing the on-disk copy underneath it,
   fixed with an mtime-keyed reload and a split dirty-flag so the fix can't re-clobber itself
   (`DECISIONS_LOG.md` ~L1826-1875); (c) the `/check` Telegram card gained `STATUS: DEAD` /
   `STATUS: RUGGED` / `STATUS: DUMPED` banners because the momentum "entry zone" field was
   answering `early` on already-dead coins with no lifecycle signal at all
   (`DECISIONS_LOG.md` ~L1876-1920). All three are display/data-integrity fixes with zero
   change to what triggers a buy-side alert.

**Takeaway for whoever reads this next**: this project has been tuned live against real
operator feedback far more than it has been "designed once." A setting's default in
`settings.py` is a starting point the operator has very often already overridden — always
check `.env` (or ask for a `/status`/`/mind` screenshot) before assuming code defaults
describe what's actually running.
