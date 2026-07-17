# Architecture — The Mental Model a New Session Needs

> **Why this document exists.** STATUS.md says *what* is built part-by-part;
> DECISIONS_LOG.md says *why*. Neither gives a fresh session the working
> mental model — how a token flows from discovery to a phone buzz, and which
> invariants must never regress. This is that map. Everything here is
> verifiable in code; file paths are given so you can jump straight in.

## The one-paragraph system

Python 3.11 async, SQLite, free/low-cost APIs. A 24/7 scanner
(`python -m meme_intelligence monitor`) discovers new Solana (and EVM)
tokens, screens them for security risk, scores them across weighted
categories, tracks a tiered watchlist, learns from outcomes, and delivers
prioritized alerts to Telegram. **It never trades and never holds funds.**
16 CLI commands share the same engine (`search token discover security scan
plan report quick compare watchlist alerts backtest wallets daily monitor
mind`).

## Package map (`meme_intelligence/`)

| Package | Job | Key files |
|---|---|---|
| `core/` | Plumbing: models, enums, errors, rate limiter, TTL cache, retry w/ backoff, provider failover pool, logging | `models.py`, `provider_pool.py`, `retry.py`, `errors.py` |
| `collectors/` | HTTP clients, all built on `BaseCollector` (rate limit + cache + retry + redaction for free) | `market_data.py` (DexScreener, GeckoTerminal, CoinGecko), `security_data.py` (GoPlus), `wallet_data.py` (Helius/Birdeye), `market_service.py` (failover pool + cross-check + search), `pumpfun_data.py` |
| `scanners/` | Discovery filtering + pump.fun launch funnel | `discovery.py`, `launch_monitor.py` |
| `analyzers/` | One analyzer per intelligence dimension; all return assessments with `coverage` and honest unknowns | `security_analyzer.py`, `community_analyzer.py`, `onchain_analyzer.py`, `token_analyzer.py`, `risk_analyzer.py`, `momentum_analyzer.py`, `narrative_analyzer.py`, `scoring_engine.py` (the keystone: locked weights + decision tree + red-flag overrides), `security_monitor.py` (fact diffing) |
| `workflow/` | Orchestration | `pipeline.py` (one token → full result), `controller.py` (the 24/7 loop — **the most important file in the repo**), `daily_routine.py`, `watchlist_review.py` |
| `alerts/` | Alert rules + dispatch + delivery | `notification_engine.py` (rules, gates, interest gate, engine), `sinks.py` (Telegram/Discord, min-priority, routes, sanitization) |
| `ai/` | Anthropic reasoning layer (optional — system fully functional without) | `reasoning.py`, `prompts.py`, `report_generator.py`, `comparison.py` |
| `learning/` | Self-learning mind layer (Section-10 spec) | see below |
| `analytics/` | Outcome grading / backtesting | `backtesting.py` |
| `database/` | SQLite storage (WAL) | `storage.py` |
| `config/` | Every tunable, env-overridable, validated at construction | `settings.py` (~296 `MEMEINTEL_*` vars, see `.env.example`) |
| `trading/` | Plans only, never orders | `trade_planner.py` |

## The monitor loop, end to end (`workflow/controller.py`)

Each cycle (`_run_cycle`):

1. **Market regime** from CoinGecko majors (risk-on/neutral/risk-off).
2. **Discovery**: GeckoTerminal `new_pools` → `DiscoveryEngine` hard filters
   (min liquidity $5k, freshness, dedupe) → candidates. A discovery outage
   yields an empty list; the cycle still runs (provider isolation).
3. **Pump.fun funnel** (optional): PumpPortal WebSocket stream → traction
   recheck → independent market confirmation → analysis. READY entries
   expire after `ready_ttl_hours=72`.
4. **Per-token analysis** (`ResearchPipeline.analyze_pair`): GoPlus security
   profile → analyzers (security / on-chain / community / token / momentum /
   narrative / risk) → `ScoringEngine` (locked weights, coverage-aware,
   red-flag overrides, 6-question decision tree) → `PipelineResult`.
5. **`_process_result`** — the decision spine, in exact order:
   snapshot recorded → security-fact diff vs baseline → watchlist
   tier/archive (dead tokens never re-enter) → **the alert decision
   pipeline (below)** → dispatch → alert history + journal → learning feed.
6. **Watchlist recheck** every `watchlist_recheck_cycles` (least-recently-
   updated first, so the whole list rotates through the per-pass limit).
7. **Learning maintenance**: `retrain_if_due` (classifier warm-start,
   archetype refit, drift check) — error-isolated, never breaks the loop.
8. Graceful shutdown on SIGINT/SIGTERM; outer backoff (5s→300s) on cycle
   failures; systemd restarts the process if it dies.

## The alert decision pipeline (memorize this)

For every analyzed token, in order. Files: `alerts/notification_engine.py`
(rules + gates), `workflow/controller.py` (screens + dispatch glue).

1. **Death check** — liquidity < `dead_liquidity_usd` ($500): ONE post-mortem
   replaces the warning/drop pair; token archived; only CRITICAL destructive
   findings survive alongside it. Unknown/NaN liquidity is NOT death.
2. **Review gates** — security 80 / community 70 / liquidity 70 / on-chain 75
   / overall 85. All gates with data must pass; unknown never passes
   (Rule 8). Fully-verified → `high_priority_opportunity` (HIGH).
   Only-community-unverified + overall ≥ 88 → `strong_candidate` (HIGH).
   Anything else that passes-with-gaps → `early_opportunity` (MEDIUM).
3. **Strong-candidate caveats** (each downgrades HIGH → MEDIUM, reason
   named): pool depth < $25k or unknown; AI judgment confidence < 40; AI
   verification ran but produced nothing usable (`ai_verification_inconclusive`).
4. **Deterministic risk screen** (`_deterministic_risk_veto`) — runs for
   every would-be HIGH, **with or without an AI key** (decoupled 2026-07-10
   after rug pulls fired unscreened while the key was off): risk alerts
   already firing, or mind-layer rug engine ≥ `verify_skip_rug_score` (10)
   using contract facts + deployer blacklist + observed dev outflow.
5. **Copycat screen** (`_copycat_veto`) — one cached provider search per
   candidate: a different token with the same normalized symbol/name holding
   ≥ $100k AND ≥ 10× the candidate's liquidity → "knock-off riding that
   name", downgrade with the original named. Outages never veto and are
   never cached.
6. **AI verification** — ONLY if a key is configured, `verify_opportunities`
   is on, the screens are clean, and this token was never verified before
   (one paid call per token, ever; inconclusive results are remembered and
   count as a veto, not a pass).
7. **Interest gate** (`gate_events_by_interest`) — protective alerts
   (`emergency_review, risk_warning, score_drop_review, token_death,
   whale_exit, insider_risk, community_fake, security_change`) demote to LOW
   unless the token previously earned a HIGH opportunity alert (or one fires
   in the same batch). Rationale: the operator only learns about tokens
   through HIGH opportunity alerts, so warnings on anything else protect no
   decision. Fails OPEN on storage errors. Config:
   `MEMEINTEL_ALERT_ENGINE_RISK_ALERTS_REQUIRE_INTEREST`.
8. **Market cross-verification** — market-data-based alerts are confirmed
   against a second provider; disagreement downgrades, unavailability
   annotates (never silently confirms).
9. **NotificationEngine.dispatch** — rank (impact/confidence/urgency/
   novelty), 900s cooldown per (token, type, priority), **delivery
   accounting**: when external sinks exist, only they decide "delivered";
   cooldown is stamped only on actual delivery so lost alerts retry.
10. **Sinks** — Telegram/Discord with `min_priority` (the operator runs
    HIGH+ on his phone), per-category routes, token-name sanitization
    (on-chain metadata is attacker-controlled), secrets redacted from logs.

Net effect: **the phone buzzes only for (a) a vetted HIGH opportunity, or
(b) trouble on a coin the bot previously recommended.** Everything else is
console/history only.

## The learning ("mind") layer (`meme_intelligence/learning/`)

- `features.py` — fingerprint v3 (`FEATURE_VERSION = 3`): signed-log1p
  scaling for heavy-tailed metrics, presence masks for missing data,
  `StandardScalerBundle`. **Bump FEATURE_VERSION on any feature change** —
  `service._load_artifacts` discards stale artifacts by version.
- `analog.py` — FAISS cosine k-NN over resolved coins with recency decay
  (`similarity × exp(-age/half_life)`); abstains below `min_analogs`.
- `classifier.py` — LightGBM warm-start (`init_model`), mean-normalized
  time-decay × balanced class weights (guards the all-old-batch hessian
  collapse), trains once enough outcomes resolve.
- `archetypes.py` — HDBSCAN clusters + novelty flag (min_cluster_size ≥ 2
  guard).
- `rug_engine.py` — hard-signal rules (unsellable, mint/freeze authority,
  concentration, LP unlock, sell tax, liquidity removal, dev dumping, fake
  volume, deployer blacklist). **Unknown never fires a signal.** Exposes
  `unsellable_override` seam for a future live sell-simulation (ROADMAP #1).
- `ensemble.py` — accuracy-weighted blend of analog/classifier/archetype/rug
  verdicts; abstaining sources drop out; tracks blended-verdict accuracy;
  drift below `drift_accuracy_floor` (with `drift_min_samples`) triggers a
  full rebuild.
- `store.py` — `learning_state/learning.db` (separate from the main DB):
  coins, snapshots, outcomes, deployer blacklist (atomic claim via
  `deployer_counted` — prevents double-counting races between processes).
- `service.py` — the façade: `evaluate_coin()` (called per scanned coin),
  `resolve_outcome()` (instant learning; `_on_rug_upgrade` blacklists
  deployers when a slow rug is re-labeled later), `retrain_if_due()`.
- **Cross-process topology**: the monitor daemon and the 6-hour backtest
  cron share the SQLite (WAL) and `learning_state/`; an ensemble-dirty flag
  prevents one process clobbering the other's freshly-written artifacts.
- Feed points: scanner `_feed_learning` (every analyzed coin) and
  `analytics/backtesting.refresh_outcomes` (graded outcomes → resolutions).
- CLI: `mind evaluate <address>`, `mind metrics` (the report card).

## Invariants and gotchas (each one is a fixed bug — do not regress)

1. **Unknown never fires, never passes** (Rule 8). Every threshold check
   must first test `is not None` and `math.isfinite` — NaN comparisons are
   silently False and have caused misclassification (NaN liquidity read as
   "dead"; NaN bar in the copycat rule).
2. **Base-vs-quote**: DexScreener token endpoints return pairs where the
   query token is the QUOTE side too; `get_token_pairs` filters to
   base-side only. Don't remove that filter.
3. **Delivery accounting**: `ConsoleSink.external = False`. When any
   external sink is configured, only external sinks count as delivery;
   cooldown stamps only after real delivery so lost Telegram alerts retry.
4. **`_ai_verified` caches per-token verdicts including "inconclusive"** —
   a vetoed token is deliberately NOT cached (risk may clear); an
   inconclusive one IS (prevents re-paying for a judgment already thrown
   away, and keeps the veto on later rechecks).
5. **Everything in the daemon is bounded**: `_BoundedKeySet` for seen/
   verified/copycat caches, LRU in the market service, cooldown pruning in
   the notification engine. New long-lived caches must be bounded too.
6. **Provider pool health**: `TransientCollectorError` counts toward
   cooldown; permanent 404s don't poison a provider; HTTP 429 honors
   Retry-After (capped 120s) — the retry loop sleeps at least the hint.
7. **`AutomationRules.evaluate(operator_interest=...)` defaults True** so
   callers that don't track alert history (CLI paths, tests) keep full
   priority. Only the controller passes a computed value.
8. **Frozen dataclasses everywhere** — mutate via `dataclasses.replace`.
9. **Settings validate at construction** — new numeric fields need
   positivity/range checks; boolean fields must be SKIPPED by
   loop-over-asdict positivity checks (`isinstance(value, bool)` first).
10. **Secrets never reach logs**: collectors take `redact=` tuples;
    Telegram/Discord tokens and webhook URLs are scrubbed from every error
    string; alert text sanitizes token names (backticks/newlines/mentions).
11. **pytest** is configured `-q` (`pytest.ini`); run
    `python -m pytest tests/` and expect **626 passed** as of 2026-07-10.
