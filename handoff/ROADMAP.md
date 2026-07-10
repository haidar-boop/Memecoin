# Roadmap — The 5 Major Upgrades (agreed with the operator, 2026-07-10)

> These five were proposed to the operator and he adopted them as the main
> plan. Build them **in this order, one per request** (Rule 2) unless he
> redirects. Each section says what it is, why it earns its place, exactly
> where it hooks into the existing code, and what "done" means. Items 1–4
> are free; only #5 costs money and it is deliberately last.

---

## 1. Live sell-simulation rug check (Jupiter round-trip) — FREE, build first

**What:** Before any HIGH opportunity alert on a Solana token, ask Jupiter's
public quote API for a real round-trip: quote buying ~$50 of the token, then
quote selling it back. A token that cannot be sold back (no route, absurd
price impact) is a rug regardless of how clean its contract looks.

**Why #1:** It is the single strongest rug signal that exists, the API is
free with no key, and the rug engine already has the seam waiting.

**How it hooks in:**
- New collector `collectors/jupiter_data.py` (`JupiterClient` on
  `BaseCollector` — rate limiting/retry/timeout for free). Quote endpoint:
  `https://quote-api.jup.ag/v6/quote` (in/out mint, amount, slippage).
- The rug engine already exposes `unsellable_override` on
  `RugEngine.assess()` (`learning/rug_engine.py`) — a live "cannot sell"
  result plugs in there and fires the 30-point `unsellable` signal.
- Call site: `ContinuousScanner._deterministic_risk_veto()`
  (`workflow/controller.py`) — run the round-trip ONLY for gate-passing HIGH
  candidates (Rule 10/11: they are rare), cache the verdict per token like
  the copycat cache (`_BoundedKeySet`), never cache an outage.
- Config group (Rule 17): `MEMEINTEL_JUPITER_*` — enabled flag, probe
  amount USD, max acceptable round-trip loss %, timeout.
- Rule 8 discipline: no route data / API down = UNKNOWN → no signal fires,
  never a pass or a veto by itself; log the gap.

**Done when:** a honeypot-style fixture is vetoed with "sell simulation
failed" named in the alert reasons; an outage neither vetoes nor crashes;
tests cover quote parsing, round-trip math, veto wiring, and outage
behavior; suite green.

---

## 2. Two-way Telegram control — FREE, biggest usability win

**What:** The bot already *sends* to Telegram; make it *listen*. Commands
(only from the configured `chat_id` — reject everyone else):
- `/status` — scanner health, last cycle stats, provider health
- `/why <address>` — why a coin alerted or was vetoed (from journal/log)
- `/holding <address>` / `/unhold <address>` — operator marks a coin he
  actually bought → permanent operator interest (full-priority protective
  alerts + tighter watching); stored in the DB
- `/watchlist`, `/mind` — top tracked coins; mind-layer report card
- `/mute <address>` — stop all alerts for a token
- 👍/👎 inline buttons on each alert → recorded as operator feedback

**Why #2:** The operator runs everything from a phone; today that means SSH.
This removes SSH from daily life AND produces labeled feedback.

**How it hooks in:**
- Long-poll `getUpdates` as an asyncio task inside the monitor
  (`workflow/controller.py` starts it like the PumpPortal listener; reuse
  the collector machinery — the bot token is already in settings).
- `/holding` writes an operator-interest flag in storage
  (`database/storage.py` — new small table) that
  `_operator_interest()` checks FIRST, before alert history.
- 👍/👎 lands in the alerts table (`outcome` column exists) and may bias the
  learning layer only as an advisory signal — operator opinion is not a
  measured outcome (Rule 8); keep it out of ground-truth labels.
- Config: `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED` (default off until he opts
  in), poll interval.
- Security: treat all message text as untrusted; never echo secrets;
  answer only the configured chat id.

**Done when:** each command answers in <5s from the phone with no SSH, a
stranger's chat id gets silence, and the monitor loop is provably unblocked
by a Telegram outage (task isolation test).

---

## 3. Mind-layer P(rug) as a live alert veto — FREE, unlocks the learning

**What:** The learning layer currently *watches* (evaluate_coin verdicts are
recorded, accuracy is tracked) but has no vote. Once its measured accuracy
clears a bar, its P(rug) becomes screen #3 on HIGH opportunities.

**Why #3 and not sooner:** Rule 8 — the veto must EARN its authority with
measured accuracy, not vibes. The metrics machinery to prove it already
exists (`mind metrics`, ensemble accuracy tracking).

**How it hooks in:**
- Extend `_deterministic_risk_veto()` in `workflow/controller.py`: when
  `self._learning` is present, call the ensemble for P(rug); veto if
  `p_rug >= threshold` AND the ensemble's measured rug-verdict accuracy ≥
  floor with ≥ min samples (read from `learning/metrics.py`).
- Abstention (cold start, low coverage, drift rebuild in progress) = no
  veto — never block on an unproven or absent opinion.
- Config: `MEMEINTEL_LEARNING_VETO_ENABLED` (default off),
  `_MIN_P_RUG`, `_MIN_ACCURACY`, `_MIN_SAMPLES`.
- Before enabling in production: ask the operator for `mind metrics`
  output and check the report card together (this was the standing plan —
  "wait a day or two, then read the report card").

**Done when:** a synthetic high-P(rug) coin is vetoed with the probability
and accuracy named in the reason; an untrained/abstaining ensemble changes
nothing; flag defaults off; suite green.

---

## 4. Read-only web dashboard — FREE, the deferred spec item

**What:** One password-protected, phone-friendly page served from the
droplet: scanner health + last cycle, watchlist by tier, recent alerts with
reasons, mind-layer report card, provider health. **Read-only** — no
buttons that change anything (never-trades doctrine; no secrets displayed).

**Why #4:** It's the only unbuilt spec deliverable that's feasible today,
and for a phone operator it replaces most remaining SSH. It ranks below
1–3 because it improves *visibility*, not *decisions*.

**How it hooks in:**
- Small FastAPI (or stdlib) app reading the same SQLite (WAL — safe
  concurrent readers) + `learning_state/`; new `dashboard/` package
  (Rule 4) + `deploy/` systemd unit; bind localhost + simple token auth via
  env (`MEMEINTEL_DASHBOARD_TOKEN`), document an SSH-tunnel/nginx option.
- No new write paths to the DB. Renders the same data the CLI commands
  print (`watchlist`, `alerts`, `mind metrics`, provider health).
- Keep the droplet budget in mind: it must idle near-zero CPU/RAM.

**Done when:** he can open it on his phone, see why the last alert fired,
and nothing on the page can mutate state; unauthenticated requests get 401.

---

## 5. Real social intelligence (Twitter/X via paid aggregator) — PAID, last

**What:** Fill the biggest data gap: Twitter/X engagement, growth rates, and
bot detection for the community/narrative scores, via LunarCrush (or
equivalent) — roughly $25–30/month at the useful tier.

**Why last:** It's the only item with a monthly bill, and the operator's
standing decision is "only once the bot proves itself." Do not start this
without his explicit go-ahead on the cost.

**How it hooks in:**
- New collector `collectors/social_data.py` behind
  `MEMEINTEL_LUNARCRUSH_API_KEY` (absent = layer off, exactly like the AI
  key — Rule 9 graceful degradation).
- Feeds the already-built consumers: `CommunityProfile` inputs
  (`analyzers/community_analyzer.py` — engagement/growth/bot detection
  slots exist and currently sit honestly unknown), narrative engine
  (Part 19 social-momentum inputs), and the AI snapshot.
- Strict Rule 11 budget: cache aggressively, poll only tracked/candidate
  tokens, never the firehose.

**Done when:** community coverage rises on fresh tokens (less "community
unverified"), bot-detection actually fires on a known-astroturfed fixture,
and the monthly request volume fits the plan's quota with headroom.

---

## Backlog (not in the 5, keep visible)

- **Probability calibration for the mind layer** — deferred until enough
  resolved outcomes accumulate (see DECISIONS_LOG).
- **EVM wallet intelligence** (Alchemy or similar) — Part 17 is Solana-only.
- **Creator launch-history intelligence** — still BLOCKED: no free data
  source found (paid options exist; revisit alongside #5).
- **Helius key rotation** — operational hygiene, see OPERATOR.md /
  OPERATIONS.md.
- **Backtest-driven threshold tuning** — the weight-experiment machinery
  exists (Part 24); closing that loop is a natural post-#3 step.
