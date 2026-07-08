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
| Parts 3+ — Discovery, security, community, on-chain engines, scoring, alerts, dashboard | — | ⏳ Upcoming |

## Project structure

```
meme_intelligence/
├── __main__.py           # temporary CLI for exercising the collection layer
├── config/
│   └── settings.py       # all tunables, env-overridable (MEMEINTEL_* vars)
├── core/
│   ├── enums.py          # Classification, RiskTier, ConfidenceLevel, ScanLayer
│   ├── errors.py         # transient-vs-permanent error hierarchy
│   ├── models.py         # TokenIdentity, DexPair, CategoryScores, score aggregation
│   ├── cache.py          # async TTL cache with LRU eviction
│   ├── rate_limiter.py   # token-bucket limiter (per provider)
│   ├── retry.py          # exponential backoff with jitter
│   ├── provider_pool.py  # multi-provider failover with cooldowns
│   └── logging_setup.py  # console + rotating file logging
├── collectors/
│   ├── base.py           # shared HTTP collector (rate limit + cache + retry)
│   └── market_data.py    # DexScreener client → normalized DexPair models
tests/                    # pytest suite (unit tests, no network required)
```

## Quick start

```bash
pip install -r requirements.txt
python -m pytest                          # run the test suite
python -m meme_intelligence search PEPE   # live end-to-end check (no API key needed)
python -m meme_intelligence token <contract-address> --chain solana
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
- **In-memory cache and SQLite-first storage** (storage arrives with the
  database phase). Redis/PostgreSQL can replace them behind the same
  interfaces when scale requires — simple, reliable solutions first (Rule 21).

## Roadmap (per spec build order, Parts 22/31)

1. ~~Foundation: config, models, collectors~~ ✅
2. Discovery engine + database layer
3. Security analysis engine (rug detection, honeypot, holder concentration)
4. Community / on-chain / momentum / narrative analyzers
5. Scoring engine with red-flag overrides + AI report generation
6. Alert system (Telegram/Discord) + dashboard
7. Backtesting and self-improvement loop
