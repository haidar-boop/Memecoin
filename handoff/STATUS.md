# Build Status — Parts 1 through 18

**274 tests passing.** ~9,450 lines of source, ~4,150 lines of tests.
13 commits on `claude/large-prompt-review-l49wp1`.

Legend: ✅ built and tested · 🟡 built partially (documented gap) ·
⏳ blocked on something outside the code (API key, data source that
doesn't exist yet)

---

## Part 1 — AI Role, Mission & Operating Rules → ✅

The classification/scoring framework, config system, and logging.

- `core/enums.py` — `Classification` (Elite/Strong Candidate/Watchlist/
  Speculative/Avoid), `RiskTier` (Acceptable Uncertainty / Serious
  Warning / Destructive — the permanent risk taxonomy from Part 31's
  Consistency Lock), `ConfidenceLevel`, `ScanLayer`
- `config/settings.py` — every tunable value, env-overridable
  (`MEMEINTEL_*`), validated at construction (weights must sum to 1.0,
  bands must descend, etc.)
- `core/models.py` — `CategoryScores`, `compute_weighted_score()` (missing
  categories excluded and reported as `coverage`, never assumed zero),
  `classify()`
- `core/logging_setup.py` — console + rotating file logs

## Part 2 — Real-Time Scanning Infrastructure & Data Architecture → ✅

- `core/rate_limiter.py` — token bucket, `per_minute()` constructor
- `core/cache.py` — async TTL cache, LRU eviction
- `core/retry.py` — exponential backoff + jitter, transient-vs-permanent
  error split
- `core/provider_pool.py` — multi-provider failover with cooldowns
- `collectors/base.py` — `BaseCollector`: every collector gets rate
  limiting, caching, retry for free
- `collectors/market_data.py` — `DexScreenerClient` (first live provider)

## Part 3 — Meme Coin Discovery Engine → ✅

- `collectors/market_data.py` — `GeckoTerminalClient.get_new_pools()`
- `scanners/discovery.py` — `DiscoveryEngine`: hard filters (min
  liquidity, freshness window), dedupe (keep deepest pool per token),
  Discovery Score (freshness/liquidity/volume/activity, 25 pts each),
  rejected pools returned with reasons (feeds future learning)

## Part 4 — Rug Pull Detection & Security Analysis System → ✅

- `collectors/security_data.py` — `GoPlusClient` (EVM + Solana, very
  different response shapes, normalized to one model)
- `core/models.py` — `SecurityProfile` (~25 normalized security facts)
- `analyzers/security_analyzer.py` — `SecurityAnalyzer`: graded risk
  taxonomy (Acceptable Uncertainty deducts lightly, Serious Warning
  deducts heavily, Destructive forces score to 0), sub-scores weighted
  per Part 33 (contract 25/liquidity 25/distribution 20/developer 15/
  manipulation 15)

## Part 5 — Foundation & Community Intelligence System → 🟡

- `core/models.py` — `CommunityProfile`
- `analyzers/community_analyzer.py` — `CommunityAnalyzer`: 5 categories
  (engagement/growth/loyalty/creativity/dev relationship, 20% each), fake
  community detection (bot-majority followings force `ARTIFICIAL` rating
  + score 0 — a red-flag override)
- `analyzers/foundation_analyzer.py` — `FoundationAnalyzer`: combines
  qualitative judgment slots (meme strength, narrative, brand, dev comms,
  long-term) with community quality
- **Gap:** no live social-data collector (X/Twitter/Telegram/Discord/
  Reddit APIs). `CommunityProfile` and the qualitative foundation slots
  are ready to receive data — nothing is currently feeding them. This is
  the single largest open gap in the system. See DECISIONS_LOG.md.

## Part 6 — On-Chain Intelligence & Wallet Behavior Analysis → ✅

- `core/models.py` — `OnChainProfile`
- `analyzers/onchain_analyzer.py` — `OnChainAnalyzer`: 6 categories
  (holder health/smart money/whale behavior/developer activity/volume
  quality/token flow), phase classification (Accumulation/Expansion/
  Distribution/Unclear), wash-trading detection (trades-per-wallet),
  volume-vs-holder-base sanity checks
- `derive_onchain_profile()` builds a partial profile from data already
  collected (market + security) — zero extra API calls
- **As of Part 17, smart money / whale / token flow are now populated
  with real data** via `enrich_onchain_profile()` for Solana tokens.

## Part 7 — Token Evaluation Framework & Market Structure Analysis → ✅

- `analyzers/token_analyzer.py` — `TokenAnalyzer`: market-cap staging
  (Early/Growth/Mature), FDV dilution overhang detection, liquidity-to-
  mcap and volume-to-mcap ratio grading (excessive churn flagged),
  supply-concentration checks, conservative valuation classification
  (Undervalued requires early stage + real demand + real depth together)
- `competition_score()` — percentile helper against competitor pairs

## Part 8 — Trading Strategy & Execution Framework → ✅

- `trading/trade_planner.py` — `TradePlanner`: generates **plans, never
  orders**. Trade score, setup classification (Early Discovery/
  Confirmation/Trend Continuation/Watch Only), conviction with position-
  size *guidance ceilings*, entry checklist (unknowns stay UNKNOWN — never
  a pass), required confirmations, invalidation conditions, FOMO-
  prevention questions embedded in every plan

## Part 9 — Risk Management System & Capital Protection Framework → ✅

- `analyzers/risk_analyzer.py` — `RiskAnalyzer` (token-level risk score,
  **higher = riskier**, opposite direction from quality scores; 5
  components: security 25/market 20/community 15/token 20/execution 20).
  A profile that's <50% verifiable can never be rated better than HIGH
  risk (unknown ≠ safe), even though unknown components are excluded from
  the number itself.
- `PortfolioRiskManager` — exposure limits (position count, single-
  position cap, chain/narrative concentration, total exposure), drawdown
  posture (Normal/Reduced/Defensive)
- `emergency_flags()` — critical/high exit conditions

## Part 10 — AI Scoring Algorithm & Decision Engine → ✅ (the keystone)

- `analyzers/scoring_engine.py` — `ScoringEngine`: combines every
  category via the **Part 31-locked weights** (Foundation/Security/
  Community/Blockchain/Momentum/Narrative 15% each, Timing 10%),
  renormalized over available data with `coverage` reported
- **Red-flag overrides** force Avoid regardless of score: destructive
  security, fake community, extreme risk
- **6-question decision tree** with a full recorded trace (contract
  safety, liquidity, community authenticity, on-chain health, narrative
  potential, risk/reward) — rejects or caps classification, unknowns
  reduce confidence without failing
- `derive_timing_score()` — heuristic from pair freshness × market-cap
  stage × market phase

## Part 11 — Daily Operating Routine & Research Workflow → ✅

- `collectors/market_data.py` — `CoinGeckoClient` (BTC/ETH/SOL majors)
- `workflow/daily_routine.py` — `DailyRoutine`: market-environment check
  → risk-on/neutral/risk-off regime → discovery → deep analysis of top
  candidates → watchlist intake/tiering → bounded review of existing
  entries → end-of-day report

## Part 12 — Final AI Output Format & Report Template → ✅

- `ai/report_generator.py` — `build_report()`: the canonical 14-section
  intelligence report — executive summary, **evidence-derived** bull/bear
  cases (every bullet traced to a real finding, never invented), all
  category sections, scoring table, decision trace, trade plan (only for
  qualifying tokens), final verdict (pass/fail, main reason, biggest
  risk/opportunity, what would change the opinion)

## Part 13 — Advanced AI Automation Blueprint → ✅

- `workflow/pipeline.py` — `ResearchPipeline`: the per-token analysis
  chain extracted into **one shared implementation** used everywhere
  (CLI, daily routine, continuous scanner) so scoring is always identical
- `workflow/controller.py` — `ContinuousScanner`: the 24/7 loop.
  Per-cycle error isolation with exponential backoff that resets on
  success (one provider outage never kills the scanner), graceful
  SIGINT/SIGTERM shutdown, session dedupe
- `alerts/notification_engine.py` — `AutomationRules` (IF/THEN gates with
  honest partial-data handling: all gates verified+passed → HIGH, any
  gate unverified → MEDIUM "provisional"), `NotificationEngine` with
  per-token/type cooldown

## Part 14 — Advanced Trading Intelligence Layer & Momentum → ✅

- `core/models.py` — `DexPair` extended with 1h/6h windows (both
  providers already returned this data; zero new API calls)
- `analyzers/momentum_analyzer.py` — `MomentumAnalyzer`: 4 lenses (price/
  volume/social/on-chain, 25% each). Acceleration beats level (1h rate vs
  24h baseline), consistent multi-window trends beat single-window pumps,
  fake-momentum detection (built on suspect volume quality). Entry zones
  (Early/Confirmation/Late) and preferred action.

## Part 15 — Real-Time Scanner Configuration & Anti-Throttling → ✅

- `collectors/market_service.py` — `MarketDataService`: wires the
  previously-unused `ProviderPool` into a real failover pool across
  DexScreener ⇄ GeckoTerminal (chain-alias-mapped to share one interface)
- **Multi-source event verification**: opportunity/momentum alerts are
  cross-checked against a second source before dispatch — agreement
  annotates the alert, disagreement downgrades it with the stated reason,
  unavailability marks it unverified (never silently confirmed)
- Watchlist recheck cadence (secondary/slower monitoring speed) in the
  continuous scanner

## Part 16 — AI Prompt Execution Rules & Operating Instructions → ✅

- `ai/prompts.py` — `ANALYST_SYSTEM_PROMPT` (the canonical system prompt
  for the future LLM reasoning layer) + `check_language()` (banned-phrase
  guard — "will pump", "guaranteed", "risk-free" etc. — enforced by tests
  against every generated report)
- New CLI commands: `quick` (Level 1 fast scan, red flags never
  skipped), `compare` (category table + explicit ranking, overrides sink
  to the bottom regardless of score), `watchlist` (view/refresh)
- `workflow/watchlist_review.py` — the review logic extracted into one
  shared implementation (previously duplicated between the daily routine
  and the CLI)
- Alert events now carry "watch next" monitoring guidance

## Part 17 — Advanced Smart Money Tracking & Whale Intelligence → ✅ (Solana)

- `collectors/wallet_data.py` — `HeliusClient` (top holders read from
  chain state — largest token accounts resolved to owner wallets, burn
  addresses excluded — + parsed token transfers), `BirdeyeClient` (token
  overview + recent trades, USD computed from trade legs),
  `WalletDataService` (combines both, degrades per-source on failure)
- `analyzers/wallet_intelligence.py` — `WalletIntelligenceAnalyzer`:
  Smart Money Confidence Score (5 lenses × 20%; `historical_success`
  honestly reports "no data" until Part 24 builds real track records —
  size/earliness never earn the smart-money label per spec doctrine),
  whale classification (Long-Term/Trading/Risk/Custodial), accumulation
  verdict (Healthy/Mixed/Artificial/Unknown — scripted same-size trades
  and dominant-buyer detection), exchange flow as stated lower bounds,
  entry-timing analysis (buying the dip vs. chasing the pump)
- `wallet_reputation()` — the Part 17 §2 reputation formula (25/20/20/
  20/15), returns `None` without history (no fabricated neutrality)
- `database/storage.py` — `wallet_sightings` table: every observed wallet
  action recorded as raw material for future reputation building
- 3 new alert types: `smart_money_accumulation`, `whale_exit`,
  `insider_risk`
- New CLI command: `wallets`
- **API keys live and verified**: `MEMEINTEL_HELIUS_API_KEY`,
  `MEMEINTEL_BIRDEYE_API_KEY` (see SETUP.md)
- **Scope: Solana only.** EVM wallet intelligence (would need Alchemy or
  similar) is not built.

## Part 18 — Advanced Rug Detection & Scam Prevention Engine → ✅

- `analyzers/security_monitor.py` — continuous contract-change
  monitoring: every analysis persists ~23 security facts; every
  re-analysis diffs against the last-known baseline. Severity taxonomy:
  honeypot/un-renounced-ownership/mint-authority-appearing/LP-unlock/
  counterfeit-flags → **CRITICAL**; new blacklist/pause/freeze/proxy
  powers, tax hikes, sharp concentration jumps → **HIGH**; concentration
  creep, holder drain → **MEDIUM**. Unknown transitions never alarm
  (first sightings are baselines, not changes); facts flickering to
  unknown keep their last known value.
- `database/storage.py` — `security_facts` table (per-token baseline)
- Wired into the continuous scanner (bypasses market cross-verification —
  rests on contract facts) and the daily routine (leads the risk list,
  journaled)
- `analyzers/wallet_intelligence.py` — promotion-and-exit pattern
  detector (price up >50%/24h while top holders distribute) — the
  on-chain half of Part 18 §9; the social half needs social collectors
- **Deferred with reason:** deep developer launch-history indexing (past
  projects beyond GoPlus's same-creator-honeypot count) needs a data
  source neither current key provides.

---

---

## New since the 2026-07-08 snapshot: live Jupiter round-trip sell test (Project 1)

**2026-07-10.** This is not a renumbered spec part — it's the first of a
separate 5-project roadmap layered on top of the existing Part 4/18/33
security stack. GoPlus's contract analysis is *static* (what the code says
the contract could do); this adds a *live* signal: actually ask Jupiter's
swap router for a quote to buy the token, then a quote to sell it straight
back, the same "can you actually sell it?" test a trader would do by hand.

- `collectors/jupiter_data.py` (new) — `JupiterClient.check_round_trip_liquidity()`:
  buys with a configurable SOL amount (default 0.3 SOL, ≈$50 at time of
  writing), then immediately quotes selling the received tokens back to
  SOL. Solana only. Requires a free Jupiter Developer Platform API key —
  Jupiter deprecated its old fully-keyless "Lite" tier; the current
  $0/month "Free" plan still requires signup (rate-limited to 1 req/s, no
  monthly cap). Get one at https://developers.jup.ag/portal.
- `collectors/base.py` — `_get_json()` gained an optional
  `error_status_as_json` parameter so a collector can treat specific
  non-200 statuses as a parseable JSON payload instead of an error (used
  here because Jupiter reports "no route" as a 400/404/422 with a JSON
  body, not a 200). Fully backward compatible — every other collector
  passes nothing and is unaffected.
- `core/models.py` — new `LiquidityProbeResult` (collector output shape)
  and three new `SecurityProfile` fields: `live_buy_route_found`,
  `live_sell_route_found`, `live_round_trip_loss_percent`.
- **Three-state semantics, not a boolean** (Rule 8 — unknown ≠ unsafe): a
  missing buy route is never treated as suspicious — Jupiter simply may
  not have indexed a very new but legitimate pool yet. Only two things
  are dangerous: (a) a buy route exists but no sell route does (a
  confirmed "can buy, can't sell" rug, full stop, regardless of how clean
  the static contract looks), and (b) a round trip that completes but
  loses a catastrophic fraction of value (a live-detected hidden tax /
  soft rug GoPlus's static tax fields might miss).
- `analyzers/security_analyzer.py` — `_assess_contract()` flags a missing
  sell route as **destructive** (forces score to 0, same override class as
  GoPlus's `is_honeypot`/`cannot_sell_all`), an extreme round-trip loss
  (≥90% by default) as destructive, and an elevated-but-not-extreme loss
  (>50% by default) as a serious warning. This is deliberately independent
  evidence from GoPlus (Rule 9 — multi-source), not a restatement of it.
- `analyzers/security_monitor.py` — the three new fields are persisted as
  baseline facts (`FACT_FIELDS`); a sell route disappearing between scans
  is a new CRITICAL change (exactly how a rug begins), and a round-trip
  loss jumping ≥20 points is a new HIGH change. A buy route disappearing
  alone is recorded but intentionally not wired into any alert.
- `config/settings.py` — new `LiquidityProbeSettings` group
  (`MEMEINTEL_LIQUIDITY_PROBE_*`: `enabled`, `probe_sol_amount`,
  `slippage_bps`), two new `SecurityThresholds` fields
  (`max_round_trip_loss_percent`, `extreme_round_trip_loss_percent`), and
  `jupiter_api_key` / `providers.jupiter_base_url` /
  `providers.jupiter_requests_per_minute` alongside the existing
  Helius/Birdeye settings.
- `workflow/pipeline.py` — `ResearchPipeline` takes an optional
  `jupiter_client`; when present, enabled, and the pair is on Solana, the
  probe runs after GoPlus data is fetched but before security scoring, so
  the merged fields participate in scoring. A `CollectorError`/`ValueError`
  from the probe degrades gracefully (logged at INFO, analysis continues)
  — it can never crash or block the pipeline.
- **Wired into every command that goes through `ResearchPipeline`/
  `ContinuousScanner`/`DailyRoutine`**: `plan`, `report`, `quick` (via the
  shared `_gather_assessments` helper), `compare`, `watchlist --refresh`,
  `daily`, `monitor`. **Explicitly NOT wired into `security` or `scan`**
  (Part 2/Part 4's standalone screening commands bypass the shared
  pipeline by design and were left untouched, per design decision 6 of
  this change).
- `__main__.py` — new `build_jupiter()` factory, mirroring
  `build_wallet_service()`'s "returns `None` when no key is configured"
  pattern exactly.
- Tests: `tests/test_jupiter_data.py` (new), plus additions to
  `test_security_analyzer.py`, `test_security_monitor.py`,
  `test_settings.py`, and a new minimal `tests/test_pipeline.py`. Full
  suite: 298 passing (was 274; +24 net across new/extended files).

## What's NOT built yet

Everything in `next_steps/` — **Parts 19 through 33** (see
`next_steps/INDEX.md`). Some of these (20, 21, 22, 23, 30, 31, 32, 32.5)
substantially overlap with what's already built, since they're
architecture/consolidation parts written before the earlier build parts
existed in code — read them anyway, since they sometimes add specific
requirements (e.g. Part 31's Framework Consistency Lock is the reason the
scoring weights are what they are today) or resolve ambiguity between
earlier parts.

## Known cross-cutting gaps (affect multiple parts)

1. **No social-data collector.** Community/narrative scoring runs on
   partial/no data everywhere. This blocks full realization of Parts 5,
   19, and the "community" gate in every alert rule. Needs a decision on
   budget (see DECISIONS_LOG.md) — X API is $200/mo; cheaper aggregators
   exist.
2. **No outcome-tracking / backtesting loop yet (Part 24).** Every
   snapshot is being recorded (`snapshots`, `wallet_sightings`,
   `security_facts` tables) specifically so that once Part 24 is built,
   historical predictions can be joined against actual outcomes. The data
   pipeline is ready; the join/scoring logic isn't written.
3. **EVM wallet intelligence** (Alchemy or similar) is not built — Part
   17 is Solana-only.
4. **No Telegram/Discord alert sinks yet** — `NotificationEngine`
   supports pluggable sinks; only `ConsoleSink` exists. Needs a Telegram
   bot token (see SETUP.md — not created yet).
5. **No Anthropic/LLM integration yet.** All qualitative judgment slots
   (meme strength, narrative scoring, bull/bear prose enrichment) are
   wired to accept AI-layer input but currently run on deterministic
   heuristics or report "no data." Needs an Anthropic API key (not
   created yet) plus the actual prompt-calling code (Parts 22 §4, 23).
6. **No dashboard/web UI** — CLI only, per the phased roadmap.
