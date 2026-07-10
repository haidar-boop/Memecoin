# Session Handoff — Current State as of 2026-07-10

> **What this is:** the "you just woke up in this project — here's where
> things stand" briefing. The durable knowledge lives in the sibling docs
> (ARCHITECTURE.md for the mental model, OPERATIONS.md for the runbook,
> OPERATOR.md for who you're working with, ROADMAP.md for what's next,
> STATUS.md for part-by-part build state, DECISIONS_LOG.md for every "why").
> This file is the snapshot that goes stale first — **update it at the end
> of any session that changes state.** (Earlier versions of this file also
> carried the rules and the module tour; those now live in
> PROJECT_RULES.md and ARCHITECTURE.md respectively.)

## Status snapshot

- **Everything in the spec that can be built is built**: Parts 1–33 plus the
  Self-Learning Mind Layer. Only the web dashboard (now ROADMAP #4) and
  creator launch-history intelligence (blocked — no free data source)
  remain unbuilt.
- **626 tests passing** (`python -m pytest tests/` — pytest.ini is `-q`).
- **Live in production**: 24/7 on the operator's DigitalOcean droplet
  (systemd service `meme-intelligence`, repo at `~/meme-intelligence`),
  Telegram alerts arriving on his phone at HIGH+ priority.
- **Branch that matters**: `claude/session-rules-preferences-kc71bf` — the
  authoritative tip; the droplet pulls it. (History note: it was reset onto
  `claude/handoff-folder-review-fuu9dq`, which superseded the older
  Parts-1–18 line. Never build on stale branches.)
- **Anthropic API key is OFF** — the operator's deliberate choice to save
  credits (2026-07-10). The system is designed to run fully without it;
  do not "fix" that.
- **Learning layer is live and accumulating**: 449 coins in analog memory on
  day one; the classifier trains as outcomes resolve. Report card:
  `mind metrics`.

## How the project got here (condensed timeline)

1. **Parts 1–33 built** across prior sessions: collectors → analyzers →
   scoring engine → workflows → alerts → wallet intel → AI layer →
   backtesting → pump.fun funnel. All spec text preserved verbatim in
   `FULL_PROJECT_HANDOFF.md` + `next_steps/`.
2. **Self-Learning Mind Layer** built to a user-provided spec (FAISS analog
   memory, LightGBM warm-start, HDBSCAN archetypes, hard-signal rug engine,
   accuracy-weighted ensemble, drift-triggered rebuilds, deployer
   blacklist). Opt-in, error-isolated, never trades.
3. **Deployed** to the droplet and verified live (clean cycles, ~440 MiB).
4. **Bug hunts** (operator-requested, agent-capped): ~29 real bugs found and
   fixed — inverted AI veto, slow-rug blind spot, lost-alert delivery
   accounting, unbounded caches, cross-process ensemble clobber,
   base-vs-quote pair mixups, calibration crash, and more. Every fix has a
   regression test and a DECISIONS_LOG / commit-message record.
5. **Live-feedback alert tuning** — the operator's phone kept getting junk;
   the fixes landed in this arc (understand it before touching alerts):
   - **Dead-token post-mortem**: collapsed pools get ONE archive notice, not
     endless HIGH warnings.
   - **Strong-candidate tier**: genuinely strong fresh launches can reach a
     HIGH-filtered phone honestly (community named as unverified).
   - **Credit gate**: free deterministic checks before every paid AI call.
   - **Interest gate** (commit `42aa271`, 2026-07-10): protective alerts
     demote to LOW on tokens never recommended to the operator.
   - **Opportunity screens** (commit `76b79fc`, 2026-07-10): the rug screen
     now runs with or without an AI key (it was silently AI-gated — rug
     pulls fired HIGH unscreened while his key was off), plus a new copycat
     veto (same symbol/name as a coin with ≥$100k and ≥10× the liquidity →
     downgraded, original named).

## The operator's complaints → what resolves them

| Complaint (his words) | Fix | Status |
|---|---|---|
| "high risk shitholes… don't even send it to me" | token-death rule + interest gate | Shipped; he should confirm the phone went quiet |
| "It's still finding and sending me bullshit… rug pulls" | interest gate + decoupled rug screen | Shipped 2026-07-10 |
| "stupid coins that are… duplicates of another good coin" | copycat veto | Shipped 2026-07-10 |
| "don't waste my credits" | credit gate; key currently off; advice given: keep it off for now | Standing doctrine |

## First moves for the next session

1. **Ask how the alerts look.** The two 2026-07-10 fixes should have made
   his phone quiet except for vetted opportunities. If junk persists, get
   the screenshot; every veto/downgrade now logs its reason, so trace that
   specific coin through the log before changing anything.
2. **Confirm the droplet actually pulled** commits `42aa271` + `76b79fc`
   (he was given the update block; verify via the journal or by asking).
3. **Ask for `mind metrics` output** once a day or two of outcomes have
   accumulated — read the report card with him and decide ROADMAP #3
   readiness (and probability calibration from the backlog).
4. **Then start ROADMAP #1** (Jupiter sell-simulation) when he says go —
   it is fully specced in ROADMAP.md.
5. Housekeeping when convenient: Helius key rotation (it appeared in a chat
   screenshot), droplet kernel reboot (`sudo reboot`).

## Anti-goals (as binding as the goals)

- **Never** add trading/execution of any kind.
- **Never** spend his money (API credits, paid tiers, droplet upgrades)
  without an explicit yes.
- **Never** spawn more than 3–5 subagents in a session (his hard cap).
- **Never** let the phone become noisy again — every alert-path change must
  answer "does this protect a decision he can actually make?"
- Don't rewrite what works (Rule 18); don't build two roadmap items at once
  (Rule 2).

## Update log for this file

- 2026-07-10 — rewritten as part of the complete handoff-folder refresh
  (added OPERATOR / ARCHITECTURE / OPERATIONS / ROADMAP / PROJECT_RULES
  docs; interest gate + opportunity screens shipped; 626 tests).
- 2026-07-09 — previous version (457-test era, pre-interest-gate); its
  rules section and module tour moved to PROJECT_RULES.md and
  ARCHITECTURE.md.
