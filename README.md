# Meme Coin Intelligence System

A multi-layer research and alerting platform that discovers, analyzes, scores,
and monitors early-stage meme coin opportunities using market, blockchain,
social, and security data.

**This is a research intelligence platform. It never executes trades.** It
finds evidence, scores opportunities, flags risks, and leaves decisions to a
human operator.

## Current status

| Spec part | Deliverable | Status |
|---|---|---|
| Part 1 — Role, mission & operating rules | Classification framework, scoring models, config system, logging | ✅ Built |
| Part 2 — Scanning infrastructure & data architecture | Rate limiting, TTL cache, retry/backoff, provider failover pool, base collector, DexScreener client | ✅ Built |
| Part 3 — Discovery engine | GeckoTerminal new-pool client, discovery engine with hard filters, dedupe, Discovery Score, rejection tracking | ✅ Built |
| Part 4 — Rug detection & security analysis | GoPlus client (EVM + Solana), security analyzer with graded risk taxonomy, destructive-risk overrides, confidence/coverage reporting | ✅ Built |
| Part 5 — Foundation & community intelligence | Community analyzer (engagement/growth/loyalty/creativity/dev-relationship) with fake-community detection; foundation score combiner | ✅ Built (free CoinGecko community data live: telegram/sentiment/reddit; Twitter-depth aggregator deferred) |
| Part 6 — On-chain intelligence & wallet behavior | On-chain analyzer (holder health, volume quality, buy/sell pressure, phase classification) running today on market+security data; smart-money/whale/flow slots ready for wallet collectors | ✅ Built (partial data sources) |
| Part 7 — Token evaluation & market structure | Token analyzer: market-cap staging, FDV dilution, liquidity/volume-to-mcap ratios, supply concentration, valuation classification, competition percentile helper | ✅ Built |
| Part 8 — Trading strategy & execution framework | Trade planner: trade score, setup classification, conviction + sizing guidance, entry checklist, required confirmations, invalidation conditions, FOMO questions — plans only, never orders | ✅ Built |
| Part 9 — Risk management & capital protection | Risk analyzer (5-component risk score, higher = riskier), portfolio exposure limits, drawdown posture, emergency exit flags | ✅ Built |
| Part 10 — Scoring algorithm & decision engine | Master scoring engine: Part 31-locked weights, red-flag overrides forcing Avoid, 6-question decision tree with caps, timing derivation, full decision trace | ✅ Built |
| Part 11 — Daily operating routine | Daily routine: market-environment check (CoinGecko), discovery→analysis pipeline, tiered watchlist with review/archival, research journal, daily report — all persisted | ✅ Built |
| Part 12 — Final report template | Canonical intelligence report renderer: evidence-derived bull/bear cases, scoring table, decision trace, trade planning, final verdict | ✅ Built |
| Database foundation | SQLite storage: tokens, assessment snapshots (feeds Part 24 backtesting), watchlist, journal | ✅ Built |
| Part 13 — Automation & agent architecture | Continuous scanner (crash-tolerant loop, graceful shutdown, seen-set dedupe), automation rules (opportunity gates, emergency review, score-drop review), notification engine with cooldown | ✅ Built (console sink; Telegram/Discord in Part 29 phase) |
| Part 14 — Trading intelligence & momentum | Momentum analyzer (price/volume/social/on-chain lenses, acceleration over level, fake-momentum detection), entry zones, preferred action; multi-window (1h/6h/24h) market data | ✅ Built |
| Part 15 — Scanner configuration & anti-throttling | MarketDataService: provider failover pool (DexScreener ⇄ GeckoTerminal, shared interface + chain aliases), multi-source liquidity verification with alert downgrade on disagreement, momentum alert, multi-speed monitoring (watchlist recheck cadence) | ✅ Built |
| Part 16 — AI execution rules & operating instructions | Canonical analyst system prompt (for the LLM layer) + banned-language guard enforced on all generated reports; quick/compare/watchlist commands; alert "watch next" guidance; shared watchlist reviewer | ✅ Built |
| Part 17 — Smart money & whale intelligence | Helius + Birdeye collectors (top holders resolved to owners, trades, transfers), wallet analyzer (whale classification, accumulation verdict, exchange flow lower-bounds, smart-money score), wallet sightings DB + reputation formula, 3 new alert types, wallets command; enriches the on-chain score | ✅ Built (Solana; track records accumulate via Part 24) |
| Part 18 — Advanced rug detection & scam prevention | Continuous contract-change monitoring: security facts persisted per token and diffed on every re-analysis (honeypot appearing / ownership un-renounced / mint authority / LP unlock → CRITICAL; blacklist/pause/tax hikes → HIGH; concentration creep / holder drain → MEDIUM); promotion-and-exit pattern detector | ✅ Built (deep dev-history indexing deferred — needs a data source we don't have) |
| Part 19 — Narrative intelligence & viral potential | Viral score (memorability/shareability/emotional impact/cultural timing/participation, 5×20%); narrative intelligence score (meme strength/timing/viral potential/creativity/long-term, 5×20%) feeding the master framework's 15% narrative category; category + life-cycle stage classification; sentiment; three narrative risk factors → Low/Medium/High; viral catalysts; evidence-derived strengths/weaknesses; community-engine cross-fill for participation/creativity | ✅ Built (judgment slots await the AI layer / social collectors — engine reports partial coverage honestly) |
| Part 23 — AI agent integration & intelligence pipeline | LLM reasoning layer (Claude, structured outputs): one validated judgment call fills the foundation/narrative qualitative slots + bull/bear reasoning + confidence; structured Section-3 snapshots (never raw data); layered validation (schema → ranges → banned-language guard → confidence floor); research modes (fast/standard/deep); runs post-pipeline only, never on destructive tokens, degrades to deterministic evidence on any failure; `report --ai` / `plan --ai` | ✅ Built (live-verified; memory/feedback loop lands with Part 24) |
| Parts 20+ — Full alert intelligence, dashboard, backtesting | — | ⏳ Upcoming |

## Project structure

```
meme_intelligence/
├── __main__.py             # CLI: search / token / discover / security / scan
├── config/
│   └── settings.py         # all tunables, env-overridable (MEMEINTEL_* vars)
├── core/
│   ├── enums.py            # Classification, RiskTier, ConfidenceLevel, ScanLayer
│   ├── errors.py           # transient-vs-permanent error hierarchy
│   ├── models.py           # TokenIdentity, DexPair, SecurityProfile, score aggregation
│   ├── cache.py            # async TTL cache with LRU eviction
│   ├── rate_limiter.py     # token-bucket limiter (per provider)
│   ├── retry.py            # exponential backoff with jitter
│   ├── provider_pool.py    # multi-provider failover with cooldowns
│   └── logging_setup.py    # console + rotating file logging
├── collectors/
│   ├── base.py             # shared HTTP collector (rate limit + cache + retry)
│   ├── market_data.py      # DexScreener + GeckoTerminal → normalized DexPair
│   ├── market_service.py   # failover pool + cross-source verification
│   ├── security_data.py    # GoPlus (EVM + Solana) → normalized SecurityProfile
│   └── wallet_data.py      # Helius + Birdeye → holders/trades/transfers (Solana)
├── scanners/
│   └── discovery.py        # Layer 1: filter/dedupe/score new pools
├── analyzers/
│   ├── common.py           # shared SubScore/Finding/confidence machinery
│   ├── security_analyzer.py   # Layer 2: graded security assessment
│   ├── community_analyzer.py  # Layer 3: social strength + fake detection
│   ├── onchain_analyzer.py    # Layer 3: wallet/volume behavior + phase
│   ├── foundation_analyzer.py # foundation score combiner (AI inputs later)
│   ├── token_analyzer.py      # token structure: staging, dilution, ratios
│   ├── risk_analyzer.py       # risk score + portfolio limits + emergencies
│   ├── momentum_analyzer.py   # momentum lenses, entry zones, preferred action
│   ├── narrative_analyzer.py  # Part 19: viral score, narrative score, stage, catalysts
│   ├── wallet_intelligence.py # whales, accumulation, smart-money score, reputation
│   ├── security_monitor.py    # Part 18: security-fact diffs, change severities
│   └── scoring_engine.py      # master score: overrides, decision tree, weights
├── trading/
│   └── trade_planner.py    # trade plans: checklist, sizing guidance, invalidations
├── alerts/
│   └── notification_engine.py # automation rules + alert dispatch with cooldown
├── database/
│   └── storage.py          # SQLite: tokens, snapshots, watchlist, journal
├── workflow/
│   ├── pipeline.py         # shared per-token analysis chain (one implementation)
│   ├── daily_routine.py    # Part 11 daily research-desk orchestration
│   └── controller.py       # Part 13 continuous 24/7 scanning loop
├── ai/
│   ├── report_generator.py # Part 12 canonical intelligence report
│   ├── comparison.py       # Part 16 multi-token comparison + ranking
│   ├── prompts.py          # analyst system prompt + banned-language guard
│   └── reasoning.py        # Part 23: LLM judgment service (structured outputs)
tests/                      # pytest suite (unit tests, no network required)
```

## Quick start

```bash
pip install -r requirements.txt
python -m pytest                          # run the test suite
python -m meme_intelligence search PEPE   # live market lookup (no API key needed)
python -m meme_intelligence discover --network solana        # find new launches
python -m meme_intelligence security <address> --chain solana  # rug/security check
python -m meme_intelligence scan --network solana --top 5    # discovery -> security -> on-chain
python -m meme_intelligence plan <address> --chain ethereum --regime neutral  # full pass + trade plan
python -m meme_intelligence report <address> --chain ethereum  # canonical intelligence report
python -m meme_intelligence report <address> --chain solana --ai  # + AI reasoning layer (Part 23)
python -m meme_intelligence quick <address> --chain solana    # Level 1 fast scan
python -m meme_intelligence compare ethereum:0xPEPE solana:WIFADDR  # table + ranking
python -m meme_intelligence watchlist --refresh               # show / re-score tracked tokens
python -m meme_intelligence daily                             # full daily routine + watchlist
python -m meme_intelligence monitor --cycles 5 --interval 30  # continuous scanner + alerts
```

Configuration is entirely environment-driven — see `.env.example` for every
variable and its default. Secrets (API keys) are only ever read from the
environment.

## Key architectural decisions

- **Python 3.11 + asyncio.** The workload is I/O-bound API fan-out; async
  keeps one process capable of running many collectors concurrently. The spec
  (Parts 21/31) allows Python or Node.js — Python was chosen for its data
  ecosystem, which the later backtesting phase (Part 24) benefits from.
- **Canonical scoring weights come from Part 31's Framework Consistency Lock**
  (Foundation/Security/Community/Blockchain/Momentum/Narrative 15% each,
  Timing 10%). Earlier drafts (Parts 10/20) differ; Part 31 explicitly locks
  this version. Weights remain configurable for backtesting experiments
  (Part 24) but defaults are the locked framework.
- **Security sub-weights follow Part 33** (Contract 25 / Liquidity 25 /
  Distribution 20 / Developer 15 / Manipulation 15) as the latest refinement
  of the security engine.
- **Missing data is `None`, never zero.** Score aggregation renormalizes over
  the categories that have data and reports *coverage*, so partially-analyzed
  tokens can't masquerade as fully-vetted ones (Rule 8 — data before
  assumptions).
- **Every collector is throttle-safe by construction.** Rate limiting,
  caching, retry with backoff, and provider failover live in the base layer,
  so no individual collector can abuse an API (Rules 9/10/11, Part 32.5).
- **Security risk is graded, not binary** (Part 33 / Part 31 lock): normal
  early-stage uncertainty deducts lightly; serious warnings (mint authority,
  unlocked LP, concentration) deduct heavily; destructive risks (honeypot,
  non-sellable, confirmed scam) force the score to 0. Partial-coverage
  assessments are labeled "partial data" with an explicit warning — unknown
  facts are never presented as safe.
- **Discovery ≠ confirmation** (Part 31 Section 6): discovery candidates have
  passed basic gates only; rejected pools are returned with reasons so the
  future learning system can measure false negatives (Part 24).
- **In-memory cache and SQLite-first storage** (storage arrives with the
  database phase). Redis/PostgreSQL can replace them behind the same
  interfaces when scale requires — simple, reliable solutions first (Rule 21).

## Roadmap (per spec build order, Parts 22/31)

1. ~~Foundation: config, models, collectors~~ ✅
2. ~~Discovery engine~~ ✅
3. ~~Security analysis engine (rug detection, honeypot, holder concentration)~~ ✅
4. ~~Community / on-chain / foundation analyzers~~ ✅ (free CoinGecko
   community data live; Twitter-depth aggregator deferred until earned)
5. ~~Token structure analyzer + trade planner~~ ✅
6. ~~Risk management framework + master scoring engine~~ ✅
7. ~~Daily workflow + database + report template~~ ✅
8. ~~Momentum analyzer + continuous scanner + automation rules~~ ✅
9. ~~Narrative engine~~ ✅ (Telegram/Discord sinks, dashboard still pending)
10. ~~AI/LLM integration for qualitative judgments~~ ✅ (backtesting loop still pending)
