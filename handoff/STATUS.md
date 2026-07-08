# Build Status — Parts 1 through 19, plus 23 and 29

**349 tests passing.** ~11,100 lines of source, ~5,200 lines of tests.
Parts 1–18 were built on `claude/large-prompt-review-l49wp1`; Parts 19
and 23 on `claude/handoff-folder-review-fuu9dq`.

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
- `collectors/market_data.py::CoinGeckoClient.get_community_profile()`
  — free community data by contract address (telegram members, sentiment
  votes, reddit activity), wired through the pipeline so the community
  category, the fake-community red flag, and decision-tree Q3 run live.
- **Gap (why still 🟡):** Twitter engagement, Discord, bot detection, and
  growth rates aren't tracked by the free source; very new tokens aren't
  listed on CoinGecko yet. Upgrade path: LunarCrush once the system
  proves itself (user decision — see DECISIONS_LOG.md).

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

## Part 19 — Narrative Intelligence & Viral Potential Prediction Engine → 🟡

- `analyzers/narrative_analyzer.py` — two Part 19 rubrics: the **viral
  score** (§3: memorability / shareability / emotional impact / cultural
  timing / community participation, 5×20%) and the **narrative
  intelligence score** (§11: meme strength / cultural timing / viral
  potential / community creativity / long-term strength, 5×20%). The
  viral score feeds the intelligence score's `viral_potential` component;
  the intelligence score fills the master framework's 15% `narrative`
  category — the last empty slot in the Part 31 locked weighting.
- `NarrativeInputs` — validated 0-100 qualitative judgment slots
  (`FoundationInputs` pattern; the AI layer fills them later), plus
  category (§2), life-cycle stage (§7), the three §10 risk flags, and
  §9 viral catalysts (description + probability/impact grading).
- Evidence-driven pieces: participation/creativity cross-fill from the
  community engine's creativity sub-score; artificial-community verdict
  zeroes participation (§5 organic-vs-artificial); stage timing signals
  and distribution-risk findings (§7); sentiment classification (§6);
  narrative risk Low/Medium/High/Unknown derived from the risk flags with
  late-stage escalation (§10/§12); evidence-derived strengths/weaknesses
  and the full §12 report format in `summary()`.
- `core/enums.py` — `NarrativeCategory`, `NarrativeStage`,
  `NarrativeRating` (Excellent/Strong/Average/Weak on the house
  85/70/50 ladder), `NarrativeRisk`, `SentimentLabel`, `CatalystLevel`
- `config/settings.py` — `NarrativeThresholds` (sentiment bands),
  `NarrativeSubWeights`, `ViralSubWeights` (env groups
  `MEMEINTEL_NARRATIVE*`, `MEMEINTEL_VIRAL_WEIGHTS_*`)
- Wired through `ResearchPipeline.analyze_pair(narrative_inputs=...)`
  (optional; without inputs the narrative category reports "no data"
  exactly as before), `PipelineResult.narrative`, the report generator
  (section + bull/bear bullets + opinion-changers), and the `report`/
  `plan` CLI output. Snapshots already persist the narrative category
  score via the `category_scores` JSON — no schema change.
- **Gap (why 🟡):** nothing feeds the judgment slots automatically yet —
  they await the AI layer (Part 23) and social collectors (same gap as
  Part 5). §5 social-trend monitoring (mentions, search interest) and
  §8/§9 automated competition/catalyst detection need those sources;
  until then coverage/confidence report the missing evidence honestly.

---

## Part 23 — AI Agent Integration Blueprint & Intelligence Pipeline → 🟡

- `ai/reasoning.py` — the LLM reasoning layer, live against the Anthropic
  API (key verified). `AIJudgmentService.judge()` makes one structured-
  output request per token (§4 prompt structure: role = the tested
  `ANALYST_SYSTEM_PROMPT`, objective, §3 structured snapshot, rules
  including the §9 bias warnings, JSON-schema output format) and returns
  a validated `AIJudgment`: `FoundationInputs` + `NarrativeInputs`
  (nullable slots — null over guessing, Rule 8), bull/bear evidence
  bullets, and the §6 confidence score with its reason.
- `build_intelligence_snapshot()` — condenses a `PipelineResult` into the
  §3 token/market/security/community/wallets format; the model never sees
  raw API payloads and is told exactly which sources are missing.
- Validation is layered: API-level JSON schema → range/enum checks →
  Part 16 banned-language guard → configurable confidence floor. Any
  failure discards the judgment; the pipeline continues on deterministic
  evidence (Rules 6/9).
- `ResearchPipeline` runs the AI pass only after the deterministic chain,
  skips destructive-security tokens entirely (Rule 10), fills whichever
  foundation/narrative slots are empty (explicit analyst inputs win), and
  re-scores through the Part 31 locked weighting. Off in the continuous
  scanner unless `MEMEINTEL_AI_ENABLE_IN_MONITOR=true`.
- §10 research modes (`fast_scan` / `standard` / `deep_investigation`)
  select prompt depth; CLI: `report --ai [--ai-mode ...]`, `plan --ai`.
- `config/settings.py::AISettings` — model (default `claude-opus-4-8`),
  max tokens, effort, rate limit (Rule 11), timeout, confidence floor;
  key via `MEMEINTEL_ANTHROPIC_API_KEY` only (Rule 16).
- **Gaps (why 🟡):** §7 memory / §8 feedback loop ride on the snapshot
  tables and activate as learning in Part 24; community judgment slots
  stay thin until the social collectors exist (the model sees the gap and
  lowers confidence — observed live).

---

## Part 29 — Real-Time Alert Intelligence & Notification System → 🟡

- `alerts/sinks.py` — `TelegramSink` (bot API) and `DiscordSink`
  (webhooks), built on the shared collector machinery (rate limit /
  retry / timeout for free); delivery failures are logged and swallowed —
  a dead messenger never stops the scanner (Rule 7). §8 channel
  organization: every alert type maps to one of the five spec categories
  (discoveries / smart_money / security / momentum / reports) with
  optional per-category routing (`MEMEINTEL_ALERT_DELIVERY_*_ROUTES`);
  external sinks deliver MEDIUM+ by default (§1 noise doctrine),
  configurable.
- `format_alert()` — the full §7 message format: §2 priority header,
  token block, time detected, event summary, why it matters, evidence,
  current scores, risk assessment (derived from the priority grading),
  recommended monitoring. `AlertEvent` gained `why_it_matters` and
  `detected_at` (stamped at dispatch).
- §10 ranking — `rank_alert()`: impact 40% / confidence 30% / urgency
  20% / novelty 10%; dispatch sends the most decision-relevant alert
  first. Component scales are documented implementation choices.
- §§11-12 — `alerts` table (every delivered alert, with score-at-alert);
  `Storage.alert_history()` and `alert_performance()` (score drift after
  each alert, per type — the measurement layer Part 24's learning loop
  builds on). Wired into the continuous scanner.
- Community rules went live with the collector: the opportunity gate now
  reads the real community score (full HIGH qualification is finally
  reachable), and a confirmed-fake community fires a `community_fake`
  HIGH alert.
- CLI: `alerts` (history + performance), `alerts --test` (synthetic
  delivery check through every configured sink).
- §§4-6 (filtering, confirmation, cooldown) were already built in Parts
  13/15; §9's daily summary is Part 11's `DailyReport`.
- **Gap (why 🟡):** no Telegram bot token / Discord webhook configured
  yet — sinks are built, tested against mocks, and activate the moment
  `MEMEINTEL_TELEGRAM_BOT_TOKEN` + `MEMEINTEL_TELEGRAM_CHAT_ID` (or
  `MEMEINTEL_DISCORD_WEBHOOK_URL`) land in `.env`. Verify with
  `python -m meme_intelligence alerts --test`.

---

## What's NOT built yet

Everything in `next_steps/` — **Parts 20-22, 24-28, and 30-33** (see
`next_steps/INDEX.md`). Some of these (20, 21, 22, 23, 30, 31, 32, 32.5)
substantially overlap with what's already built, since they're
architecture/consolidation parts written before the earlier build parts
existed in code — read them anyway, since they sometimes add specific
requirements (e.g. Part 31's Framework Consistency Lock is the reason the
scoring weights are what they are today) or resolve ambiguity between
earlier parts.

## Known cross-cutting gaps (affect multiple parts)

1. **Partial social-data collector.** The free CoinGecko community
   collector (see DECISIONS_LOG.md — "cheap aggregator" decision) now
   feeds telegram size, sentiment votes, and reddit activity into the
   community/narrative engines and the AI snapshot. Twitter engagement,
   Discord, bot detection, and growth rates remain untracked until a
   paid aggregator (LunarCrush) is added — planned once the system
   proves itself. Very new tokens aren't listed on CoinGecko yet and
   report "no data" honestly.
2. **No outcome-tracking / backtesting loop yet (Part 24).** Every
   snapshot is being recorded (`snapshots`, `wallet_sightings`,
   `security_facts` tables) specifically so that once Part 24 is built,
   historical predictions can be joined against actual outcomes. The data
   pipeline is ready; the join/scoring logic isn't written.
3. **EVM wallet intelligence** (Alchemy or similar) is not built — Part
   17 is Solana-only.
4. ~~No Telegram/Discord alert sinks yet~~ **Resolved — Part 29 built.**
   Sinks activate when the bot token / webhook URL lands in `.env`
   (still pending on the user's side).
5. ~~No Anthropic/LLM integration yet.~~ **Resolved — Part 23 built and
   live.** The AI reasoning layer fills the qualitative judgment slots
   via `report --ai` / `plan --ai`; only the social-data half of those
   judgments (gap #1) remains thin.
6. **No dashboard/web UI** — CLI only, per the phased roadmap.
