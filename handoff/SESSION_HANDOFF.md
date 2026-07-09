# Meme Coin Intelligence System — Complete Handoff

> **Purpose of this document.** A single, detailed briefing for anyone (human
> or AI) picking up this project in a fresh session. It covers what the system
> is, everything it does, the 21 Project Rules that govern how it must be
> engineered, the full architecture module-by-module, everything done in the
> most recent working session, how it is deployed and operated, the complete
> CLI and configuration reference, and what is intentionally left unbuilt.

**Status at handoff:** Parts 1–33 of the specification are **built or
verified-satisfied**. **457 automated tests pass** (`python -m pytest tests/ -q`).
~13,900 lines of source, ~7,300 lines of tests. The system runs 24/7 on a
DigitalOcean droplet, streaming discoveries and sending Telegram alerts.

---

## 1. What this system is

A **research and intelligence platform for meme coins** — primarily Solana,
extensible to EVM chains. It continuously discovers newly launched tokens,
analyzes each across eight dimensions (security, community, on-chain,
foundation, momentum, narrative, token structure, timing), scores them, and
sends prioritized alerts. It **never executes trades and never holds funds** —
it is a decision-support system; a human makes every final call. Position
sizing, when mentioned, is advisory guidance only.

The system is CLI-driven (`python -m meme_intelligence <command>`) and runs a
24/7 background scanner (`monitor`) that delivers alerts to Telegram/Discord.

---

## 2. The 21 Project Rules (verbatim — mandatory, every session)

These come from `PROJECT_RULES.md` at the repo root and are the authoritative
engineering contract. They are never optional.

**Rule 1 — Follow the Specification.** Always use the project specification as
the primary source of requirements. Do not invent major features that conflict
with the spec. If a requirement is unclear, make the most reasonable
implementation and explain your assumptions.

**Rule 2 — Build in Small Steps.** Do not build the whole app in one response.
Implement one logical section at a time. After each: verify functionality,
explain what was built, list remaining work, wait for the next request.

**Rule 3 — Never Break Working Code.** Before changing existing code:
understand what it does, preserve existing functionality where possible,
improve without removing working features unless necessary. If a breaking
change is required, explain why.

**Rule 4 — Modular Design.** Separate responsibilities (scanner, security,
community, on-chain, scoring, dashboard, alerts, database, configuration,
logging, testing). Avoid putting unrelated functionality in one large file.

**Rule 5 — Prioritize Readability.** Write code another developer can easily
understand: clear names, helpful comments, logical structure, consistent
formatting. Avoid unnecessary complexity.

**Rule 6 — Production Quality.** Assume real-world use. Avoid placeholders.
Handle errors, missing data, API failures, invalid responses, timeouts.

**Rule 7 — Build for Reliability.** The scanner runs continuously. Design for
automatic recovery after crashes, error logging, retry logic, graceful
shutdown, stable long-running execution.

**Rule 8 — Data Before Assumptions.** Base conclusions on evidence. Do not
create scores from unsupported assumptions. If information is missing: reduce
confidence, explain uncertainty. Do not fabricate data.

**Rule 9 — Multi-Source Intelligence.** Never rely on one source where
practical. Combine blockchain, market, community, security, social signals. If
one provider is unavailable, continue with the remaining data.

**Rule 10 — Efficient Data Collection.** Avoid unnecessary requests. Prefer
WebSockets, event-driven monitoring, cached data, intelligent refresh
schedules. Only run expensive analysis when a token passes initial filtering.

**Rule 11 — Avoid API Abuse.** Respect provider limits: request queues, rate
limiting, retry delays, exponential backoff, caching. The objective is stable
operation, not maximum request frequency.

**Rule 12 — Keep Performance High.** Optimize for fast scanning, efficient
memory, efficient DB queries, low latency. Avoid unnecessary processing.

**Rule 13 — Logging.** Log every important action (scanner started, API
unavailable, security analysis completed, alert generated, database updated,
errors, warnings).

**Rule 14 — Testing.** For each new feature verify it works, doesn't break
existing functionality, and handles obvious edge cases. Include automated
tests when practical.

**Rule 15 — Documentation.** Document new modules, APIs, configuration,
database changes, major architectural decisions. Keep documentation updated.

**Rule 16 — Security.** Never expose API keys, private keys, secrets,
passwords. Store secrets via environment variables or a secure secrets manager.

**Rule 17 — Configuration.** Avoid hardcoding. Put configurable settings in
config files or environment variables (refresh intervals, thresholds, API
keys, database settings, alert settings).

**Rule 18 — Backward Compatibility.** When improving a module, prefer
extending over rewriting. Only replace large sections when there is a clear
benefit.

**Rule 19 — Explain Major Decisions.** For important architectural decisions,
briefly explain why the approach was chosen, its benefits, and trade-offs.

**Rule 20 — Ask Before Major Changes.** If the spec is unclear or two valid
implementations exist, do not guess silently. Present the options with their
advantages and disadvantages before proceeding.

**Rule 21 — Development Mindset.** The objective is a reliable, maintainable,
extensible platform — not just code. Prioritize accuracy, stability,
scalability, clear architecture, practical performance. Avoid unnecessary
complexity; simple reliable solutions beat clever fragile ones.

---

## 3. Where everything lives

- **Specification (source of truth):** `handoff/FULL_PROJECT_HANDOFF.md` — the
  21 rules plus the complete original spec, Parts 1–32.5, verbatim. Individual
  unbuilt/verified parts are split under `handoff/next_steps/` (see
  `handoff/next_steps/INDEX.md` for the build order and verification-pass
  results).
- **Implementation:** `meme_intelligence/` (modular packages below).
- **Tests:** `tests/` — run with `python -m pytest tests/ -q`. All must stay
  green (Rule 3, Rule 14). 457 tests across 39 files at handoff.
- **Build status & prior decisions:** `handoff/STATUS.md` and
  `handoff/DECISIONS_LOG.md`. Update these when you build a part or resolve an
  ambiguity (Rule 15, Rule 19).
- **Deployment kit:** `deploy/` (systemd unit, setup script, cron installer,
  DB backup, DigitalOcean README).
- **Session instructions:** `CLAUDE.md` (repo root) — points every session at
  the rules and conventions.
- **Config template:** `.env.example` — every tunable env var with defaults.

---

## 4. Architecture — module by module

Packages under `meme_intelligence/`:

### `core/` — shared primitives
- `models.py` — normalized dataclasses every layer speaks: `TokenIdentity`,
  `DexPair`, `SecurityProfile`, `CommunityProfile`, `OnChainProfile`,
  `WalletIntelData`, `CategoryScores`, `PumpFunLaunch`, `PumpFunCoinState`,
  etc. Missing data is always `None`, never a fabricated zero (Rule 8).
- `enums.py` — `Classification` (Elite/Strong/Watchlist/Speculative/Avoid),
  `RiskTier`, `WatchlistTier`, `AlertPriority`, `EntryZone`, `MarketRegime`,
  `NarrativeCategory/Stage`, `CatalystLevel`, etc.
- `errors.py` — exception hierarchy. `MemeIntelError` base; `CollectorError`
  (carries `status_code`), `TransientCollectorError`, `RateLimitedError`,
  `AllProvidersFailedError`, `InsufficientDataError`, `ConfigurationError`.
- `cache.py` — async-safe TTL/LRU cache (Rule 10).
- `rate_limiter.py` — token-bucket limiter, `RateLimiter.per_minute(...)` (Rule 11).
- `retry.py` — `retry_async` with exponential backoff + jitter on transient
  errors only (Rule 7).
- `provider_pool.py` — `ProviderPool`: priority-ordered failover across
  providers with consecutive-failure tracking and cooldowns; `call_with_provider`
  returns which provider answered (Rule 9).
- `logging_setup.py` — `get_logger(name)` convention (Rule 13).

### `config/` — configuration (Rule 17)
- `settings.py` — one frozen-dataclass tree, `Settings.from_env(env)`. Every
  tunable maps to `MEMEINTEL_<GROUP>_<FIELD>`. Validation in `__post_init__`
  raises `ConfigurationError` (NaN/negative/out-of-range all rejected). Secrets
  are root scalars defaulting to `""` (empty = that layer stays off).

### `collectors/` — external data, normalized (Rules 2, 9, 32)
- `base.py` — `BaseCollector`: rate limit + TTL cache + retry + timeout +
  secret redaction for every HTTP call. `_get_json` short-circuits on cache.
- `market_data.py` — `DexScreenerClient`, `GeckoTerminalClient`,
  `CoinGeckoClient` (majors + free community data). Normalize to `DexPair`.
- `market_service.py` — `MarketDataService`: failover pool + `cross_check_liquidity`
  (agreement raises confidence, disagreement drops it, unverifiable = None).
- `security_data.py` — `GoPlusClient` → `SecurityProfile` (honeypot,
  permissions, taxes, LP lock, distribution).
- `wallet_data.py` — `HeliusClient` (chain-truth top holders + transfers),
  `BirdeyeClient` (holder counts, trades), `WalletDataService` (combines them;
  Solana-first, metered → on-demand only).
- `pumpfun.py` — `PumpPortalClient` (free keyless WebSocket, token-creation +
  migration events, single-connection reconnect-with-backoff), and
  `PumpFunFrontendClient` (per-coin traction rechecks from the unofficial
  frontend API).

### `scanners/` — discovery
- `discovery.py` — `DiscoveryEngine`: hard filters (min liquidity, freshness
  window) + Discovery Score (freshness/liquidity/volume/activity, 25 pts each);
  dedupe keeping deepest pool; rejected pools returned with reasons.
- `launch_monitor.py` — `LaunchMonitor`: the Part 32.5 §7 funnel front. Basic
  filtering on launch events (launchpad, anonymity, insider dev-buy), bounded
  traction rechecks (per-token cadence, per-cycle budget, TTL), and §8
  promotion gates (market cap, SOL-cap growth, replies, recent trading;
  graduation = fast path). Every gate needs data to pass (Rule 8).

### `analyzers/` — scoring engines (Rule 4)
- `common.py` — `SubScore`/`Finding` building blocks: observe facts, contribute
  signals, apply deductions, report coverage/confidence honestly.
- `security_analyzer.py` — Part 4/18/33 security score; destructive findings
  pin the score to 0 (security-first doctrine). Sub-weights per Part 33 §11:
  Contract 25 / Liquidity 20 / Developer 20 / Distribution 20 / Manipulation 15.
- `security_monitor.py` — Part 18 §10 contract-change detection (CRITICAL/HIGH/
  MEDIUM diffs vs the stored baseline).
- `community_analyzer.py` — Part 5 community/engagement + fake-community override.
- `onchain_analyzer.py` — Part 6 holder health, volume quality, token flow.
- `foundation_analyzer.py` — Part 5 §12 foundation blend (meme/narrative/brand/
  community-quality/dev-comm/long-term).
- `momentum_analyzer.py` — Part 14/26 momentum (price/volume/social/on-chain,
  acceleration not levels, false-momentum detection, entry zones).
- `narrative_analyzer.py` — Part 19 narrative intelligence + viral potential.
- `token_analyzer.py` — Part 7 token structure (valuation, supply, dilution).
- `risk_analyzer.py` — Part 9/25 risk score + `emergency_flags`.
- `scoring_engine.py` — the **master score**: Part 31-locked weights
  (Foundation/Security/Community/Blockchain/Momentum/Narrative 15% each, Timing
  10%), coverage-honest, red-flag overrides, classification bands.
- `opportunity_ranker.py` — **NEW (Part 28 §5):** watchlist opportunity rank
  (Growth 30 / Momentum 25 / Foundation 20 / Risk 15 / Timing 10) — a *second,
  separate* upside-tilted axis that orders which tracked tokens deserve
  attention. Never touches the master score.
- `wallet_intelligence.py` — Part 17 smart-money/whale analysis (Solana).

### `ai/` — AI reasoning layer (Part 23)
- `prompts.py` — analyst system prompt + `check_language` banned-phrase guard
  (now negation-aware, so cautionary disclaimers aren't flagged as hype).
- `reasoning.py` — `AIJudgmentService` (Anthropic), `build_judgment_service`,
  structured-output parsing validated twice. Fills qualitative slots
  (foundation/narrative, bull/bear case) only when it has evidence.
- `report_generator.py` — the full human-readable intelligence report.
- `comparison.py` — `compare` command ranking.

### `alerts/` — notification (Part 29)
- `notification_engine.py` — `AutomationRules` (turns results into alert events
  via the human-review gates) + `NotificationEngine` (per-token+type+priority
  cooldown, ranking, per-sink isolation). Alert types include
  `high_priority_opportunity`, `strong_candidate` (NEW), `early_opportunity`,
  `momentum`, `smart_money_accumulation`, `whale_exit`, `insider_risk`,
  `risk_warning`, `emergency_review`, `security_change`, `community_fake`,
  `score_drop_review`, `token_death` (NEW).
- `sinks.py` — `ConsoleSink`, `TelegramSink`, `DiscordSink` (Section 7 format,
  Section 8 channel routing, credential redaction, min-priority filtering).

### `database/` — persistence
- `storage.py` — SQLite (WAL mode for safe concurrent monitor + cron access).
  Tables: `tokens`, `snapshots` (with market facts + `opportunity_rank`),
  `watchlist`, `journal`, `security_facts`, `wallet_sightings`, `alerts`,
  `outcomes`. In-place `_migrate()` upgrades old DBs (Rule 18). Includes
  `top_opportunities()` (Part 28 ranking query).

### `analytics/` — Part 24
- `backtesting.py` — outcome windows (1h/24h/7d/30d), prediction grading,
  §4 metrics, signal performance, weight experiments (report-only under the
  Part 31 lock), alert-outcome labeling.

### `trading/` — advisory only (never executes)
- `trade_planner.py` — Part 8 conviction levels + advisory position-size
  guidance ("NOT an order").

### `workflow/` — orchestration
- `pipeline.py` — `ResearchPipeline.analyze_pair`: the one shared analysis chain
  (security → on-chain → wallet → community → token → momentum → narrative →
  risk → master → opportunity rank → optional AI enrichment). Used by every
  entry point so scoring is identical everywhere.
- `controller.py` — `ContinuousScanner`: the 24/7 loop (discovery → filter →
  analyze top candidates → pump.fun launch funnel → watchlist recheck →
  alerts). Per-cycle error isolation + exponential backoff; graceful SIGINT/
  SIGTERM shutdown.
- `daily_routine.py` — the once-a-day full routine (market regime, watchlist
  deep review, daily report).
- `watchlist_review.py` — tier re-assignment logic.

---

## 5. What the bot does, end to end

1. **Discovers** new tokens two ways: GeckoTerminal new-pools polling, and (opt-in)
   the free PumpPortal WebSocket streaming pump.fun launches in real time.
2. **Filters** most launches out immediately (Rule 10): dead/insider/anonymous
   launches never reach analysis. Pump.fun launches must additionally show
   traction (market cap, growth, community replies, recent trading) AND be
   confirmed by an independent market provider before deep analysis — discovery
   is never confirmation (Part 32.5 §2).
3. **Analyzes** survivors across 8 categories via the shared pipeline, producing
   a master score (Part 31 weights), a classification (Elite→Avoid), a risk
   score, and an opportunity rank.
4. **Verifies** the strongest candidates: important market-based alerts are
   cross-checked against a second data source; if the AI layer is enabled,
   gate-passing opportunities get one Claude judgment that can confirm (annotate
   the alert) or veto (knock it below the gate so it never fires).
5. **Alerts** to Telegram, prioritized. Confirmed rugs/deaths get a single quiet
   post-mortem; genuine risk on live tokens and strong fresh candidates get HIGH
   alerts.
6. **Tracks** everything on a tiered watchlist, re-checks it on a cadence,
   archives dead/weakened tokens, and ranks the live ones by opportunity.
7. **Learns**: every prediction is snapshotted; a scheduled backtest measures
   outcomes over time so accuracy metrics accumulate (human-in-the-loop for
   weight changes, per the Part 31 lock).

---

## 6. Everything done in the most recent session (commit by commit)

Branch: `claude/handoff-folder-review-fuu9dq`. Chronological:

- **`43dca9e`** Fixed 15 findings from a partial bug hunt across the
  narrative/AI/alerts/collectors code (fabricated scores, unhandled provider
  data, silent data loss, a Rule 16 credential-leak path).
- **`4e769e7`** Built **Part 32.5 §3 — Pump.fun early-launch discovery**:
  `PumpPortalClient` (WS), `PumpFunFrontendClient`, `LaunchMonitor` funnel,
  `PumpFunSettings`. Off by default; opt-in via `monitor --pumpfun`.
- **`07face9`** Added the **DigitalOcean deployment kit**: `deploy/` systemd
  unit, setup script, README.
- **`a8db190`** Wired **wallet + AI services into the continuous scanner** behind
  `enable_in_monitor` flags (Parts 17/23).
- **`3d442bd`** Added **scheduled jobs** (daily routine, backtest refresh, DB
  backup) via cron, and switched SQLite to **WAL** for safe shared access.
- **`a4a949b`** **Dead-token handling**: a collapsed-liquidity token gets one
  MEDIUM post-mortem and is archived, instead of spamming repeated HIGH warnings.
- **`3cd8295`** **AI verification of gate-passing opportunities** (Part 32.5 §8):
  one Claude judgment only after all gates pass; confirms or vetoes.
- **`9377558`…`179d2c9`** A **full adversarial bug hunt** (9 parallel finders +
  independent verifiers) across the whole codebase, fixing 31+ confirmed bugs in
  four commits: timestamp crashes that could kill the scanner, a Helius key leak,
  NaN-blind validation, WebSocket backoff hammering, launch-funnel candidate
  loss, transient-outage false archiving, alert-delivery loss, market
  self-confirmation, AI narrative fabrication, and more. Each fix has a
  regression test.
- **`8963509`** Added the **HIGH `strong_candidate` alert tier** so genuinely
  strong fresh launches (no CoinGecko community data yet) reach a HIGH-filtered
  phone instead of being capped at MEDIUM and hidden.
- **`f9dc91d`** **Verification pass over Parts 20–33**: aligned Part 33 security
  weights to the literal spec (undocumented drift), and **built Part 28 §5/§6
  opportunity ranking** (`watchlist --top`).

Tests went from ~362 to **457**, all green.

---

## 7. Deployment & operations (the live droplet)

Running on a **DigitalOcean droplet** (Ubuntu, ~$6/mo) as a systemd service.

**The deployment kit (`deploy/`):**
- `meme-intelligence.service` — systemd unit; `Restart=always`, 512MB cap.
- `setup.sh` — one-shot bootstrap (venv, deps, installs the unit).
- `install-cron.sh` — installs three cron jobs: **daily routine** (13:05 UTC),
  **backtest refresh** (every 6h), **DB backup** (13:45 UTC, keeps 7 days).
- `backup_db.py` — consistent online SQLite backup (safe against the live writer).
- `README.md` — step-by-step DigitalOcean instructions.

**Operating commands (on the droplet):**
```
cd ~/meme-intelligence            # project dir
git pull                          # get latest code
sudo systemctl restart meme-intelligence   # apply changes
sudo systemctl status meme-intelligence    # is it running?
journalctl -u meme-intelligence -f         # live logs (Ctrl-C to stop watching)
```

**Currently active on the droplet:** the 24/7 monitor with `--pumpfun`,
smart-money intelligence (Helius key set), community data (CoinGecko key set),
AI opportunity-verification (Anthropic key set), Telegram delivery at
`external_min_priority=high`, plus the three cron jobs.

**Reliability (Rule 7):** the app self-recovers from transient errors with
exponential backoff; systemd restarts the process on hard crash/reboot; the DB
is backed up daily. Everything worth keeping is committed and pushed — the
droplet is reproducible from the repo + `.env`.

---

## 8. CLI reference (`python -m meme_intelligence <command>`)

- `search <query>` — find pairs by name/symbol/address.
- `token <address> [--chain]` — pairs for a contract.
- `discover [--network] [--limit] [--show-rejected]` — scan new pools once.
- `security <address>` — security assessment for one token.
- `scan` — one discovery+analysis pass.
- `plan <address> [--ai]` — trade-planning view (advisory).
- `report <address> [--ai]` — full intelligence report (the detailed one). `--ai`
  adds a Claude judgment.
- `quick <address>` — condensed report.
- `compare <addresses...>` — rank several tokens side by side.
- `watchlist [--refresh] [--include-archived] [--top] [--limit N]` — show tracked
  tokens; **`--top` ranks by opportunity score (Part 28)**.
- `alerts [--limit] [--test]` — alert history / send a test alert through sinks.
- `backtest [--refresh]` — grade predictions / measure due outcome windows.
- `wallets <address> [--chain]` — smart-money/whale intelligence (needs a wallet key).
- `daily [--network]` — full daily routine.
- `monitor [--network] [--cycles] [--interval] [--regime] [--pumpfun]` — the 24/7
  scanner. Ctrl-C stops gracefully.

Exit codes: 0 success, 1 no-data/error, 2 destructive security finding.

---

## 9. Configuration (secrets & key tunables)

All via environment variables / `.env` (see `.env.example` for the full list).
Secrets default to empty = that layer stays off (Rule 16).

**Secrets:**
- `MEMEINTEL_TELEGRAM_BOT_TOKEN`, `MEMEINTEL_TELEGRAM_CHAT_ID` — Telegram delivery.
- `MEMEINTEL_DISCORD_WEBHOOK_URL` — Discord delivery (optional).
- `MEMEINTEL_ANTHROPIC_API_KEY` — AI reasoning / `--ai` / opportunity verification.
- `MEMEINTEL_HELIUS_API_KEY` and/or `MEMEINTEL_BIRDEYE_API_KEY` — wallet intelligence.
- `MEMEINTEL_COINGECKO_API_KEY` — higher CoinGecko rate limits (optional).

**Feature flags:**
- `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR` (or `monitor --pumpfun`) — pump.fun stream.
- `MEMEINTEL_WALLET_ENABLE_IN_MONITOR` — smart-money in the 24/7 loop.
- `MEMEINTEL_AI_ENABLE_IN_MONITOR` — judge *every* analyzed token (expensive).
- `MEMEINTEL_AI_VERIFY_OPPORTUNITIES` (default true) — judge only gate-passers.
- `MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY` — phone filter (currently `high`).

**Notable tunables:**
- `MEMEINTEL_ALERTS_STRONG_CANDIDATE_OVERALL` (88) — bar for the HIGH strong-candidate tier.
- `MEMEINTEL_ALERT_ENGINE_DEAD_LIQUIDITY_USD` (500) — below this a token is "dead".
- `MEMEINTEL_OPPORTUNITY_WEIGHTS_*` — the Part 28 opportunity-rank weights.
- `MEMEINTEL_WORKFLOW_MONITOR_INTERVAL_SECONDS` (45) — scan cadence.

---

## 10. Known gaps & deferred items (documented, not failures)

- **Web/monitoring dashboard** (Part 21 §10 / 22 / 27 §12 / 28 §11) — the system
  is CLI + Telegram; a dashboard is a deferred Phase-4 item.
- **Creator track-record intelligence** (Part 27 §10) — blocked: no free data
  source exposes a creator's prior-launch success/failure history.
- **Paid social data** (Twitter engagement, bot detection, Discord) — deferred
  under the "cheap aggregator" decision until the system proves itself; free
  CoinGecko community data is used today.
- **EVM wallet intelligence** — Part 17 is Solana-only for now.
- The pump.fun frontend API is **unofficial** and has rotated hosts before; the
  base URL is config and failures degrade to data gaps. Watch for repeated 403s.

---

## 11. Recommended next steps for a new session

1. **Let it run and read the data.** After ~1–2 weeks, run `backtest` on the
   droplet — it will show which alert types were useful vs noise and whether
   high-scored tokens outperformed. That data drives all tuning.
2. **Tune from evidence, not guesses.** Adjust `strong_candidate_overall`,
   the dead-liquidity floor, or the phone filter based on what the backtest says.
3. **Consider the dashboard** if a UI is wanted — it's the one substantial
   unbuilt feature.
4. **Rotate the exposed secrets** — the Telegram bot token and Anthropic key
   were shown in chat during setup; regenerating them is good hygiene (BotFather
   `/token`; console.anthropic.com).
5. **Always** read `PROJECT_RULES.md`, `handoff/STATUS.md`, and
   `handoff/DECISIONS_LOG.md` before changing anything, and keep the 457 tests
   green.

---

*Generated as the session handoff. The authoritative living documents remain
`handoff/STATUS.md` (build state), `handoff/DECISIONS_LOG.md` (why things are the
way they are), `handoff/next_steps/INDEX.md` (part-by-part verification), and
`PROJECT_RULES.md` (the 21 rules).*
