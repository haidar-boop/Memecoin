# Decisions Log

Architectural decisions, spec ambiguities and how they were resolved, and
things intentionally deferred. Per Rule 19 (explain major decisions) and
Rule 20 (ask before major changes) — most of these were flagged to the
user in real time; this is the consolidated record.

## Resolved ambiguities

### 1. Scoring weight conflicts across Parts 10 / 20 / 30 / 31

The spec defined the top-level category weighting differently in four
places as the parts accumulated:
- Part 10: Security 20%, Community 15%, On-chain 15%, Foundation 15%,
  Token 10%, Momentum 10%, Catalysts 10%, Risk 5%
- Part 20: Security 20% (same idea, restated)
- Part 30: Foundation/Security/Community/Blockchain/Momentum/Narrative
  15% each, Timing 10% (flatter, no security priority)
- **Part 31 "Framework Consistency Lock"** explicitly declares itself
  the permanent, locked version — matches Part 30's flatter weighting —
  and states in its own text that future sections must not "randomly
  change scoring weights."

**Resolution:** Part 31 is authoritative. This is what's implemented in
`config/settings.py::ScoringWeights` today: Foundation/Security/
Community/Blockchain/Momentum/Narrative at 15% each, Timing at 10%.

### 2. Security sub-score weighting: Part 4 vs Part 18 vs Part 33

Three slightly different breakdowns appeared across the security-focused
parts. Part 33 ("Security & Rug Detection Intelligence Engine") is the
most detailed and most recently written version, so it was adopted as
canonical: Contract 25%, Liquidity 25%, Distribution 20%, Developer 15%,
Manipulation 15%. Implemented in `SecuritySubWeights`.

### 3. Final classification label set

Multiple label sets appeared (Strong Candidate/Watchlist/Speculative/
Avoid in Part 1; Elite/Strong/Watchlist/Speculative/Avoid in Part 10 and
Part 20 with explicit score bands 90/80/70/60; Exceptional/Promising/
Speculative/Weak/Avoid in Part 30). **Part 20's five-band version was
adopted** since it's tied to explicit numeric thresholds and repeated
most consistently. Implemented as `Classification` enum with
`ClassificationBands` in settings.

### 4. Foundation category input in the master score

Part 31 defines "Foundation" as spanning "project structure, developer
behavior, token design, long-term potential" — language that describes
both the qualitative `FoundationAnalyzer` (meme strength, narrative,
brand) *and* the structural `TokenAnalyzer` (market cap, liquidity,
supply). **Decision:** when both assessments exist, the master score
averages them; when only one exists, that one is used alone. See
`ScoringEngine._foundation_input()`.

### 5. Risk vs. Opportunity as two separate axes (Part 25 / Part 31 lock)

Part 25 introduced "Opportunity Score" and "Risk Assessment" as two
separate numbers rather than one blended score, and the Part 31 lock
formalized this as permanent: **Acceptable Risk** (new, small, unknown
team — reduces confidence, never auto-rejects) vs. **Destructive Risk**
(honeypot, fake liquidity, hidden control — invalidates the opportunity).
This is why `RiskAnalyzer` produces a risk score where *higher = riskier*
(a deliberately different direction/axis from the quality scores) rather
than trying to fold risk into one number with everything else.

### 6. Part 19's two overlapping /100 rubrics (viral score vs. narrative intelligence score)

Part 19 defines a **viral score** (§3: memorability / shareability /
emotional impact / cultural timing / community participation, each /20)
*and* a **narrative intelligence score** (§11: meme strength / cultural
timing / **viral potential** / community creativity / long-term strength,
each /20), and §12 requires the report to show the viral score while the
master framework has exactly one 15% `narrative` category. **Decision:**
the §3 viral score is computed first and feeds the §11 score's
`viral_potential` component (scaled /100 like every sub-score); the two
rubrics share one cultural-timing lens (computed once, never
double-counted in findings/confidence); the §11 narrative intelligence
score is what fills `ScoringEngine.evaluate(narrative_score=...)`. This
is the only reading under which both sections and the single master slot
are all satisfied by one engine (same combine-don't-pick style as
decision #4).

### 7. Part 19 narrative judgment inputs follow the `FoundationInputs` pattern

The §3/§4/§11 components (memorability, meme strength, long-term
strength, ...) are qualitative judgments not computable from market
numbers — per the agent architecture (Parts 13/23) they are produced by
the AI reasoning layer or an analyst. **Decision:** `NarrativeInputs`
mirrors `FoundationInputs` — validated 0-100 slots, `None` = not yet
assessed. Evidence-driven pieces are wired now: participation/creativity
cross-fill from the community engine's creativity sub-score (reuse per
Rule 18; a *confirmed-artificial* community instead zeroes participation
— fake participation is zero participation, and an organic verdict alone
never contributes a score since it says nothing about how *much*
participation exists); sentiment classification from measured sentiment
data; stage-driven timing signals. Sentiment is classified and reported
but never deducted from the narrative score — measured sentiment already
feeds the community engine's loyalty category, and counting one fact in
two master-score categories would skew the Part 31 locked weighting.

### 8. Part 19 narrative risk ladder (§10/§12)

§12 wants a Low/Medium/High narrative-risk label; §10 defines three risk
factors (short-term hype, trend dependency, copycat) with no combining
formula. **Decision:** the three factors are explicit tri-state inputs
(True/False/None — unknown is never safe, Rule 8): zero confirmed among
the assessed = Low, one = Medium, two+ = High; a Saturation/Decline
life-cycle stage escalates one level (§7 names those stages as where
distribution risk lives); with *nothing* assessed the label is Unknown —
except that a late stage alone justifies Medium. Confirmed factors also
deduct from the long-term-strength component (30/20/20 points), since
that is where narrative fragility materializes.

### 9. Part 23's "multi-agent AI design" maps onto the existing engines

Part 23 Section 2 describes seven specialized AI agents (coordinator,
security, blockchain, community, narrative, market, final decision). Four
of those responsibilities — security, blockchain, market, final decision —
are already deterministic analyzers whose scoring Part 31 locks, and the
coordinator is the shared `ResearchPipeline`. **Decision:** the LLM covers
the judgment work the deterministic engines cannot do (the Community and
Narrative analyst agents' qualitative slots, bull/bear reasoning, and the
Section 6 confidence score) and only ever feeds *inputs* into the locked
framework — it never overrides a computed score. Re-implementing the
deterministic engines as LLM calls would trade reproducible, tested logic
for token cost and nondeterminism (Rule 21; Part 23's own final rule:
"the AI is the reasoning layer, the data pipeline is the intelligence
foundation").

### 10. One structured judgment call, not seven

Rather than one API call per Section 2 agent, `AIJudgmentService.judge()`
makes a single structured-output request whose JSON schema carries every
judgment slot (`FoundationInputs` + `NarrativeInputs` + bull/bear prose +
confidence). One call sees the whole Section 3 snapshot (judgments stay
mutually consistent), costs a seventh as much, and validates in one place:
API-level schema enforcement, then range checks, then the Part 16
banned-language guard. A judgment that fails any check — or scores below
the configurable confidence floor — is discarded and the pipeline
continues on deterministic evidence alone (Rules 6/8/9).

### 11. AI enrichment runs after the deterministic chain, then re-scores

`ResearchPipeline` runs the AI pass only after the full deterministic
chain completes, skips it entirely for tokens with destructive security
findings (Rule 10 — never spend tokens on an invalidated coin), and then
recomputes the master score through the same locked weighting with the
new foundation/narrative inputs. Explicit analyst `narrative_inputs`
always win over the AI's. Any AI failure (network, refusal, invalid JSON,
banned language, low confidence) leaves the deterministic result
untouched. The structured-outputs schema cannot carry numeric
minimum/maximum constraints (discovered in live testing) — ranges are
stated in field descriptions and enforced when parsing.

### 12. Part 29 ranking/format derivations, and what was already built

Part 29's §§4-6 (filtering, confirmation, cooldown) were built in Parts
13/15, and §9's daily summary is Part 11's `DailyReport` — Part 29's
build covered only the genuinely new surface (sinks, §7 format, §10
ranking, §§11-12 history/performance). Interpretive choices:

- **§10 ranking components.** The spec fixes the weights (impact 40 /
  confidence 30 / urgency 20 / novelty 10) but not the component scales.
  Impact and urgency derive from the §2 priority level (the spec defines
  priority AS the impact/urgency grading); confidence from evidence
  density (bullet count); novelty from whether this token+type was
  alerted before in this run. Ranking orders dispatch — it never
  suppresses (cooldown and the min-priority filter do that).
- **§7 "Risk Assessment: Low/Medium/High"** derives from the alert's
  priority level for the same reason.
- **§12 performance measure**: score drift between the master score at
  alert time and the token's latest snapshot afterwards, aggregated per
  alert type. Deliberately direction-agnostic: positive drift after
  opportunity alerts = useful; negative drift after risk alerts = the
  alert fired correctly. Outcome labeling belongs to Part 24's learning
  loop; the `alerts.outcome` column is ready for it.
- **External sinks default to MEDIUM+** (§1: information vs signal vs
  event) while the console shows everything; configurable (Rule 17).
- Route strings validate loudly — a typo in a category name raises at
  startup instead of silently sending security alerts nowhere (Rule 6).

### 13. Part 24 grading rules and the self-improvement boundary

- **A prediction is the FIRST snapshot per token** — later snapshots are
  re-assessments and become the outcome measurements instead. Outcome
  windows (1h/24h/7d/30d) prefer a stored snapshot within a tolerance of
  the window target (data the scanner already collected, Rule 10) and
  fall back to a live pair fetch; a token with no tradable pair left is
  recorded as dead at price zero — disappearance IS the outcome.
- **Grading is deliberately three-valued** (correct / incorrect /
  undetermined): +50% best-window makes a positive call correct, −50% or
  death makes it wrong; Avoid grades inverted; Watchlist/Speculative are
  middle calls asserting neither outcome and stay ungraded for accuracy.
  Sideways price action proves nothing and is never force-classified
  (Rule 8). All thresholds env-tunable (`MEMEINTEL_BACKTEST_*`).
- **Section 10's "Rule Adjustment" stays human-in-the-loop.** Weight
  experiments recompute stored category scores under variant weightings
  and report which discriminates winners best — but the Part 31
  Consistency Lock keeps shipped weights canonical, so the system never
  self-modifies. Adopting a change = env override + a
  `record_strategy_change()` journal entry (Section 11's what/why/
  results). This was chosen over auto-tuning deliberately: silent
  self-modification would violate Rule 20 and make Section 14's "never
  change rules without recording it" unenforceable.
- Snapshots gained price/liquidity/mcap/regime columns via an in-place
  migration (`Storage._migrate()`, Rule 18) — old databases keep working
  and simply lack price outcomes for their pre-migration rows (honest
  gap, reported as unmeasurable rather than guessed).

## Deferred, with reasons

### Social data collectors (Parts 5, 19, and the "community" gate everywhere)

X/Twitter's official API starts at $200/mo for read access. This is a
real budget decision, not a technical blocker, and was explicitly left to
the user rather than silently built around. Cheaper aggregator APIs
(e.g. LunarCrush-style) exist as a middle option.

**Status: user decision made (2026-07).** LunarCrush's API tier priced at
~$5/day was judged too expensive for an unproven system. The chosen path:
**free CoinGecko community data now, upgrade to LunarCrush if the bot
proves itself.** Implemented in
`CoinGeckoClient.get_community_profile()` (contract-address lookup):
telegram members, community sentiment votes, and reddit activity — at $0,
sharing the client and rate budget the system already had. What this
covers vs. not:

- Covered: telegram size, positive-sentiment percent, reddit
  subscribers/posts/comments (only trusted when a real subscriber base
  exists — zeros from an untracked subreddit stay unknown, Rule 8).
- Not covered (stays honestly "no data"): Twitter followers/engagement,
  Discord, bot detection, growth rates. Coverage/confidence report the
  gap; a paid aggregator plugs into the same `CommunityProfile` later
  without engine changes.
- Coverage caveat: CoinGecko only reports tokens it has listed — very new
  launches return "not listed" (treated as a data gap, not an error).

### EVM wallet intelligence

Part 17 was built Solana-first because both provided API keys (Helius,
Birdeye) are Solana-native, and Solana is also where new meme launches
concentrate. Extending to EVM chains would need an Alchemy (or similar)
key and a parallel collector — the `WalletIntelData` / `WalletHolding` /
`TokenTrade` models are chain-agnostic already, so this is additive work,
not a redesign.

### Deep developer launch-history (Part 18 §5)

GoPlus reports "honeypots by the same creator" as a count, which is used
today. Full launch history (all previous tokens by a given deployer
wallet, success/failure record) needs either a paid indexer or custom
on-chain crawling that neither Helius nor Birdeye's free tiers expose
directly. Flagged as a gap rather than approximated.

### ~~AI/LLM reasoning layer (Parts 13 §4, 22 §4, 23)~~ — RESOLVED (Part 23 built)

Built in `ai/reasoning.py` once the user supplied an Anthropic API key.
`ANALYST_SYSTEM_PROMPT` is the system prompt exactly as planned. See
resolved ambiguities #9-11 for the architectural decisions. Remaining
Part 23 gaps: Sections 7-8 (memory/feedback loop) activate with Part 24;
the community judgment slots stay thin until the social collectors exist
(the model is told the data is missing and lowers confidence — verified
live: it reported 38/100 confidence on a snapshot with no social data).

### Telegram / Discord alert delivery (Part 29)

`NotificationEngine` accepts a list of `AlertSink` objects; only
`ConsoleSink` is implemented. Adding `TelegramSink` / `DiscordSink` is a
small, additive change once a bot token / webhook URL exists. Not done
yet — needs the user to create a Telegram bot (instructions were given
earlier in conversation but not yet completed on the user's end as of
this handoff).

### Backtesting / outcome tracking (Part 24)

Every relevant table (`snapshots`, `wallet_sightings`, `security_facts`)
is being populated *specifically* so that Part 24, when built, can join
predictions against actual outcomes without needing new instrumentation.
The join logic, outcome labeling (winner/loser/rug), and weight-tuning
loop itself are not written.

### Bug-hunt fix pass across Parts 19/23/24/29 + community collector

A multi-agent adversarial bug hunt over the recently-built modules
(narrative engine, AI reasoning, backtesting, alerts, CoinGecko
collector) surfaced 15 concrete defects, mostly Rule 6/8 violations
(fabricated scores, unhandled malformed provider data, silent data
loss). Fixed directly, all 362 tests green:

- **`analyzers/common.py`**: `SubScore` gained an opt-in
  `requires_signal` flag. Deduction-style analyzers (start at 100,
  subtract) keep the old default; `NarrativeAnalyzer._assess_long_term`
  now uses it so Section 10 risk flags alone (no `long_term_strength`
  judgment) can no longer fabricate a "perfect 100" score out of thin
  air — mirrors the existing organic-growth guard in
  `_assess_participation`.
- **`analyzers/narrative_analyzer.py`**: `NarrativeInputs.catalysts` is
  now validated at construction (fail-fast instead of a deferred
  `AttributeError` inside `summary()`); the `community_creativity_proxy`
  fact is deduped so it's tallied once, not twice, when both the
  participation and creativity components fall back to it.
- **`ai/reasoning.py`**: `score()` rejects JSON booleans explicitly
  (`bool` is an `int` subclass in Python); `bull_case`/`bear_case` are
  validated as lists before iteration instead of raising an uncaught
  `TypeError` that escaped `judge()` and crashed the whole pipeline run.
- **`ai/prompts.py`**: `check_language` now ignores a banned phrase when
  a negation word (not/no/never/nothing/without/...) appears in the
  preceding few words — the system prompt explicitly asks the analyst to
  write cautionary disclaimers ("no guarantee against a rug pull", "not
  risk-free"), and those were being flagged as hype, silently discarding
  otherwise-valid AI judgments.
- **`collectors/market_data.py`** / **`core/errors.py`** /
  **`collectors/base.py`**: `CollectorError` now carries a `status_code`
  so "not listed" detection no longer depends on a substring match
  against the error message; unlisted-token 404s are now negatively
  cached (previously re-hit CoinGecko every recheck cycle); the
  community cache key is lowercased (the request itself keeps original
  casing) so checksummed vs. lowercase addresses no longer fragment the
  cache; `get_majors` now sends the configured demo API key like
  `get_community_profile` already did; `positive_sentiment_percent` is
  clamped to `None` outside 0-100 (was crashing narrative assessment
  with a raw `ValueError`); `user_content_per_day` no longer substitutes
  0 for a genuinely missing reddit posts/comments field.
- **`workflow/pipeline.py`**: narrative `ValueError`s are now caught
  alongside `InsufficientDataError` (defense in depth even with the
  collector-level fix above); `_enrich_with_ai` only builds a foundation
  score from `community_quality` when the AI judgment actually supplied
  at least one foundation slot — previously an all-null AI judgment
  still fabricated a foundation score from the community score alone,
  double-counting it and moving the master score with zero new
  evidence; an artificial community's score is now excluded from
  `community_quality` the same way `NarrativeAnalyzer` already excludes
  it.
- **`alerts/notification_engine.py`**: the per-token cooldown key now
  includes alert priority — `events_from_security_changes` deliberately
  emits one event per severity for the same `alert_type`, and without
  priority in the key a CRITICAL alert could be silently swallowed by an
  earlier (or same-batch) MEDIUM one, with no way to ever re-fire since
  the baseline had already advanced. Most `AlertEvent` emissions that
  omitted `master` from their `scores` dict now include it (needed for
  the `alert_performance()` score-drift measurement below).
- **`database/storage.py`**: `record_alert`'s score extraction no longer
  uses `or` (which treated a legitimate `0.0` score as missing) and now
  checks every score key the automation rules actually use, so
  risk-only alert types (`emergency_review`, `whale_exit`,
  `insider_risk`, ...) get a real `score_at_alert` instead of a
  permanent `NULL` that silently excluded them from
  `alert_performance()`/`alerts_with_drift()`.
- **`alerts/sinks.py`** / **`collectors/base.py`**: `BaseCollector`
  gained an optional `redact` tuple of secret substrings, scrubbed out
  of every raised error message before it reaches a log line.
  `TelegramSink`/`DiscordSink` register their bot token / webhook
  URL(s) — previously any delivery failure (timeout, 429, 4xx) logged
  the live credential in plaintext at WARNING/ERROR.
- **`workflow/controller.py`**: `ContinuousScanner` now applies the same
  `hasattr(community_client, "get_community_profile")` guard
  `DailyRoutine` already had, so a legacy get-majors-only client
  degrades gracefully instead of crashing the scanner on the first
  token.

### Pump.fun data source selection (Part 32.5 Section 3)

Four candidate sources were researched and the two keyless ones probed
live (2026-07-08) before building — Rule 8, data before assumptions:

- **PumpPortal WebSocket** (`wss://pumpportal.fun/api/data`) — chosen as
  the discovery feed. Verified live: keyless, free for
  `subscribeNewToken`/`subscribeMigration`, real launch events within
  seconds (16 launches captured in a 40s probe). Event-driven beats
  polling here (spec §6, Rule 10), and it follows the established
  free-source-first doctrine (see the "cheap aggregator" decision).
  Constraint honored in code: PumpPortal allows ONE data connection —
  the client keeps a single background listener with reconnect/backoff.
- **Pump.fun frontend API** (`frontend-api-v3.pump.fun`) — chosen for
  per-token traction rechecks only, never bulk polling. Verified live:
  keyless today, but it is an *unofficial* surface whose v1/v2 hosts
  were deprecated within about a year each — so the base URL is
  configuration, every field is optional, and failures degrade to data
  gaps (Rules 6/8/9/17).
- **Moralis / Bitquery / Solana Tracker** — official keyed products with
  free tiers; rejected for now as unnecessary (both chosen sources are
  free and keyless) but they are the natural upgrade path if the
  unofficial frontend API breaks.
- **DexScreener** — verified live that it indexes bonding-curve tokens
  pre-graduation (dexId `pumpfun`, volume populated, liquidity honestly
  null). This makes the existing `MarketDataService` the *confirmation*
  source: a launch enters the pipeline only after an independent
  provider returns a real pair, which is spec §2 ("the AI must never
  treat discovery as confirmation") enforced structurally rather than
  by convention.

Design decisions of note: launch events carry no USD conversion, so all
launch-side market caps stay SOL-denominated and the growth gate
compares SOL to SOL (converting with an assumed SOL price would
fabricate data — Rule 8); every §8 promotion gate requires data to pass
(a missing metric fails the gate, it is never assumed); the whole stage
is off by default and opt-in via `MEMEINTEL_PUMPFUN_ENABLE_IN_MONITOR`
or `monitor --pumpfun` (Rule 11 — it adds a WebSocket plus per-launch
rechecks). The live smoke test validated the funnel: of 16 real
launches, basic filtering rejected one whose creator bought 46.2% of
supply at launch, and zero minutes-old tokens were promoted — exactly
the "most launches should be filtered out" behavior §3 demands.

### Metered layers wired into the continuous scanner (Parts 17/23 gap-close)

`MEMEINTEL_WALLET_ENABLE_IN_MONITOR` and `MEMEINTEL_AI_ENABLE_IN_MONITOR`
had existed in settings since Parts 17/23 but were never consumed — the
`monitor` command never built or passed a wallet/AI service, so smart-
money analysis and AI judgments could not run in the 24/7 loop at all.
Now `_cmd_monitor` builds both services when their flag is on and their
keys exist (a set flag with missing keys prints a note instead of
failing silently — Rule 13), and `ContinuousScanner` treats the flags as
authoritative: a wired service with the flag off is dropped with a log
line, so metered spend can never happen by accident (Rules 10/11). Both
default off; enabling smart money in production is one Helius (free
tier) key plus one flag in `.env`.

### Dead-token post-mortems replace warning spam (first live-feedback tuning)

First real-world operation (2026-07-08, DigitalOcean droplet) produced
alert fatigue of a specific shape: tokens pumped, scored 80-90 on
pump-window data, got tiered into the watchlist, then rugged within
minutes — and every recheck of the corpse fired a HIGH risk-warning
plus a HIGH score-drop review. All technically correct, none
decision-relevant: alerts exist to protect decisions (Part 29 S1), and
no entry/exit decision remains once liquidity has collapsed.

Change: liquidity below `alert_engine.dead_liquidity_usd` (default
$500, env `MEMEINTEL_ALERT_ENGINE_DEAD_LIQUIDITY_USD`) marks the token
dead. One MEDIUM `token_death` post-mortem replaces the warning/drop
pair (still recorded for Part 24 grading; silent on a HIGH-filtered
phone), opportunity/momentum/accumulation rules are suppressed for the
corpse (pump artifacts, not signals), and the controller archives the
token immediately instead of re-tiering it on its inflated pump score.
Boundaries kept honest: a confirmed destructive finding still fires
CRITICAL (holders need it); unknown liquidity is NOT death (Rule 8);
below-minimum-but-alive liquidity (e.g. $3k) keeps its HIGH warning —
that deterioration is still decision-relevant.

## Notable implementation choices (Rule 19)

- **Python 3.11 + asyncio** over Node.js (both allowed by spec): the
  workload is I/O-bound API fan-out, and the eventual backtesting phase
  benefits from Python's data tooling.
- **SQLite** over Postgres for now: "simple, reliable solutions are
  preferred over clever" (Rule 21). `database/storage.py` is the only
  module that knows the backend, so swapping later is contained.
- **One shared `ResearchPipeline`** (Part 13) instead of separate
  analysis code paths per entry point (CLI/daily/continuous scanner) —
  guarantees identical scoring everywhere and was a mid-build refactor
  once duplication started to appear (Rule 18: extend, don't duplicate).
- **Coverage/confidence reporting is universal.** Every analyzer that
  combines weighted sub-scores reports what fraction of the framework it
  actually had data for, and every report/alert surfaces that number.
  This was treated as non-negotiable under Rule 8, even where the spec
  didn't explicitly ask for it — the spec's own doctrine ("unverified is
  not a pass," Part 31 §6) implies it.
