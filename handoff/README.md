# Handoff Folder — Meme Coin Intelligence System

Complete project handoff as of **2026-07-14**, written so a fresh session
(human or AI, with zero memory of prior conversations) can operate, debug,
and extend the system without re-learning anything the hard way.

**The system is LIVE**: Parts 1–33 + the Self-Learning Mind Layer +
Projects 1/2/3/6 are built, plus (2026-07-12 → 14) the buy-side size
ceiling, decline/peak-decline suppression, honest re-alert framing, the
smart-wallet data clock + reputation connector, and /wallets — and it runs
24/7 on the operator's droplet sending Telegram alerts and executing
operator-tapped buy/dump trades from a dedicated low-balance wallet
(**never auto-trading**). Branch: **`claude/ceiling-and-boost`** (supersedes
`claude/memecoin-onboarding-yrvjbg`).

## Read in this order

| # | File | What it gives you | Read when |
|---|---|---|---|
| 0 | **[OWNERS_MANUAL_2026-07-14.md](./OWNERS_MANUAL_2026-07-14.md)** | The complete refreshed manual: full 2026-07-12→14 changelog, every package/setting/command/table documented from a same-day deep read of the code, honest caveats, and the agreed next step (watchlist staleness door) | Start here |
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

A meme-coin research, intelligence, and (operator-initiated) trading
platform — **never an auto-trader**: since Project 6 (2026-07-11) it can
buy/dump from Telegram, but only on an explicit operator button/command,
signing from a dedicated low-balance wallet whose funding is the real risk
cap. Python 3.11 (async) + SQLite + free/low-cost APIs (DexScreener,
GeckoTerminal, GoPlus, CoinGecko, Helius, Birdeye, Jupiter, pump.fun,
optional Anthropic). It discovers new launches, screens security (including
a LIVE Jupiter round-trip sell test), scores across weighted categories,
tracks a tiered watchlist, **learns from outcomes** (analog memory +
classifier + rug engine + ensemble), and sends prioritized Telegram alerts
filtered so the operator's phone buzzes only for vetted opportunities or
trouble on coins it previously recommended. Two-way Telegram control
(`/status`, `/check`, `/buy`, `/dump`, …); the 24/7 entry point is
`monitor`.

## What's next (the operator's chosen plan — status as of 2026-07-11)

The five upgrades in [ROADMAP.md](./ROADMAP.md):
**1)** Jupiter live sell-simulation rug check — ✅ BUILT ·
**2)** two-way Telegram control + feedback — ✅ BUILT ·
**3)** mind-layer P(rug) alert veto — ✅ BUILT (veto flag stays off until
its measured accuracy earns authority) ·
**4)** read-only web dashboard — ❌ **DISCARDED by the operator; do not
build** · **5)** paid Twitter/X social intelligence — ⏸ **PARKED** until
the bot makes money (explicit cost approval required).
Plus the unplanned **Project 6** (operator-requested): live buy/dump from
Telegram — ✅ BUILT, ARMED, fully validated 2026-07-11 (live buy AND dump
round trip confirmed).
Smart-money wallet intelligence is temporarily **paused** (Helius credits
exhausted; re-enable criteria in DECISIONS_LOG 2026-07-11).

## Where the code lives

Repository: `haidar-boop/Memecoin` · Branch:
`claude/memecoin-onboarding-yrvjbg` · Implementation:
`meme_intelligence/` · Tests: `tests/` · Deployment kit: `deploy/` ·
Live host: the operator's DigitalOcean droplet (see OPERATIONS.md).
