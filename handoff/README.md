# Handoff Folder — Meme Coin Intelligence System

Complete project handoff as of **2026-07-10**, written so a fresh session
(human or AI, with zero memory of prior conversations) can operate, debug,
and extend the system without re-learning anything the hard way.

**The system is LIVE**: Parts 1–33 + the Self-Learning Mind Layer are built,
**626 tests pass**, and it runs 24/7 on the operator's droplet sending
Telegram alerts. Branch: `claude/session-rules-preferences-kc71bf`.

## Read in this order

| # | File | What it gives you | Read when |
|---|---|---|---|
| 1 | **[PROJECT_RULES.md](./PROJECT_RULES.md)** | The 21 engineering rules, verbatim. Mandatory in every session, for every change. | Always, first |
| 2 | **[OPERATOR.md](./OPERATOR.md)** | Who runs this, his constraints (phone-only, three budgets, agent caps), decisions already made | Always, second |
| 3 | **[SESSION_HANDOFF.md](./SESSION_HANDOFF.md)** | Current state, recent fixes, open threads, first moves | Always, third |
| 4 | **[ARCHITECTURE.md](./ARCHITECTURE.md)** | The mental model: package map, monitor loop, the full alert decision pipeline, learning layer, invariants that must never regress | Before touching code |
| 5 | **[OPERATIONS.md](./OPERATIONS.md)** | Live-deployment runbook: update procedure, `.env` state, verification, troubleshooting | Before/after any deploy |
| 6 | **[ROADMAP.md](./ROADMAP.md)** | **The agreed plan: 5 major upgrades**, fully specced, in build order + backlog | Before building anything new |
| 7 | [STATUS.md](./STATUS.md) | Part-by-part build status, file by file — ground truth for "is X done?" | When checking coverage |
| 8 | [DECISIONS_LOG.md](./DECISIONS_LOG.md) | Every architectural decision, spec-ambiguity resolution, and live-feedback tuning, with rationale | Before re-deciding anything |
| 9 | [SETUP.md](./SETUP.md) | Fresh local install + full CLI reference | Setting up a dev machine |
| 10 | [FULL_PROJECT_HANDOFF.md](./FULL_PROJECT_HANDOFF.md) + [next_steps/](./next_steps/) | The ORIGINAL specification, verbatim (rules + Parts 1–32.5). Historical source of truth for features. | When spec text is needed |

Note on `next_steps/`: those files are the verbatim spec for parts that were
unbuilt when the folder was created — **almost all are now built**
(STATUS.md is the ground truth). They are kept because Rule 1 makes the
literal spec text the reference for any future rework.

## The one-paragraph summary

A meme-coin research and intelligence platform — **never an auto-trader; it
never holds funds** — in Python 3.11 (async) + SQLite + free/low-cost APIs
(DexScreener, GeckoTerminal, GoPlus, CoinGecko, Helius, Birdeye, pump.fun,
optional Anthropic). It discovers new launches, screens security, scores
across weighted categories, tracks a tiered watchlist, **learns from
outcomes** (analog memory + classifier + rug engine + ensemble), and sends
prioritized Telegram alerts filtered so the operator's phone buzzes only
for vetted opportunities or trouble on coins it previously recommended.
16 CLI commands; the 24/7 entry point is `monitor`.

## What's next (the operator's chosen plan)

The five upgrades in [ROADMAP.md](./ROADMAP.md), in order:
**1)** Jupiter live sell-simulation rug check (free) · **2)** two-way
Telegram control + feedback (free) · **3)** mind-layer P(rug) as an alert
veto once its accuracy is proven (free) · **4)** read-only web dashboard
(free) · **5)** paid Twitter/X social intelligence (only with his explicit
cost approval).

## Where the code lives

Repository: `haidar-boop/Memecoin` · Branch:
`claude/session-rules-preferences-kc71bf` · Implementation:
`meme_intelligence/` · Tests: `tests/` · Deployment kit: `deploy/` ·
Live host: the operator's DigitalOcean droplet (see OPERATIONS.md).
