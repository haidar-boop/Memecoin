# Session Handoff — Current State as of 2026-07-12

> **What this is:** the "you just woke up in this project — here's where
> things stand" briefing. The durable knowledge lives in the sibling docs
> (ARCHITECTURE.md for the mental model, OPERATIONS.md for the runbook,
> OPERATOR.md for who you're working with, ROADMAP.md for what's next,
> STATUS.md for part-by-part build state, DECISIONS_LOG.md for every "why").
> This file is the snapshot that goes stale first — **update it at the end
> of any session that changes state.**

## Status snapshot

- **Everything in the spec that can be built is built** (Parts 1–33 + the
  Self-Learning Mind Layer), plus four of the follow-on projects:
  **Project 1** (live Jupiter round-trip sell test), **Project 2** (two-way
  Telegram control + copy-address button), **Project 3** (mind-layer P(rug)
  veto — built, veto flag off until authority is earned), and **Project 6**
  (live buy/dump from Telegram — see below).
- **865 tests passing** (`python -m pytest tests/ -q`).
- **Live in production**: 24/7 on the operator's $6/mo DigitalOcean droplet
  (systemd service `meme-intelligence`, repo at `~/meme-intelligence`),
  Telegram alerts arriving on his phone.
- **Branch that matters**: `claude/memecoin-onboarding-yrvjbg` — the
  authoritative tip; the droplet pulls it. (Supersedes
  `claude/session-rules-preferences-kc71bf` from the 2026-07-10 handoff.)
- **LIVE TRADING IS ARMED AND FULLY VALIDATED.** Project 6 is real money
  now: on 2026-07-11 the operator executed a live `/buy` AND a live
  `/dump` (full round trip — buy and sell both confirmed) from a DEDICATED
  fresh Phantom wallet funded with ~$20 CAD (never his main wallet), small
  per-trade caps in the droplet `.env`. It NEVER auto-trades — every trade
  is a button he taps.
- **Wallet intelligence (smart-money, Part 17) is OFF in the monitor**
  (`MEMEINTEL_WALLET_ENABLE_IN_MONITOR=false`): it exhausted the main
  Helius account's monthly free credits and produced only 429 noise.
  Deliberate operator decision — see "Money & keys" below and
  DECISIONS_LOG 2026-07-11. Do not "fix" it back on. **2026-07-20:** the
  credit-gated build is restored and sitting DORMANT (operator request —
  he plans a paid Helius plan). Enable ONLY when he says go:
  `bash deploy/enable-wallet-tracking.sh <paid-key>` (see README
  "Wallet tracking" section; DECISIONS_LOG 2026-07-20).
- **Anthropic AI layer is ON** — the operator turned it back on overnight
  (2026-07-11) after keeping it off to save credits since 2026-07-10. The
  free deterministic screens still run FIRST (credit gate); watch the spend
  with him for a few days. He may toggle it either way — follow his lead.
- **Learning layer is live and accumulating**; report card via `/mind` or
  `mind metrics`.
- **Alert quality overhaul, 2026-07-11** (operator moved his phone from
  HIGH to MEDIUM to see opportunities again, then back to HIGH once he saw
  the junk it let through — walk through DECISIONS_LOG 2026-07-11 in
  order): (a) rug/risk/copycat vetoes now SUPPRESS buy-side alerts instead
  of downgrading to MEDIUM; (b) a configurable liquidity/market-cap floor
  (`MEMEINTEL_ALERTS_OPPORTUNITY_MIN_LIQUIDITY_USD`, off by default) drops
  buy-side alerts on untradeable pools; (c) a young/data-starved token that
  scores AVOID purely from missing categories is no longer permanently
  blacklisted — it gets a paced second look
  (`MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_*`, on by default) instead of
  being excluded from analysis for the life of the process. Mind-layer
  P(rug) veto is now ON (`MEMEINTEL_LEARNING_VETO_ENABLED=true`, authority
  earned at 0.97 rug precision).

## Money & keys (read before touching anything Helius/trading)

Two SEPARATE Helius accounts, on purpose (DECISIONS_LOG 2026-07-11):

| Key | Used by | State |
|---|---|---|
| `MEMEINTEL_HELIUS_API_KEY` (main account) | scanner (wallet-intel when enabled) | **monthly free credits exhausted**; resets monthly; wallet-intel paused meanwhile |
| `MEMEINTEL_EXECUTION_HELIUS_API_KEY` (second account) | trading RPC only (balance reads, send, confirm) | healthy — trading sips a few credits per trade, lasts ~forever |

Why the split exists: the scanner can exhaust an account's server-side
credit budget, and credits are per-ACCOUNT (not per-key), so a trade's
balance read on a shared account gets 429'd no matter how polite the
client-side rate limiter is. The money path must never compete with data
collection. Both `build_wallet_service` and `build_executor` also share
ONE client-side `RateLimiter` per key when they do share an account.

**Hard security rule (never bend it):** the trading wallet's private key /
seed phrase goes ONLY into the droplet `.env` over SSH — never into chat,
never into git, never anywhere else. API keys are lower-stakes secrets
(they guard RPC credits, not funds) but still don't paste them where
avoidable; rotate any that leaked.

## The operator (condensed — full picture in OPERATOR.md)

Not a strong coder; phone-first; runs everything by SSH from his phone.
Walk him through droplet steps ONE command block at a time, tell him
exactly when to `git pull` / restart, and prefer `printf >> .env` style
one-liners over "open nano" (nano sessions have gone wrong twice: a
paste-merge corrupted a line, and an empty-file scare from being in the
wrong directory). His deploy loop, verbatim:

```
cd ~/meme-intelligence
git pull
sudo systemctl restart meme-intelligence
```

## How the project got here (condensed timeline)

1. **Parts 1–33 + Mind Layer** built across prior sessions; deployed;
   ~29-bug hunt; live-feedback alert tuning (interest gate, opportunity
   screens, copycat veto). See the 2026-07-10 version of this file's
   history in git if needed.
2. **2026-07-10, Projects 1–3 built** (sell test, Telegram control,
   P(rug) veto scaffold) + an adversarial review that confirmed and fixed
   3 findings (thin-pool false honeypots, EVM chain-guess mute mismatch,
   command replay after restart) — commit `02bf596`.
3. **2026-07-10, Project 6 built** (operator-requested live buy/dump),
   then an adversarial money-safety review confirmed and fixed 6 findings
   BEFORE arming — the big two: a confirmation-error path that hid the
   transaction signature (double-spend invitation) and unbounded dynamic
   slippage (commit `2957ea2`). Semgrep hygiene pass in `d5ef485`.
4. **2026-07-11, arming Project 6 live** surfaced, in order: `solders`
   missing from the droplet venv (graceful dry-run fallback worked as
   designed; fixed by `pip install -r requirements.txt`), a double-reply
   bug on typed commands (fixed, `85ed00d`), Helius 429s on the balance
   read → diagnosed as the main account's credits being exhausted
   server-side ("max usage reached") → shared one client-side limiter per
   key (`10c6643`) AND added the dedicated trading account
   (`5850616`). **First live buy then succeeded.** Wallet-intel paused
   (operator decision, .env-only change).

## First moves for the next session

1. **Round-trip validation is DONE** (2026-07-11): live `/buy` and live
   `/dump` both executed and confirmed. The bot is trusted for real
   alerts within its caps. Nothing is owed here.
2. **Check the phone experience:** duplicate replies should be gone
   (`85ed00d` is deployed), 429 spam in the journal should be gone
   (wallet-intel off). `/status` should show `trading LIVE`.
3. **When his Helius credits reset (monthly):** wallet-intel can come back
   ONLY per the agreed re-enable criteria — bot profitable + paid plan +
   credit-gating built (only best/alert-worthy candidates get wallet
   lookups). Neither piece alone. See DECISIONS_LOG 2026-07-11.
4. **Watch the trading wallet's spend** with him occasionally
   (`/holdings`, Solscan on the wallet address) — the funded amount
   (~$20) is the real hard cap.

## Anti-goals (as binding as the goals)

- **Never auto-trade.** Live execution exists (Project 6) but a trade
  happens ONLY on an explicit operator button/command. No code path may
  initiate a trade on its own — this is the load-bearing safety invariant.
- **Never touch his main wallet.** Trading uses the dedicated low-balance
  wallet only; its funding IS the risk cap.
- **Never ask for / accept the private key or seed phrase in chat.** It
  lives only in the droplet `.env`.
- **Never spend his money** (API credits, paid tiers, droplet upgrades)
  without an explicit yes. He has ~$20 in the trading wallet and free-tier
  everything else, deliberately.
- **Do not build Project 4 (dashboard)** — discarded. **Do not start
  Project 5 (paid social)** — parked until he asks.
- **Never let the phone become noisy again** — every alert-path change
  must answer "does this protect a decision he can actually make?"
- Don't rewrite what works (Rule 18); one project/spec-part per request
  (Rule 2); keep all tests green (Rules 3/14).

## Update log for this file

- 2026-07-12 — safety checklist on buy-side alerts: only a rug (the rug
  engine's combined `deterministic_risk_veto`) OR an untradeable coin (0/missing
  liquidity or market cap, `_untradeable`) suppresses; every OTHER soft signal
  (mint/freeze authority, sell tax, deployer history, thin-but-real liquidity)
  now ANNOTATES via a "passed X/Y" checklist on the alert instead of dropping
  it. Top-wallet concentration is a note framed "normal for a new launch," never
  a fail. Operator rule: "if it's a rug don't send it; if one thing misses,
  send it and tell me; but never send 0-liquidity/0-mcap junk." Plus operator
  LOOSENED the alert gates via `.env` (security 55 / overall 62 / momentum 55 /
  delivery=medium — see DECISIONS_LOG) so coins actually reach the phone; the
  spec defaults stayed as committed. See DECISIONS_LOG 2026-07-12. 778 → 788 tests.
- 2026-07-12 — massive adversarial bug hunt: 8 fixes across the money path
  (edited-message re-trade, double-tap double-trade, send-cancel signature
  loss, raw-exception loop kill, synchronous retrain stall, torn-FAISS
  crash, retry re-hammering a failing provider, non-finite probe size). All
  fixed with regression tests; see DECISIONS_LOG 2026-07-12. 766 → 778 tests.
- 2026-07-11 — rewritten after the live-trading arc: Project 6 armed and
  first live buy validated; dedicated trading Helius account; shared
  per-key rate limiter; double-reply fix; wallet-intel paused (operator
  decision); Projects 1-3 + 6 adversarial reviews recorded; branch moved
  to `claude/memecoin-onboarding-yrvjbg`; 766 tests. (Superseded by the
  2026-07-12 bug-hunt entry above.)
- 2026-07-10 — rewritten as part of the complete handoff-folder refresh
  (added OPERATOR / ARCHITECTURE / OPERATIONS / ROADMAP / PROJECT_RULES
  docs; interest gate + opportunity screens shipped; 626 tests).
- 2026-07-09 — previous version (457-test era, pre-interest-gate); its
  rules section and module tour moved to PROJECT_RULES.md and
  ARCHITECTURE.md.
