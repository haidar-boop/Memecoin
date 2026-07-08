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
| Part 5 — Foundation & community intelligence | Community analyzer (engagement/growth/loyalty/creativity/dev-relationship) with fake-community detection; foundation score combiner | ✅ Built (engines; social collectors need API keys) |
| Part 6 — On-chain intelligence & wallet behavior | On-chain analyzer (holder health, volume quality, buy/sell pressure, phase classification) running today on market+security data; smart-money/whale/flow slots ready for wallet collectors | ✅ Built (partial data sources) |
| Part 7 — Token evaluation & market structure | Token analyzer: market-cap staging, FDV dilution, liquidity/volume-to-mcap ratios, supply concentration, valuation classification, competition percentile helper | ✅ Built |
| Part 8 — Trading strategy & execution framework | Trade planner: trade score, setup classification, conviction + sizing guidance, entry checklist, required confirmations, invalidation conditions, FOMO questions — plans only, never orders | ✅ Built |
| Parts 9+ — Risk management, scoring engine, alerts, dashboard, database | — | ⏳ Upcoming |

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
│   └── security_data.py    # GoPlus (EVM + Solana) → normalized SecurityProfile
├── scanners/
│   └── discovery.py        # Layer 1: filter/dedupe/score new pools
├── analyzers/
│   ├── common.py           # shared SubScore/Finding/confidence machinery
│   ├── security_analyzer.py   # Layer 2: graded security assessment
│   ├── community_analyzer.py  # Layer 3: social strength + fake detection
│   ├── onchain_analyzer.py    # Layer 3: wallet/volume behavior + phase
│   ├── foundation_analyzer.py # foundation score combiner (AI inputs later)
│   └── token_analyzer.py      # token structure: staging, dilution, ratios
├── trading/
│   └── trade_planner.py    # trade plans: checklist, sizing guidance, invalidations
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
4. ~~Community / on-chain / foundation analyzers~~ ✅ (social + wallet
   collectors pending API keys — engines run on partial data honestly)
5. ~~Token structure analyzer + trade planner~~ ✅
6. Risk management framework, momentum analyzer, database layer
7. Scoring engine with red-flag overrides + AI report generation
8. Alert system (Telegram/Discord) + dashboard
9. Backtesting and self-improvement loop
