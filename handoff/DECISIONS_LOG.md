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
canonical. Its Section 11 literal weighting is **Contract 25%, Liquidity
20%, Developer 20%, Distribution 20%, Social/Manipulation 15%**, now
implemented exactly in `SecuritySubWeights`.

Correction (Parts 20-33 verification pass): an earlier version of this
entry recorded "Liquidity 25% / Developer 15%", and the shipped code
matched that — but neither figure appears in Part 33 Section 11, which
says Liquidity /20 and Developer /20. That was an undocumented drift
(the Part 33 handoff note even claimed the code already used 20/20). The
code has been corrected to the literal spec (Rule 1) and a test now locks
the five values so the drift cannot silently recur.

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

### AI verification of gate-passing opportunities (Part 32.5 S8 middle mode)

User request from live operation: "after it passes the strict gate
thing, use AI to fully verify." This is Part 32.5 Section 8 verbatim
("the AI should not perform expensive analysis on every token; a token
should receive deeper analysis only after meeting initial
requirements"), so it shipped as a first-class mode:

- `MEMEINTEL_AI_VERIFY_OPPORTUNITIES` (default **true**; activates when
  the Anthropic key exists). When the deterministic chain fires a
  `high_priority_opportunity` (ALL review gates passed with data), the
  scanner runs exactly one AI judgment, re-scores through the locked
  weighting, and re-runs the same gates on the enriched result:
  - judgment holds the score up -> the alert dispatches annotated with
    the AI's confidence and its strongest bear-case point (an alert must
    never read as unconditional endorsement — Part 23 doctrine)
  - judgment knocks the score below a gate -> the high-priority alert
    simply never fires (logged, snapshot records the honest lower score)
  - judgment unavailable/discarded -> deterministic evidence stands
    (Rule 9 — the AI can veto by evidence, its absence cannot)
- Distinct from `MEMEINTEL_AI_ENABLE_IN_MONITOR` (default false), which
  judges every analyzed token. Verification-only costs ~nothing: gate
  passers are rare (the community gate alone requires real social data).
- Mechanically: no new pass/fail logic was invented — verification is
  "re-run the existing gates on AI-enriched scores" (Rule 18/21).

### Full adversarial bug hunt across the whole codebase (2026-07-08/09)

At the user's request, ran a comprehensive multi-agent bug hunt (9
parallel finders, each with an independent adversarial verifier)
covering every module, weighted toward the recently-built Pump.fun
integration and its controller wiring. The spend-limit interruptions
mid-hunt meant several batches of raw findings never reached automated
verification; those were triaged and fixed directly rather than
re-spending on more agent verification passes. In total this fixed:

**Critical:** an out-of-range/NaN/Infinity timestamp from any external
API (pump.fun frontend, DexScreener, Helius) could raise OverflowError/
ValueError uncaught by any error hierarchy, killing the whole scanner
process; HeliusClient's API key was never registered with the redact
mechanism (Rule 16); NaN silently passed nearly every positivity/weight-
sum validation check in settings.py (`nan <= 0` and `nan >= X` are
always False); bool env parsing silently coerced typos to `False`.

**Major (14 fixes):** PumpPortal reconnect backoff reset before proving
the connection healthy (could hammer the server at 1/sec forever);
launch-tracking capacity was checked before expiring stale entries;
READY candidates got expired on the same TTL as PENDING ones,
contradicting the module's own retry guarantee; `_seen` was marked on
attempt rather than successful analysis, letting the pump.fun path
silently drop a candidate the regular pool-discovery path had merely
tried and failed on; a transient full market-provider outage was
indistinguishable from a token's pairs genuinely disappearing, so
outages archived healthy watchlist tokens; `MEMEINTEL_AI_VERIFY_
OPPORTUNITIES=false` was ignored whenever `enable_in_monitor` was on;
a persistent gate-passing token got re-verified with a fresh paid
Claude call on every watchlist recheck forever; `update_watchlist`'s
separate SELECT-then-INSERT/UPDATE could IntegrityError once two
processes started sharing the database (WAL mode, added earlier)
concurrently — replaced with an atomic UPSERT; alert dispatch stamped
cooldown before delivery was attempted, so a total sink outage
permanently lost the alert, and one sink raising aborted the rest of
the batch; `cross_check_liquidity` assumed the pair being verified
always came from `providers[0]`, so after failover it could ask a
provider to confirm its own data; the `has_foundation_evidence` guard
(added in an earlier fix pass) was never mirrored for the parallel
narrative path, so an all-null AI judgment still fabricated a narrative
score from the community-creativity fallback alone; `monitor` leaked
the Telegram/Discord/Anthropic HTTP sessions on shutdown.

**Minor:** `ViralCatalyst` validated only its description, not that
`probability`/`impact` were actual `CatalystLevel` enum members (same
deferred-crash class already fixed once for catalysts); AI judgment
parsing accepted a non-string `narrative_summary`; a Helius
`get_recent_transfers` failure discarded the "helius" source
attribution even when the preceding `get_top_holders` call had already
succeeded; `get_watchlist(include_archived=True)` sorted the tier
column as raw TEXT, ranking archived tokens ahead of Tier 1;
`_migrate()`'s `ALTER TABLE` could race between two processes on first
startup after an upgrade; `PumpPortalClient.close()` could swallow the
calling task's own cancellation.

444 tests green (33 new, spanning all of the above).

### Strong-candidate HIGH alert tier for fresh launches (live-feedback tuning)

Second round of live-operation feedback: the operator was getting only
HIGH risk warnings, never the positive opportunity signals, and asked
why the bot "only finds rug pulls." Root cause was a design/config
interaction, not a detection failure:

1. Every positive opportunity signal (`early_opportunity`, `momentum`,
   `smart_money_accumulation`) is MEDIUM priority by design, because for
   a fresh pump.fun-era token the community gate can never be verified
   (CoinGecko doesn't list it for days), so the "all gates verified"
   HIGH `high_priority_opportunity` is unreachable.
2. The operator had earlier raised `external_min_priority=high` to cut
   alert-fatigue noise — which asymmetrically deleted every MEDIUM
   positive signal while keeping every HIGH warning.

Observed in their own data: HUHCAT was scored 86 (a strong provisional
opportunity) then dropped to 50; they only received the HIGH score-drop
warning, never the entry signal.

Fix (chosen by the operator over "just lower the filter"): a new
`strong_candidate` alert at HIGH priority. It fires when a token clears
a raised overall bar (`AlertThresholds.strong_candidate_overall`,
default 88, must be >= `overall`) with every MEASURABLE gate passing and
the ONLY unverified gate being community. This lets a genuinely strong
fresh launch reach a HIGH-filtered phone while staying honest (the
alert names community as unverified — Rule 8; it is not the full
"every gate verified" tier). It only fires when community data is
genuinely absent: a present-but-weak or artificial community still
fails the community gate and suppresses the alert. Wired into market
cross-verification, the Part 32.5 §8 AI-verification trigger, and the
"discoveries" delivery channel. Threshold is config (Rule 17), so the
operator can raise it for fewer/stronger alerts.

### Interest gate: protective alerts demote to LOW on never-recommended tokens (2026-07-10)

Third round of live-operation feedback: even with the token-death floor
and the strong-candidate tier in place, the operator's phone kept
receiving HIGH `risk_warning` and HIGH `score_drop_review` alerts on
dying pump.fun garbage (liquidity $2,700–$9,600, one wallet holding
65–97%, top-10 holding ~99.9%) — tokens above the $500 dead floor but
walking dead, and tokens the operator had never been told about, let
alone bought. Verbatim complaint: "It's still finding and sending me
bullshit… it's also giving me rug pulls."

Root insight (Part 29 S1 — alerts exist to protect decisions): this
system never trades, and the operator only *learns about* a token when
it earns a HIGH opportunity alert (`high_priority_opportunity` /
`strong_candidate`). A protective alert — risk warning, score drop,
emergency, whale exit, insider risk, fake community, security change,
death post-mortem — on any other token guards no decision the operator
could possibly have made. It is internal research telemetry, not
actionable intelligence.

Change (`alerts/notification_engine.py` + `workflow/controller.py`):

- `gate_events_by_interest()` demotes the protective alert types above
  to LOW priority when the token has no *operator interest*. LOW is
  below every external sink's minimum priority, so the phone stays
  silent; the console still prints them and alert history still records
  them (Rule 13, Part 24 grading unaffected).
- Interest = a HIGH opportunity alert was previously delivered for the
  token (checked against alert history, fail-OPEN on storage errors so
  an error can never silently suppress a warning — Rule 6), OR a HIGH
  opportunity alert fires in the same batch (contradictory signals on a
  just-recommended token both arrive at full priority).
- MEDIUM `early_opportunity` deliberately does NOT grant interest: it
  is a provisional research note, invisible on the operator's
  HIGH-filtered phone, and most of the dying garbage passed through it
  on the way down — counting it would defeat the gate.
- Config: `MEMEINTEL_ALERT_ENGINE_RISK_ALERTS_REQUIRE_INTEREST`
  (default true; Rule 17). Disabling restores full-priority warnings on
  every token. Opportunity/momentum/accumulation alerts are never
  touched — they ARE the operator's introduction to a token.

Trade-off accepted explicitly: a CRITICAL honeypot finding on a
never-recommended token is also demoted. Correct here because the
operator cannot hold what he was never pointed at, and the finding is
still recorded; revisit if the system ever monitors externally-acquired
holdings.

### Opportunity quality: rug screen decoupled from AI + copycat veto (2026-07-10)

Same feedback round, other half of the complaint: the *recommendations*
themselves were junk — "stupid coins that are either rug pulls or
duplicates of another good coin", observed while the operator had the
Anthropic API key turned off. Two distinct root causes:

1. **The rug-engine screen was reachable only through the AI gate.**
   `_ai_spend_veto` was designed as a credit-conservation check, so the
   whole block sat behind `if self._ai_verifier is not None`. With the
   API key off, `_ai_verifier` is None and the deterministic screen —
   rug engine, deployer blacklist, risk-alerts-already-firing — never
   ran: gate-passing tokens fired HIGH completely unscreened. Fix:
   renamed to `_deterministic_risk_veto` and hoisted out of the AI
   conditional. The free screen now runs whenever a HIGH opportunity is
   about to fire, AI or no AI (a rug is a rug with the key on or off);
   when AI *is* configured, the paid call still runs only after the
   screen is clean, so credit conservation is unchanged. Side benefit:
   the screen now also re-runs on rechecks of already-AI-verified
   tokens (it used to be skipped once verified), so risk appearing
   later still vetoes.

2. **Nothing checked for copycats at all.** A fresh token wearing the
   symbol/name of an established coin (the classic pump.fun knock-off
   pattern) passed every gate on its own numbers. Fix: a second free
   screen, `_copycat_veto` — one provider search
   (`MarketDataService.search_pairs`, DexScreener-backed with provider
   failover) per gate-passing candidate, verdict cached per token
   (names never change; Rule 10/11). The pure rule
   (`_find_established_duplicate`): a *different* token, any chain,
   whose pool holds ≥ `copycat_min_liquidity_usd` (default $100k) AND ≥
   `copycat_liquidity_ratio` × (default 10×) the candidate's liquidity,
   with a normalized-equal symbol or name → veto, downgrading both HIGH
   tiers to MEDIUM with the original named. The size gap is the
   evidence — two small coins sharing a ticker is a coincidence
   (symbols collide constantly), so no veto fires without it (Rule 8).
   Pair age is deliberately not required: aggregated search results
   often omit it, and the liquidity gap alone identifies which token
   owns the name. Unknown/missing search capability (test doubles, a
   provider outage) means no veto, and outages are never cached as
   "clear". Config: `MEMEINTEL_ALERTS_COPYCAT_*` (Rule 17).

Both screens are zero-API-cost to the paid AI budget and downgrade
rather than suppress — the token still appears as a MEDIUM
`early_opportunity` with the veto reason named, invisible on the
operator's HIGH-filtered phone but auditable in history.

### Watchlist opportunity ranking (Part 28 §5/§6) — second, separate axis

Part 28 §5 specifies an upside-tilted "Opportunity Ranking" (Growth 30 /
Momentum 25 / Foundation 20 / Risk 15 / Timing 10) to decide which
tracked tokens deserve attention. This is genuinely distinct from the
master score, whose weights are frozen by the Part 31 Framework
Consistency Lock. Rather than conflate them (which would break Part 31),
the ranking is a **separate advisory axis** in
`analyzers/opportunity_ranker.py`: it composes already-computed category
scores (narrative→growth, momentum, foundation, 100−risk, timing) under
the §5 weights, coverage-honest (a missing factor drops out and the rest
renormalize — never scored as 0). It never alters the master score or its
Elite/Strong/Avoid classification. Persisted per snapshot
(`snapshots.opportunity_rank`, added via the in-place migration) and
surfaced with `watchlist --top`. Part 25 §10 specifies a near-identical
rating with slightly different weights; per the owner's direction to
consolidate overlapping formulas, the Part 28 ranking is treated as
satisfying both. Weights are configurable (`OpportunityWeights`,
`MEMEINTEL_OPPORTUNITY_WEIGHTS_*`) per Rule 17.

## 2026-07-10 — Project 1: live Jupiter round-trip sell test

This is the first of a separate 5-project roadmap layered on top of the
existing Part 4/18/33 security stack, not a renumbered spec part. Three
decisions worth recording:

### 1. Jupiter's keyless tier is deprecated — a free API key is now required

The original assumption going into this work was that Jupiter's
"Lite"/free quote API (`lite-api.jup.ag`) needed no API key at all, based
on older documentation. As of July 2026 that fully-keyless tier has been
deprecated. The current $0/month "Free" plan (`api.jup.ag`) still requires
signup and an `x-api-key` header — it just carries no monthly usage cap
(rate-limited to 1 request/second / 60/minute). **Resolution:** the
collector is gated exactly like Part 17's wallet intelligence (Helius/
Birdeye) — `build_jupiter()` returns `None` when `MEMEINTEL_JUPITER_API_KEY`
is unset, and the probe silently stays off rather than failing loudly.
Nothing else in the pipeline depends on it being present.

### 2. SOL-denominated probe size instead of a live USD conversion

The natural framing ("probe with about $50") would normally suggest
converting a USD figure to SOL at query time. That was rejected: it would
introduce a live SOL/USD price dependency into the *per-token* pipeline,
where none exists today — CoinGecko is already rate-limited to ~10 req/min
and is used exactly once per day, for the market-environment check, not
per-token. Instead, `LiquidityProbeSettings.probe_sol_amount` (default 0.3
SOL) is a static, operator-tunable value, exactly like every other USD
threshold in this system (`min_liquidity_usd`, etc., are also static
figures, not live-priced). The default is documented in `.env.example` as
approximating $50 "at time of writing" and something to revisit as SOL's
price moves — a manual tuning knob, deliberately not automated.

### 3. Three-state semantics — a missing buy route is never treated as suspicious

The naive design would score "no buy route found" as a bad sign (can't
even determine if the token is tradable). That was explicitly rejected
per **Rule 8** ("unknown does not equal unsafe"): Jupiter routing data
lags real pool creation, especially for brand-new pump.fun/Raydium
launches, so "no route yet" is a routine, meaningless-on-its-own outcome
for perfectly legitimate new tokens, not a red flag. **Resolution:**
`live_buy_route_found`, `live_sell_route_found`, and
`live_round_trip_loss_percent` form a three-state model — the latter two
are structurally *not applicable* (not "unknown") until a buy route is
actually confirmed, and are only ever observed/scored inside that branch
(`analyzers/security_analyzer.py::_assess_contract`). The only two
outcomes that are ever treated as dangerous are (a) a confirmed buy route
with no sell route (an unambiguous "can buy, can't sell" rug, forced
destructive regardless of how clean the static GoPlus analysis is), and
(b) a completed round trip that loses a catastrophic fraction of value.
This mirrors the same "unknown excluded from scoring, never assumed safe"
discipline already used everywhere else in this codebase (e.g.
`SubScore.observe()`).


## 2026-07-10 — Project 2: two-way Telegram control

Four decisions worth recording:

### 1. 👍/👎 feedback is advisory-only, by design

Operator thumbs land in their own `operator_feedback` table and are
surfaced in `/mind` — they are deliberately NOT written into
`alerts.outcome` (that column is for measured market outcomes, Part 24)
and NOT fed into the learning layer's ground-truth labels. Rule 8:
operator opinion is an opinion, not a measured outcome. Project 3 can
evaluate the feedback signal explicitly before giving it any weight.

### 2. Auth is a chat-id allowlist with silent drop

Only updates whose chat id string-equals `MEMEINTEL_TELEGRAM_CHAT_ID` are
processed. Strangers get no reply at all (not even an error) — replying
would confirm a live bot worth probing. Their message text is never
logged (only the chat id), inbound text is never echoed unsanitized,
addresses must pass a strict base58/hex charset check before any use, and
replies are plain text (no parse_mode) so nothing inbound can become live
markdown.

### 3. Buy-from-Telegram: scaffold shipped, execution deliberately unbuilt

The owner asked for a buy button on alerts, set up but not launched.
Decision: `trading/execution.py` contains ONLY a `DryRunExecutor` that
journals the intent and says so; no wallet keys, no transaction building,
no signing, no live code path exist anywhere (even
`MEMEINTEL_EXECUTION_DRY_RUN=false` changes nothing, and the reply says
so). The button itself ships hidden behind
`MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=false`. Doctrine note: the system
remains never-AUTO-trading — a button pressed by the operator is a manual
decision — but real execution requires a hot wallet key on the droplet,
which is a key-custody security decision the owner must make explicitly
in a dedicated conversation before any live executor is written.

### 4. Mute filtering sits in the controller and fails open

`/mute` suppresses delivery at the single dispatch point in
`ContinuousScanner._process_result` — analysis, fact recording, snapshots,
and learning all still run (a muted coin keeps building history). An
`is_muted` lookup error counts as NOT muted: an infrastructure hiccup must
never silently swallow a protective alert (Rule 6).

Also closed in this build: ROADMAP item 1's leftover seam — the live
Jupiter probe's confirmed cannot-sell now feeds
`RugEngine.assess(unsellable_override=...)` in the deterministic veto.
The override is only ever True or None, never False: a successful $50
probe must not erase GoPlus honeypot flags (Rule 9 — one source never
overrides another's red flag).

## 2026-07-10 — Project 3: mind-layer P(rug) veto

### Precision, not overall accuracy, is the authority bar

The ROADMAP said "measured rug-verdict accuracy"; the implementation gates
on measured rug **precision** (of everything the layer called RUG, how
many actually rugged), over a minimum count of graded rug calls. Reason:
the veto's only failure cost is the false positive — a wrongly blocked
HIGH alert is an opportunity the operator never sees. Recall failures
(missed rugs) are already covered by every other screen; a veto earns
authority by NOT crying wolf. Sample count is the number of graded rug
calls (TP+FP), the honest denominator behind that precision figure.

### Abstention is the default in every direction

Flag off (ships off), learning layer absent, too few graded calls,
precision below the floor, metrics unavailable, or evaluate_coin raising —
all abstain and change nothing (Rule 8: an unproven or absent opinion
never blocks an alert). The earned-authority verdict is cached for 30
minutes (configurable) so the metrics sweep never runs per candidate;
the live P(rug) evaluation does run per gate-passing candidate (they are
rare by design).

### Operator 👍/👎 feedback stays out of the veto

Project 2's feedback remains advisory display only. The veto's authority
comes exclusively from measured market outcomes (graded predictions),
never from opinion — same Rule 8 line drawn in the Project 2 entry.

## 2026-07-10 — Adversarial review of Projects 1-3: three fixes

A cold multi-agent review of the three shipped features (each finding
independently verified) surfaced three real defects, all fixed:

### 1. (HIGH) Sell-leg "no route" was a false-positive honeypot verdict

Project 1's buy leg correctly treated Jupiter "no route" as unknown (Rule
8 — a fresh pool Jupiter hasn't indexed isn't a rug), but the sell leg
mapped the identical ambiguous 4xx to a CONFIRMED cannot-sell -> forced
DESTRUCTIVE/score 0. That sinks legit brand-new or thin pools that simply
can't exit a full $50 position in one swap — the exact false positive the
operator called unacceptable. **Fix:** when a full-size sell finds no
route, re-probe with a small fraction (`sell_confirm_fraction`, default
5%). Nothing sells at any size -> real honeypot (destructive stands); a
tiny sell routes -> the pool is merely thin, sellability is confirmed and
the token is NOT condemned (round-trip loss left unknown; the liquidity
sub-score already handles thinness). Verified end-to-end: honeypot ->
destructive, thin pool -> survives, healthy -> unchanged.

### 2. (LOW) Mute/holding missed on non-Ethereum EVM addresses

`_resolve_token` guessed chain "ethereum" for any unknown 0x address, so a
`/mute` or `/holding` on an unscanned Base/BSC/Arbitrum token filed under
the wrong chain and silently did nothing. Dormant under the Solana-only
default. **Fix:** `is_muted`/`is_holding` now match by ADDRESS on any chain
(SOL base58 and EVM 0x formats can't collide; 0x matched
case-insensitively), so the operator's intent sticks regardless of the
inferred chain.

### 3. (LOW) Command replay after a monitor restart

The getUpdates offset lived only in memory; Telegram redelivers
un-acknowledged updates for ~24h, so a restart could replay the last
commands and double-record advisory feedback / re-journal dry-run intents.
**Fix:** on startup the listener drains and DISCARDS any pending backlog
(short poll) before processing — a redeploy is now a no-op for input, and
stale commands never re-fire (correct for a control bot).

## 2026-07-10 — Project 6: live buy/dump from Telegram (operator-requested)

The operator asked to buy and dump straight from Telegram. This crosses the
line the project held from day one ("never AUTO-trades / decision-support
only"), so the boundaries were drawn deliberately and confirmed with him:

### The line that still holds

The system still never AUTO-trades. Nothing initiates a trade without an
explicit operator button/command. What changed is that an operator-tapped
buy/dump can now actually execute, where before it only journaled a dry-run
intent.

### Custody: a dedicated hot wallet, never the main one

For a buy to land in the operator's Phantom wallet, a Phantom-visible wallet
must sign it. Fully-automatic execution (no per-trade approval) therefore
requires the signing key on the droplet. Decision (confirmed with the
operator): use a DEDICATED, freshly-created Phantom account funded with only
pocket money, imported into Phantom so he can watch/withdraw it, and NEVER
his main wallet. The blast radius of a server compromise is bounded to
whatever he funds that one wallet with. He chose a ~$50 CAD ceiling.

The key is read only from ``MEMEINTEL_EXECUTION_PRIVATE_KEY`` (env/.env on
the droplet), never in code, git, logs, or chat (Rule 16); the RPC/api keys
in the trading path are redacted from logs, and error text is scrubbed of
the wallet pubkey.

### Guardrails

* ``live_enabled`` defaults OFF; with it off (or no key) every buy/dump
  routes to the dry-run executor (signs nothing).
* Per-trade cap ``max_buy_sol``; a single buy above it is refused before any
  network call. The wallet balance is the ultimate cap (can't spend what it
  doesn't hold), re-checked live before each buy.
* One trade at a time (asyncio lock). No route / on-chain error / timeout are
  all reported to the operator, never crash the listener.
* Execution is Solana-only, via Jupiter (quote -> build swap tx -> sign with
  solders -> submit via Helius RPC -> confirm). ``solders`` is imported lazily
  so the scanner runs without it when live trading is off.

### Dump = sell 100%

The "dump button" sells the wallet's entire balance of that token back to
SOL (the panic-exit the operator asked for), quoting the full position and
refusing gracefully if no sell route exists right now.

## 2026-07-10 — Adversarial review of Project 6 (live trading): six fixes

A three-lens money-safety review (each finding independently verified,
6/6 confirmed) hardened the live executor before it was ever armed:

* **Double-spend on confirmation error (HIGH).** A buy whose transaction was
  already broadcast but whose confirmation RPC then hiccupped was reported as
  "Buy failed — retry", hiding the signature and inviting a second buy. Now
  the rule is absolute: once `send` returns a signature the tx is journaled
  and the signature is always surfaced; a confirmation-phase error returns
  "submitted, unknown — do NOT retry, verify on Solscan", a genuine on-chain
  revert returns "did not go through, only the fee was spent — safe to retry",
  and a submission error returns "may not have gone through — do NOT retry
  blindly". Failures that provably spent nothing (cap, balance, no-route,
  build/sign) still say so.
* **Unbounded slippage (HIGH).** `dynamicSlippage: true` let Jupiter fill a
  thin meme pool 20-50% below quote regardless of the configured
  `slippage_bps`. Changed to `dynamicSlippage: {maxBps: slippage_bps}` so the
  operator's setting is a real hard cap on the signed transaction.
* **Stale cached quote (MEDIUM).** Live swaps were built from the probe's
  shared 45s-cached quote. The executor now fetches quotes with
  `use_cache=False`, so every signed swap uses a trade-time quote.
* **Crash-on-missing-solders (LOW).** `build_executor` only caught
  `ValueError`, so an ImportError (solders absent/broken) would crash the
  scanner at startup. It now catches any construction failure and falls back
  to dry-run.

## 2026-07-11 — Live-trade 429s: dedicated Helius account for trading

The first live `/buy` attempts aborted safely ("Nothing was spent") because
the pre-broadcast balance read got HTTP 429 from Helius. Diagnosis on the
droplet: a single direct `getBalance` also 429'd (5/5), while the journal
showed the scanner's wallet-intelligence traffic
(`api.helius.xyz/v0/addresses/…/transactions`) being continuously rate
limited — the shared free-tier Helius account is exhausted server-side, so
any budget the trade path shares with the scanner is already spent.

Two-step resolution (Rules 4/9/11/17/18):

* **Share one client-side bucket per account.** `build_wallet_service` /
  `build_executor` accept a shared `RateLimiter`; the monitor builds one per
  Helius key so two clients on the same account can no longer each assume
  the full budget. (Necessary, but insufficient when the account itself is
  out of credits.)
* **Separate account for the money path (operator's suggestion).**
  `MEMEINTEL_EXECUTION_HELIUS_API_KEY` names a second Helius account used
  only by `SolanaRpcClient` (balance reads, send, confirm — a handful of
  calls per trade). When set and different from the scanner's key, the
  trading client gets its own fresh limiter — a separate account is a
  separate real budget, and sharing the scanner's exhausted bucket would
  re-create the starvation. Empty keeps the old shared-key behavior
  (Rule 18); the trade path never competes with data collection (Rule 4).

## 2026-07-11 — First live trade; double-reply fix; wallet intelligence paused

**First live buy succeeded — and the round trip completed.** With the
dedicated trading Helius key in place (previous entry), the operator's
`/buy <token> 0.001` executed end to end from Telegram — cap check →
balance read → fresh quote → sign → broadcast → on-chain confirmation —
on the dedicated ~$20 Phantom trading wallet. Later the same day the
operator confirmed the **`/dump` sell-back also executed successfully**,
completing the full buy-and-sell round-trip validation. Project 6's money
path is fully validated in production; live trading is trusted for real
alerts within its caps.

**Double-reply fix (commit `85ed00d`).** Typed `/buy` and `/dump` commands
produced two Telegram messages: `_do_buy`/`_do_dump` replied directly AND
returned a short string the message handler also sent. Confirmed from the
operator's screenshot. Resolution: the do-functions are now pure (return
the full result, no side-effect reply); the button-callback path replies
explicitly and returns a short ack; the text-command path returns the
result through the normal single-reply flow. Regression tests assert the
reply COUNT, not just the first message (the original tests missed this by
only checking message content).

**Wallet intelligence (Part 17) paused in the monitor — operator decision.**
Running smart-money analysis on every analyzed token exhausted the main
Helius account's monthly free credits; every call (scanner and trading
alike) got 429 "max usage reached", so the layer was producing zero data
while spamming retries. The operator's call, in his words: *"i need to make
money first off this bot… for now we dont need it"*. So:
`MEMEINTEL_WALLET_ENABLE_IN_MONITOR=false` on the droplet (.env change
only — no code was removed; the layer stays built and tested).
**Re-enable criteria (agreed):** only when the bot is making money, and
only together with (a) a paid Helius plan (~$49 USD/mo Developer tier) AND
(b) credit-gating in the pipeline so wallet lookups run only on
best/alert-worthy candidates instead of every analyzed token (est. 5-10×
credit reduction; may even fit the free tier). Neither piece alone.

**Standing operator directions reaffirmed this session:** Project 4
(dashboard) is DISCARDED — do not build. Project 5 (paid social/Twitter
data) is PARKED — build later, only with explicit cost approval. The hard
security rule stands: the wallet private key / seed phrase goes ONLY into
the droplet `.env` over SSH — never into chat, never into git. API keys
are lower-stakes (they guard RPC credits, not funds) but are still
secrets; keys that transited chat this session can be rotated in the
provider dashboard at the operator's leisure.

## 2026-07-11 — Adversarial bug hunt on the money path (post-arming)

Three parallel hunters reviewed the code that touches money, each finding
independently verified against source before any fix. The newest code (the
shared-limiter and dedicated-Helius-key commits) came back **clean** — the
key/limiter matrix and RPC lifecycle were confirmed correct. Real defects
found and fixed (all with regression tests; suite 738 → 747):

- **Backlog replay on restart (money-safety).** If `_discard_backlog`'s
  first poll failed transiently, `_poll_forever` swallowed it and entered
  LIVE polling with the offset un-advanced — the backlog was then *handled*,
  replaying a buffered `/buy` or `/dump` as a real trade. Fix: the drain is
  now RETRIED (with backoff) until it provably succeeds before live polling
  starts; a malformed/`ok:false` drain response raises instead of being
  mistaken for "drained". This restores the invariant a restart is a no-op
  for input, which matters much more now that trading is live.
- **Button popup always claimed "Buy sent"/"Dump sent".** The callback ack
  was hardcoded regardless of outcome, so tapping Dump on a token you don't
  hold popped "Dump sent" while the chat said "Nothing to dump". Fix: the
  popup is now neutral ("Done — see chat for the result"); the chat reply
  remains the authoritative outcome.
- **Non-finite trade amount.** `nan`/`inf` slipped past `<= 0` and `> cap`
  (both False for nan) into `int(nan * ...)`. Fix: `math.isfinite` guard at
  the command layer AND at the executor money gate (defense in depth).
- **CancelledError could lose a broadcast signature.** On shutdown during
  the confirm poll, cancellation escaped uncaught. The signature is already
  journaled before confirm; added an explicit `except CancelledError` that
  logs the signature/Solscan link at ERROR before propagating, so it is
  never lost from the record. (The far narrower cancel-inside-send window is
  documented, not "fixed" — the robust fix risks shutdown-time detached
  tasks in money code; the journal-before-confirm ordering already bounds
  the exposure.)
- **Malformed balance RPC → traceback.** A non-numeric `getBalance` value
  raised a bare `TypeError` past the `except CollectorError`. Fix:
  `solana_rpc` now raises `CollectorError` on a non-numeric balance, so the
  pre-broadcast read fails closed to the clean "could not read the wallet
  balance — Nothing was spent" abort.
- **Partial-dump under-report (low).** `get_token_balance_raw` silently
  skipped unparseable token accounts; added a warning log so a rare
  under-counted "sell 100%" is diagnosable.

## 2026-07-11 — OOM crash-loop, then a reclaim stall: memory policy for 1GB

One day after arming, the bot went silent. Root causes, in the order they
were hit (full symptoms + watch-items in OPERATIONS.md "Memory on the 1GB
droplet"):

1. The learning layer had grown from ~450 to 6,362 resolved coins
   (~440M → ~740M RSS), outgrowing the unit's `MemoryMax=512M` → OOM
   crash-loop every ~34s. Each restart's backlog-discard (correctly)
   dropped the operator's buffered Telegram commands, which is why the
   bot "ignored" him while the service showed active.
2. The first fix (`MemoryHigh=700M` + `MemoryMax=800M`) traded the crash
   for a stall: usage sat ABOVE the soft limit, so the kernel throttled
   the single-event-loop process into unresponsive reclaim. Lesson
   recorded: never use MemoryHigh for this workload on a swapless box.

**Resolution:** `MemoryMax=880M` as an OOM backstop only (no MemoryHigh),
plus a persistent 1G swapfile on the droplet; `deploy/meme-intelligence.service`
updated so reinstalls don't resurrect the 512M cap. **Standing watch-item:**
learning memory grows unboundedly with resolved coins — when steady-state
nears ~800M, either bound the analog index or move to the 2GB droplet;
do not keep raising the cap on 1GB.

## 2026-07-11 — Liquidity/market-cap floor for buy-side alerts

The operator dropped his phone threshold to MEDIUM (to see opportunities
again after HIGH strong_candidate alerts dried up — see the delivery trace)
and immediately started getting buy-side alerts on 0-liquidity / 0-market-cap
coins: "it's not using its mind." Root cause: `strong_candidate_min_liquidity_usd`
only DOWNGRADES a thin candidate HIGH→MEDIUM, and there was no absolute floor
on MEDIUM opportunity/momentum alerts — so an untradeable pool's pump artifact
still fired at MEDIUM.

Fix (`AlertThresholds.opportunity_min_liquidity_usd` /
`_min_market_cap_usd`, both default 0.0 = OFF, Rule 18): in
`AutomationRules.evaluate`, below a set floor the BUY-SIDE alert types
(`_BUY_SIDE_ALERT_TYPES`: high_priority_opportunity, strong_candidate,
early_opportunity, momentum, smart_money_accumulation) are dropped entirely.
Protective alerts (death/risk/whale-exit/insider/…) are NEVER floored — a
thin dying coin's holder still needs the warning. Unknown/NaN liquidity or
mcap counts as below a set floor (a buy you cannot size is not phone-worthy,
Rule 8), matching the strong-candidate depth veto's own convention.
Verified end-to-end: an $800-liquidity coin's momentum alert is suppressed
while its risk_warning survives; a $90k coin is unaffected. Operator sets
`MEMEINTEL_ALERTS_OPPORTUNITY_MIN_LIQUIDITY_USD` (~10000 to start).

NB (separate follow-up, not yet fixed): the delivery trace also found that
`NotificationEngine.dispatch` counts a console-only or min-priority-filtered
(None) sink result as "delivered" (notification_engine.py:711-716), so the
`alerts` table's presence of a row does NOT prove phone delivery. Confirm
real phone delivery with `alerts --test`, not DB counts.

## 2026-07-11 — Rug/risk veto now SUPPRESSES buy-side alerts (was: downgrade)

Immediately after moving to the MEDIUM phone threshold, the operator got
buy-side alerts on coins the rug engine had flagged: "it's still not
detecting rugs." It WAS detecting them — every deterministic screen (rug
engine, copycat, risk-already-firing) and even the mind-layer p(rug) veto
only DOWNGRADED a flagged HIGH candidate to a MEDIUM `early_opportunity`
("provisional, held back by vetoes"). That hid it from a HIGH-only phone,
but on a MEDIUM phone the demoted rug lands directly on it. The whole veto
design silently assumed HIGH-only delivery.

Fix: in `AutomationRules.evaluate`, when `deterministic_risk_veto` is set,
the buy-side alert types (`_BUY_SIDE_ALERT_TYPES`) are dropped entirely —
same suppression path as the liquidity floor above — so a flagged coin is
no buy alert at ANY priority (opportunity AND momentum AND smart-money).
Protective alerts still fire (a flagged coin's holder needs the warning);
the softer downgrades (lukewarm-AI, strong-candidate depth caveat) still
emit a MEDIUM `early_opportunity` — only the risk/rug/copycat veto
suppresses. The mind-layer p(rug) veto flows through the same
`deterministic_risk_veto` path, so enabling `MEMEINTEL_LEARNING_VETO_ENABLED`
now adds the learned 97%-precision detector to the suppression (previously
it too only downgraded). Verified end-to-end + updated the two controller
tests that asserted the old MEDIUM-downgrade behavior.

## 2026-07-11 — Young tokens are no longer permanently blacklisted (Part 13/15)

The operator asked "how come it's not learning new coins" and, separately,
"is my bot detecting coins then calling it rug pulls because of the age?"
Investigation (an Explore agent tracing the multi-cycle token flow through
`workflow/controller.py`) confirmed a real, previously-unknown gap:

- The rug-engine/copycat veto re-verification path already worked as
  designed (Part 15 comment: "a vetoed token is NOT cached as verified") —
  a flagged HIGH candidate genuinely gets a fresh look on the watchlist
  recheck cadence once its risk clears.
- BUT a token that scored `Classification.AVOID` on its very FIRST look —
  overwhelmingly because a brand-new pool has no GoPlus/community data yet,
  not because anything was actually wrong with it — was added to the
  scanner's `_seen` dedupe set unconditionally, forever. That exact token
  was never analyzed again for the life of the process, even once its data
  fully resolved an hour later.

**Fix** (`workflow/controller.py`): `_finalize_or_reschedule()` replaces the
unconditional `_seen.add(key)`. A CONFIRMED red-flag AVOID
(`result.master.overrides` non-empty — destructive security, fake
community, extreme risk) is real evidence and is still permanently
excluded, unchanged. An AVOID with NO overrides and `coverage` below
`WorkflowSettings.insufficient_data_min_coverage` (default 0.5) on a pool
younger than `insufficient_data_max_age_minutes` (default 120) is instead
scheduled into a new bounded `_retry_pending` set (paced by
`insufficient_data_retry_minutes`, default 15) and re-analyzed later by the
new `_retry_insufficient_data()` pass — mirroring `_recheck_watchlist`'s
existing pattern (re-fetch via `market_service.get_token_pairs`, re-run the
full pipeline, dispatch through the normal alert path). Unknown pool age
is not retried (Rule 8 — can't reason about "too young" without knowing the
age). All four knobs are configurable and default to preserving the
pre-fix behavior when the flag is off.

Two real bugs surfaced and fixed during implementation (both caught by the
test suite, not by inspection):
1. Solana addresses are case-sensitive base58; the retry queue's key is
   deliberately lowercased for dedup (matching `_seen`'s own convention),
   which is fine for membership checks but would corrupt a later live API
   call. Fixed by carrying the ORIGINAL-case address in `_retry_pending`'s
   value alongside the due-time.
2. `_BoundedKeySet` has no delete, so a key promoted to `_seen` leaves a
   stale, still-"due" leftover in `_retry_pending` — undetected, this would
   re-fetch and re-analyze an already-finalized token every cycle
   thereafter. Fixed with a cheap `if key in self._seen: continue` guard at
   the top of the retry pass (the leftover itself is harmless dead weight,
   same tolerance `_BoundedKeySet`'s own FIFO eviction already documents).

15 new tests (`test_controller.py`, `test_settings.py`); suite 754 -> 765.

## 2026-07-11 — The rug engine now screens EVERY buy-side alert, not just HIGH

Following the "no permanent blacklist for young coins" fix, the operator
asked to "fix where it thinks rug pulls are new coins." Investigation of
`workflow/controller.py::_process_result` found a bigger, pre-existing gap
than the age question implied:

`_deterministic_risk_veto()` (the rug engine + mind-layer p(rug) screen) was
only ever COMPUTED when the token's provisional alerts included one of the
two rare HIGH tiers (`high_priority_opportunity` / `strong_candidate`). If
a token instead only fired `momentum` or `early_opportunity` — by far the
largest alert categories (momentum alone: ~7,000/day in the operator's own
DB) — `deterministic_veto` stayed `None` for the whole function, so the
rug engine NEVER RAN on it, veto-suppression or not. A rug pull classically
pumps hard right before it dumps, so exactly the coins momentum got excited
about were the ones the rug engine never checked. This predates today's
suppress-vs-downgrade fix and would have limited its effect to the rare
HIGH tier regardless.

The rug-engine/mind-layer screen is genuinely zero-API-cost (pure local
computation over already-collected data — its own docstring said so); the
HIGH-tier gating was a CPU-conservation choice, not a cost one (Rule 12,
not Rule 11). Only the copycat screen (`_copycat_veto`) makes a real
market-search API call.

**Fix:** `_process_result` now computes two independent trigger flags —
`fires_buy_side` (any `_BUY_SIDE_ALERT_TYPES`) and `fires_high_tier` (the
original two-tier check). The free rug-engine/mind-layer screen runs
whenever `fires_buy_side`; the costed copycat search stays gated behind
`fires_high_tier` only (Rule 11 preserved); AI verification likewise stays
HIGH-tier-gated (unchanged cost behavior). A veto now suppresses ANY
buy-side alert type via the existing `deterministic_risk_veto is not None`
check in `AutomationRules.evaluate` (today's earlier fix).

Verified with a regression test that fails against the pre-fix code and
passes against the fix: a $6,000-liquidity coin (fires momentum +
early_opportunity, confirmed never HIGH tier) with a blacklisted deployer
is now fully suppressed; the same fixture with a clean deployer still fires
normally (fixture sanity-checked both ways). 1 new test; suite 765 → 766.

## 2026-07-12 — Massive adversarial bug hunt: 8 fixes across the money path

At the user's request ("go on a massive massive bug hunt"), ran a multi-agent
adversarial find → refute → synthesize pass over the whole codebase. 19
candidates surfaced; 9 survived independent refutation and deduped to 8
distinct findings (5 CONFIRMED, 3 PLAUSIBLE), all fixed here with regression
tests. Every fix follows Rule 3 (never break working code) and Rule 7
(reliability / graceful degradation). Suite 766 → 778 (12 new tests).

1. **Edited Telegram message re-fired a real trade.** `_handle_update`
   dispatched `update.get("message") or update.get("edited_message")`, so
   editing a prior `/buy`/`/dump` message was re-processed as a SECOND live
   trade with no new operator intent. Now only `message` is dispatched; edits
   are ignored. (`alerts/telegram_commands.py`)

2. **Double-tapped inline button double-traded.** Two taps of the same buy/dump
   button (Telegram delivers both) each executed. Added `_claim_button`
   (bounded FIFO keyed on `(message_id, data)`) claimed AFTER the trading guard
   and BEFORE execution — a repeat tap is rejected with "already actioned". A
   callback lacking a numeric `message_id` fails OPEN (never blocks a real
   trade). Both `_handle_buy` and `_handle_dump` guarded.
   (`alerts/telegram_commands.py`)

3. **Send cancelled mid-flight could lose a broadcast signature.** A graceful-
   shutdown cancel landing while the `sendTransaction` body is on the wire left
   no record of a possibly-broadcast tx. The signature is now derived from the
   signed bytes (`_signature_of`, deterministic and identical to the RPC's
   return) BEFORE the send await, and a `CancelledError` during send journals
   it with a verify-on-chain note, then re-raises — mirroring the confirm-stage
   guard so the operator never blindly re-taps. (`trading/execution.py`)

4. **A raw (non-project) exception killed the 24/7 loop.** The cycle backstop
   caught only `MemeIntelError`, so an unwrapped `sqlite3.OperationalError` (full
   disk / locked DB under monitor+cron contention) or any `RuntimeError` escaped
   and stranded the operator with no way to `/dump`. Widened to `except
   Exception` (CancelledError/KeyboardInterrupt/SystemExit are BaseException and
   still propagate for clean shutdown). (`workflow/controller.py`)

5. **Synchronous retrain stalled the event loop.** `retrain_if_due()` (lightgbm
   retrain + HDBSCAN + faiss build, multi-second, CPU-bound) ran inline on the
   shared event loop, so a rebuild could freeze an emergency `/dump` for its
   whole duration. Now dispatched via `asyncio.to_thread`. (`workflow/controller.py`)

6. **Torn FAISS index crashed the whole verdict path.** An interleaved persist
   (daemon + retrain cron racing) can leave `index.ntotal` > `len(entries)`; a
   returned id past the metadata length `IndexError`ed. `query()` now bounds-
   checks the id and degrades to the valid analogs. (`learning/analog.py`)

7. **Insufficient-data retry re-hammered a failing provider every cycle.** A
   non-terminal retry outcome (market outage, security data not yet indexed,
   analyzed via another path) left the entry at its old already-past due time,
   so it re-hit the provider every cycle for the whole outage. `_retry_pending`
   now carries a 3-tuple `(due, case-preserved address, give-up deadline)` and
   `_repace_retry` pushes the entry to its next paced due time — or finalizes it
   into `_seen` once the pool ages past its give-up deadline. (`workflow/controller.py`)

8. **Non-finite probe size slipped past validation.** `probe_sol_amount <= 0`
   let NaN/inf through (all comparisons with NaN are False), so a misconfigured
   env could size a probe trade with a non-finite amount. Guarded with
   `math.isfinite`. (`config/settings.py`)

## 2026-07-12 — Safety checklist: annotate soft misses, block only rugs

**Operator request:** "I don't want only one thing to stop it from sending it
to me. If it's a rug pull don't send it to me at all, but if just one thing
misses the checklist send it through and just let me know if anything didn't
make the checklist." Plus the standing concern that a brand-new coin (naturally
concentrated in one wallet, thin history) must not be rejected merely for being
young.

**Design principle:** *gate on a rug, annotate everything else.* A rug is a
COMBINED verdict (many contract facts weighed together) — exactly what the rug
engine already produces and what flows through `deterministic_risk_veto`. A
single soft signal never crosses that combined threshold, so keeping the rug
veto as the only suppressor directly satisfies "one thing shouldn't block it,
but a rug should."

**What changed (`alerts/notification_engine.py`):**
- The buy-side suppression condition was `deterministic_risk_veto is not None OR
  below the liquidity/market-cap floor`. The floor half was REMOVED from
  suppression. Now only the rug veto drops a buy-side alert.
- Every surviving buy-side alert carries a `_SafetyCheck` list rendered onto the
  alert (`AlertEvent.checklist`, shown in both `AlertEvent.render()` and
  `sinks.format_alert`): a "passed X/Y" header plus per-signal lines —
  ✅ pass, ⚠ soft miss (annotate, never suppress), ℹ note, ❔ unknown.
- Checklist lines: Sellable (honeypot/unsellable), Mint authority renounced,
  Freeze authority renounced, Sell tax under a comfort ceiling, Deployer clean
  (no same-creator honeypots), Liquidity vs the operator's comfort floor, and
  Market cap vs its floor (only when set). Top-wallet concentration is an
  informational NOTE — never a fail — framed "normal for a new launch" on a
  young pool. Missing data is ❔, never assumed safe (Rule 8).

**Why the liquidity floor moved from gate to note:** it was the ONE standalone
(non-rug) suppressor. The operator explicitly accepted more alerts in exchange
for seeing *why* each one is imperfect; a thin pool now sends with
"⚠ Liquidity $6,200 — below your $10,000 comfort floor" rather than vanishing.
Any advisory line can be promoted back to a hard block later if the noise
returns — the split is intentional and reversible.

**Config (`AlertThresholds`, env `MEMEINTEL_ALERTS_*`):**
`checklist_sell_tax_max_percent` (default 15.0) and
`checklist_new_launch_minutes` (default 60.0). The liquidity/market-cap floors
keep their env vars but are now comfort lines, not gates.
`AutomationRules` gained an optional `now_func` (the controller passes its
clock) to age the pool for the concentration note.

Rug behavior is unchanged: a confirmed honeypot is destructive (no buy-side
alert, a protective emergency warning instead), and `deterministic_risk_veto`
still suppresses buy-side entirely. 9 new/updated tests; suite 778 → 784.

**Follow-up same day — hard "must be tradeable" floor (`_untradeable`).** After
loosening the alert gates via `.env` so coins actually reached the phone, the
operator got alerts for coins with **0 / missing liquidity or market cap**. Those
aren't thin-but-real coins the checklist should annotate — they cannot be bought,
sized, or valued at all, so they are noise. Added `AutomationRules._untradeable`:
a buy-side alert is suppressed outright when liquidity OR market cap is 0,
negative, NaN, or None (missing counts as untradeable — Rule 8, absent data is
not a green light). This is a second hard suppressor alongside the rug veto and
is distinct from the comfort floor: a $4k pool still sends with a ⚠ note; a $0/None
pool never sends. 4 new tests; suite 784 → 788. Note: this superseded the earlier
checklist test that annotated unknown liquidity — 0/None now blocks, thin-but-real
still annotates.

**Operator gate tuning (`.env`, not code).** The spec's human-review gates
(security 80 / overall 85 / onchain 75 / momentum 70) are tuned for rare elite
picks and produced ~zero alerts on meme coins. Loosened on the droplet via
`MEMEINTEL_ALERTS_*` (security 55, overall 62, onchain 50, liquidity 55,
community 55, momentum 55, strong_candidate_overall 68, strong_candidate depth
6000) plus `MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY=medium`. Kept as
operator `.env` config, NOT committed defaults — the spec defaults stay the
source of truth (Rule 1/18); this is operator tuning for the live-trading feed
use case and is trivially reversible. Safe to loosen because the rug veto, the
new untradeable floor, and the safety checklist all still apply.

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

### Self-learning "mind" layer: analog + model + rug, reusing existing engines

The "Self-Learning Mind Layer" prompt asked for analog pattern recognition
(FAISS k-NN over past coins), a warm-started LightGBM classifier, HDBSCAN
archetypes + novelty, a hard-signal rug engine, and an accuracy-weighted
ensemble — as a reasoning layer on top of the existing scanner, returning
structured data (never auto-trading). Several design choices were made under
Rules 1/8/18/20:

- **Reuse over reinvention (Rule 18).** The rug engine's contract-level
  signals (honeypot/un-sellable, mint & freeze authority, concentration, LP
  lock, sell tax, same-creator honeypot count) read the existing
  `SecurityProfile` (populated by the GoPlus collector) rather than
  re-collecting. "Rug-by-analogy" (§5b) is not separate code — confirmed rugs
  land in the same FAISS index as any outcome, so a coin near past rugs is
  flagged by resemblance.
- **One feature space.** Analog search, the classifier, and archetype
  clustering all operate on the *scaled* fingerprint; refitting the
  `StandardScaler` (drift handling) triggers a full rebuild so the three models
  never disagree about coordinates.
- **Honest uncertainty, not fabricated confidence (Rule 8).** A source with
  too few analogs or an untrained classifier abstains (`None`) and is dropped
  from the ensemble; with no source available the verdict is a uniform
  distribution, and cold start scales confidence down. Missing fingerprint
  metrics lower a reported `coverage` rather than being invented.
- **Active honeypot simulation deferred with a seam (Rule 20).** The spec's
  live Solana Jupiter round-trip / EVM `eth_call` sell simulation needs RPC we
  don't run in this environment and is a distinct collector; the rug engine
  exposes an `unsellable_override` parameter so that simulator plugs in later,
  and uses the existing GoPlus honeypot flags today.
- **Additive, off-by-default integration (Rules 3/7/10/11).** The scanner hook,
  the backtester `resolve_outcome` feed, and the CLI `mind` command are all
  opt-in (`MEMEINTEL_LEARNING_ENABLED` / `_ENABLE_IN_MONITOR`, `monitor
  --learn`) and fully error-isolated — a learning failure logs and is
  swallowed, never breaking a scan cycle.

**Resolution:** Implemented in `meme_intelligence/learning/` (`models`,
`features`, `analog`, `archetypes`, `classifier`, `rug_engine`, `ensemble`,
`metrics`, `store`, `service`), config groups `LearningSettings` /
`LightGBMSettings` / `RugThresholds` / `RugSignalWeights` in
`config/settings.py`, the `mind` CLI command, and the opt-in hooks in
`workflow/controller.py` and `analytics/backtesting.py`. Decision-support only;
it never trades (Rule 21).

## 2026-07-17 — Snapshot restore + 1-hour freshness gate

**Restore:** the operator ordered the bot restored exactly to his
2026-07-13 snapshot ("I want it exactly how it was. Take everything we did
after the snapshot and delete it"). The uploaded tarball was verified
byte-for-byte identical to commit 19d1a20 (168 files, both directions) and
the tree was reset to it as a NEW commit (9b0eca7) — nothing force-deleted;
the pre-restore tip (24156d9, 947 tests) stays recoverable in history, and
a stamped pre-restore code tarball was handed to the operator. Everything
2026-07-14 → 2026-07-16 is content-removed: ceilings/freshness gate,
smart-wallet clock + reputation connector, staleness door, credit gate,
threading fix, streaming rebuild, training cap, watchdog, mind upgrades
1-2. Deploy guidance flipped MEMEINTEL_WALLET_ENABLE_IN_MONITOR and
MEMEINTEL_BOOST_WATCHER_ENABLED back to false (the restored code has no
credit gate, and the boost radar — removed 07-14 by operator decision —
is alive again in this tree). Known regressions accepted with the restore
and told to the operator: /mind's heavy walk will freeze the bot at
today's 26k resolved coins (avoid the command), no watchdog, classifier
retrains fail silently in the worker thread (the 07-13 behavior).

**Freshness gate (built ON TOP of the restored tree, operator request:
"Make it so it only sends me coins less then 1 hour old"):**
``AlertThresholds.opportunity_max_age_hours`` (default 1.0 — ON per the
explicit request; env ``MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS``;
0 = off). ``AutomationRules._too_old`` suppresses BUY-SIDE alerts
(opportunity/momentum/smart-money) for pools older than the window, in the
same suppression clause as the untradeable/oversized gates. Protective
alerts always fire, and an unknown pool age never trips the gate (Rule 8,
mirroring the ceiling's convention). Generic test fixtures moved from a
3h-old pair to 30min so the default-on gate is exercised, plus 6 dedicated
tests (default-on suppression, fresh pass-through, unknown age, 0=off,
protective exemption, config validation). Suite: **827 passing** (821
restored + 6).

## 2026-07-17 (later) — 3-hour freshness window + wallet intelligence rebuilt with the credit gate

Operator reported buy-side alerts full of "unusual graphs like a robot is
controlling it" — volume-bot/bundler launches, which dominate the sub-1h
pump.fun population his 1h freshness gate had concentrated on. His call:
"Make it three hours and build a wallet intelligents thing."

1. **opportunity_max_age_hours default 1.0 -> 3.0.** Most bot-run launches
   collapse or dump inside the first hour; a coin still healthy at 2-3h is
   likelier organic. Same gate semantics (buy-side only, protective alerts
   exempt, unknown age never trips).
2. **Wallet-intelligence credit gate rebuilt** (the 2026-07-15 design,
   re-implemented on the restored 2026-07-13 tree — the original was
   content-deleted by the restore): `WalletIntelSettings.credit_gate_min_
   security_score` (default 50, env-configurable); `ResearchPipeline._worth_
   wallet_lookup` spends a metered lookup only on a candidate that could
   still earn a buy-side alert (not destructive, score >= floor, tradeable,
   inside the alert engine's own ceiling AND the 3h freshness window —
   deliberately the same thresholds the alert engine enforces, so no lookup
   is ever spent on a coin the operator can never be pitched);
   `force_wallet_check` bypass threaded through all 5 controller call
   sites (holdings always checked; /check always checks). Wallet
   intelligence is the layer with the purpose-built bot-chart detectors
   (identical-size trade fraction, dominant-buyer volume fraction) — with
   the gate, re-enabling it costs a handful of lookups per day instead of
   one per analyzed token (the 2026-07-11 credit-burn incident).
   The monitor flag stays operator-controlled in .env; flipping it on is
   part of the deploy block. Until the Helius account's monthly credits
   reset, gated lookups will fail gracefully (429) and analysis continues
   without wallet data — it starts working the moment credits return.

Suite: **837 passing** (+10: 9 pipeline gate tests incl. an empirically
derived weak-but-not-destructive security fixture at 35.25, 1 controller
holdings-bypass test).

### Review pass on the 3h + credit-gate build (same day): 3 confirmed findings, all fixed

A 5-agent review (2 finders, 3 adversarial verifiers — operator's agent cap)
confirmed and fixed:

1. **Momentum alerts had NO security floor** (medium; the review's core
   find): opportunity tiers require security >= 80, but `_momentum_rule`
   fired for any non-destructive coin — so a coin scoring 40-49 purely on
   soft flags (zero rug signals; the verifier constructed one empirically at
   44.2) could ride bot-painted volume to a delivered MEDIUM momentum alert
   while the credit gate, by design, skipped its wallet screening. Fixed:
   `AlertThresholds.momentum_min_security_score` (default 50, aligned with
   the credit gate's floor; 0 = off). The gate's "below the floor never
   reaches the phone as a buy signal" justification is now actually true.
2. **No spend ceiling on gated lookups** (medium): every gate input is
   attacker-manufacturable (clean-by-construction launches), theoretical
   drain ~38k metered calls/day. Fixed:
   `credit_gate_max_lookups_per_day` (default 200/UTC day, warn-once log
   on exhaustion).
3. **Watchlist rechecks re-spent on the same hot coin every ~7.5 min**
   (found in overflow, fixed with #2's machinery):
   `credit_gate_cooldown_minutes` (default 60) — per-token cooldown;
   forced lookups stamp it too so a gated lookup right after a forced one
   is not re-spent. Forced lookups (holdings, /check, plan/report) bypass
   budget AND cooldown — operator safety is never starved.
4. **CLI plan/report was silently gated** (overflow): operator-initiated
   deep research now passes `force_wallet_check=True` like /check.

Accepted limitation (documented, not built): the gate's freshness/ceiling
conditions also govern a lookup that powers PROTECTIVE whale-exit/insider
signals, so a watched-but-not-held coin older than 3h loses wallet-based
whale-exit detection. The protective contract runs through /holding —
holdings are always fully checked. Revisit only if the operator asks.

Suite: **844 passing** (+7).

## 2026-07-18 — Buy buttons: percent-of-balance instead of fixed SOL presets

Operator request: "remove the 0.01 and the 0.04 SOL [buttons] and replace
it with percentages. Like 20% of my sol, 50% 75% and 100%." (The fixed
presets didn't scale — a $0.05 SOL preset means something different at a
0.1 SOL balance than at a 1 SOL one, and the operator wants to size
relative to what's actually in the wallet.)

**Built:**
- `ExecutionSettings.buy_button_percents` (default `"20,50,75,100"`,
  replaces the removed `buy_presets_sol`) — validated to (0, 100] each.
- `LiveExecutor.get_spendable_balance_sol()` / `DryRunExecutor.
  get_spendable_balance_sol()`: a live RPC balance read MINUS the same
  fee/rent buffer `execute_buy` always reserves, so a 100% tap computes an
  amount that can actually clear `execute_buy`'s own balance re-check
  instead of being refused for lacking fee money. Dry-run honestly returns
  `None` (no real wallet exists — Rule 8, never fabricate a balance).
- `feedback_keyboard`/`TelegramSink`: buttons now read "Buy 20%" / "Buy
  50%" / etc., callback data `buy:<address>:pct:<percent>`.
- `_handle_buy`: resolves the percent against a FRESH balance lookup at
  the moment the button is tapped (never at alert-render time — the
  balance moves) — `sol_amount = spendable_balance * (percent/100)` —
  then proceeds through the EXACT same `_do_buy` path as before. The
  `buy:<address>:<sol>` legacy callback format is still accepted (Rule
  3/18) so any button on an alert already delivered before this change
  keeps working; only newly rendered alerts show percentage buttons.
- Deliberately did NOT touch: `max_buy_sol` (still a hard per-trade
  ceiling — a computed percentage that exceeds it is refused, exactly like
  a too-large fixed amount always was), the `/buy <address> <sol>` manual
  text command (unchanged — explicit amount, operator's call), or the
  balance re-check inside `execute_buy` (still authoritative; the
  percentage lookup is a sizing convenience, not a new spend path).

Operator flagged during this same session: wallet tracking stays OFF by
design (his call, unrelated to this change) — the July 11 credit-exhaustion
incident is reason enough not to revisit it without him asking.

Suite: **840 passing** (+13: executor balance/fee-buffer tests, keyboard/
sink rendering tests, telegram callback-handling tests including legacy-
format backward compatibility, double-tap, and off-guard-before-lookup
ordering).

## 2026-07-20 — Removed the per-trade SOL cap (operator explicit request)

Operator's percent-of-balance buy buttons (2026-07-18) kept getting
refused as the wallet grew — a 20% tap became bigger than the fixed
`MEMEINTEL_EXECUTION_MAX_BUY_SOL` ceiling set back when the wallet was
first funded. Operator: "I don't want a cap remove it." Flagged plainly
before building it (the cap was one of four pillars of the documented
"HARD SAFETY MODEL," Project 6, 2026-07-10) — operator's call stood.

`ExecutionSettings.max_buy_sol` now accepts 0 = no ceiling (same
convention as every other cap this session: `max_training_records`,
`opportunity_max_age_hours`, `credit_gate_max_lookups_per_day`, etc.);
negative/non-finite still rejected. `LiveExecutor.execute_buy` skips the
ceiling check entirely at 0. **Explicitly NOT touched:** the live
balance re-check immediately before every trade — the bot still cannot
spend SOL the wallet does not hold; that remains the sole automatic
guard with the cap off. Startup log now prints "Per-trade cap NONE
(wallet balance is the only limit)" instead of a misleading "0 SOL" when
disabled.

Suite: **841 passing** (+1).

## 2026-07-20 — Fast movers unbuyable: preflight-rejection retries + short trade errors

Operator, after removing the cap, still could not buy: every tap on a
fresh pump.fun coin came back as a wall of raw RPC JSON ("big text") and
"Do NOT retry blindly." Root cause, from the pasted live error: RPC
-32002, Jupiter custom program error 0x1771 = **slippage tolerance
exceeded** — the coin's price moved past the 5% allowance in the seconds
between quote and landing, so the node's preflight simulation refused
the transaction. Two distinct defects:

1. **Misclassification.** A -32002 preflight rejection means the node
   NEVER broadcast the transaction — definitively nothing spent — but
   the executor lumped it in with ambiguous submission errors ("may
   have gone through... do NOT retry"), so the one failure mode that IS
   safe to retry was the one being frozen.
2. **Unreadable errors.** `SolanaRpcClient._rpc` stringified the whole
   RPC error object, including the multi-KB `data` program-log dump,
   straight into the operator's Telegram reply.

Fixes:

* `SolanaRpcClient` now classifies sendTransaction errors: -32002 /
  "simulation failed" raises the new `TransactionRejectedError`
  (subclass of CollectorError) with a one-line plain-English message
  (0x1771 mapped to "price moved beyond the slippage allowance");
  every other RPC error raises a compact code+message string (~200
  chars, `data` never included). Full error detail still goes to the
  log (Rule 13).
* `LiveExecutor` buy AND dump paths now retry a preflight rejection
  with a **fresh quote** — up to `preflight_retries` extra attempts
  (new `ExecutionSettings` field, `MEMEINTEL_EXECUTION_PREFLIGHT_RETRIES`,
  default 2, range 0-10; Rule 17). A fresh quote re-centers the
  slippage allowance on the CURRENT price, which is how a fast mover
  actually gets caught. This is NOT auto-trading: it is the same
  operator-initiated intent, and it only ever re-runs when the network
  provably discarded the previous attempt. Ambiguous submission errors
  (tx may have reached the network) are still never auto-retried — the
  double-spend guard from 2026-07-10 stands untouched.
* Retries exhausted → one short reply: nothing was spent, the coin is
  moving faster than the configured slippage, raise
  `MEMEINTEL_EXECUTION_SLIPPAGE_BPS` if it keeps happening. Jupiter's
  own `simulationError` string is truncated to 200 chars too.

Suite: **848 passing** (+7: retry-then-succeed, retries-exhausted short
message, dump retries, ambiguous-error-never-retried, slippage/non-
slippage rejection classification, compact generic RPC error).
## 2026-07-20 — Wallet tracking restored as a DORMANT kit (operator request, paid-Helius plan)

Operator: "create the wallet tracking with Helius paid membership but
don't actually enable it or touch or interfere with the bot and its
thinking and scanning. Just build it, keep it aside and add simple
instructions on the read me on how to enable it and add the keys."

Done by reverting the 2026-07-18 revert (`aa3be05`), which restores the
already-reviewed credit-gated wallet intelligence build (`1ac5774` +
review fixes `a5691a4`) — Rule 18, extend rather than rewrite — with two
deliberate default changes so TODAY'S behavior is untouched:

1. `opportunity_max_age_hours` stays **1.0** (the restored commit had
   widened it to 3.0; the operator kept the 1h window when he reverted
   on 2026-07-18, so the 1h default stands).
2. `momentum_min_security_score` default **0 = off** (was 50 in the
   restored commit). The floor is part of the wallet-tracking kit: the
   enable script sets it to 50 together with the monitor flag, keeping
   it aligned with the credit gate's floor — but until then momentum
   alerts behave exactly as they do today.

What is now sitting ready, all dormant behind
`MEMEINTEL_WALLET_ENABLE_IN_MONITOR=false`: the pipeline credit gate
(`_worth_wallet_lookup`: lookups only on candidates that could still
earn a buy-side alert), spend bounds (200 lookups/UTC-day budget,
60-min per-token cooldown), and forced-lookup bypasses (holdings,
/check, plan/report always check).

New: `deploy/enable-wallet-tracking.sh` (on: takes the paid Helius key,
updates .env in place — the loader keeps the FIRST occurrence of a key,
so appending duplicates would silently do nothing — sets the monitor
flag + momentum floor 50, restarts the service; off: reverses both
flags). README gained a "Wallet tracking (smart money)" section with
the plain-language enable/disable steps. The trading account
(`MEMEINTEL_EXECUTION_HELIUS_API_KEY`) is never touched by the script —
the money path keeps its own credit budget.

Suite: **865 passing** (848 + 17 restored gate/floor tests, updated to
the dormant defaults).

## 2026-07-20 — X/Twitter community tracking built as a DORMANT kit (Roadmap item 5, LunarCrush)

Operator: "build the thing for the X community, turn it off, add
instructions [to the README]." Roadmap item 5 ("Real social intelligence
(Twitter/X via paid aggregator)") was previously parked (see "Deferred,
with reasons" above); this builds it, mirroring the wallet-tracking kit's
shape exactly (Rule 18, extend rather than rewrite) — a paid, metered data
source wired behind its own credit gate, off by default, with a README
section and an enable/disable shell script.

**New collector**: `meme_intelligence/collectors/social_data.py` —
`LunarCrushClient(BaseCollector)`, talking to LunarCrush's public API v4
(`coins/list/v1` for a 4h-cached full coin directory, `topic/{topic}/v1`
for 15-min-cached per-coin platform detail). Matches a token to a
LunarCrush coin ONLY via `blockchains[].address` on the matching chain —
never via symbol/name, since meme coins routinely share tickers across
unrelated chains and a symbol match could silently attribute one coin's
social data to a different coin (a Rule-8 fabrication bug, not just an
inefficiency; the collector's test suite includes a same-symbol,
wrong-chain regression case for exactly this).

**The Rule-8 decision this build hinges on**: LunarCrush's public v4 API
exposes aggregate, coin-level social-conversation metrics (overall and
X-specific sentiment, unique X-post counts, social dominance, "galaxy
score", rank, trend) — nothing per-account. It has NO follower count,
engagement-rate, or bot-follower-percentage data for a coin's Twitter/X
conversation anywhere in the coins/topic endpoints; those only exist for
one specific named creator under a completely different endpoint
(`/public/creator/:network/:id/v1`), out of scope. So this client maps
LunarCrush data into exactly two fields `CommunityProfile` already had
(`positive_sentiment_percent` preferring X-specific sentiment over the
coin-level fallback, `user_content_per_day` from unique X-post counts) plus
five new, honestly-scoped fields (`social_volume_24h`,
`social_dominance_percent`, `galaxy_score`, `alt_rank`, `social_trend`) —
and it deliberately NEVER sets `twitter_followers`,
`twitter_engagement_rate_percent`, `twitter_growth_rate_7d_percent`, or
`bot_follower_percent`. Populating those four from data that doesn't
describe them would be fabrication, not a coverage improvement. This is
tested explicitly (`test_never_sets_per_account_fields`) as the single
most important test in the new suite.

**Merging, not replacing**: a new `merge_community_profiles(primary,
secondary)` helper in `analyzers/community_analyzer.py` lets the pipeline
layer LunarCrush on top of CoinGecko's existing free profile —
CoinGecko's non-`None` fields always win, LunarCrush only fills gaps,
`source` becomes `"coingecko+lunarcrush"`. CoinGecko's coverage can only
ever improve from this, never regress (Rule 9). `CommunityAnalyzer`'s
scoring logic is untouched — it already reads every field this merge can
populate.

**Same credit-gate shape as the wallet layer, copied not shared** (Rule
21 — this codebase prefers duplicated-but-simple over premature
abstraction, and the wallet gate is tested/deployed and must not be
touched, Rule 3): new `SocialIntelSettings` (`enable_in_monitor`,
`credit_gate_min_security_score` 50.0, `credit_gate_max_lookups_per_day`
200, `credit_gate_cooldown_minutes` 60.0 — identical defaults to
`WalletIntelSettings`'s equivalents), and a direct copy of
`ResearchPipeline`'s `_gate_allows`/`_worth_wallet_lookup`/
`_note_wallet_lookup`/`_roll_wallet_budget_day` quartet renamed with a
`_social_` prefix. One deliberate difference from the wallet gate: NOT
restricted to Solana — LunarCrush covers multiple chains, and the
collector's own chain-normalization table already safely returns `None`
for unmapped chains, so no chain restriction is needed at the pipeline
level. `force_social_check` (mirroring `force_wallet_check`) bypasses the
gate at every site `force_wallet_check=True` or
`force_wallet_check=self._storage.is_holding(...)` already appears
(`ContinuousScanner.check_token`, the three `analyze_pair` call sites in
`_run_cycle`/`_process_launches`/`_recheck_watchlist`/
`_retry_insufficient_data`, and `__main__.py`'s shared plan/report
builder) — operator holdings and manual `/check`/`plan`/`report` always
get social data.

**Wiring**: `ResearchPipeline` gains `social_client=None`;
`ContinuousScanner` gains `social_client=None` behind the same
"metered-layer-off-unless-`enable_in_monitor`" guard `wallet_service`
already gets, and a new `self._layers["social_intel"]` entry for
`/status`. `__main__.py` gains `build_social_service()` (mirrors
`build_wallet_service()`: returns `None` when
`MEMEINTEL_LUNARCRUSH_API_KEY` is empty) wired into both the monitor
command and the shared plan/report path.

**Deploy + docs**: new `deploy/enable-x-community-tracking.sh` (on/off,
same in-place `.env` editing as `enable-wallet-tracking.sh`), a new
".env.example" block, and a README "X/Twitter community tracking — built,
OFF by default" section modeled on the wallet-tracking one — explicit
that LunarCrush's public API does not expose per-account follower/
engagement/bot-detection data, so the feature improves the sentiment and
content-volume signals it honestly can, not everything the original
roadmap imagined (Rule 8 applies to what the operator is told, not just
to what's read from providers).

**Fully dormant by default, verified explicitly**:
`SocialIntelSettings.enable_in_monitor` defaults to `False`,
`Settings.lunarcrush_api_key` defaults to `""`, so with no `.env` changes
`build_social_service()` returns `None`, `ContinuousScanner` strips a
wired-but-disabled `social_client` exactly like it already does for
`wallet_service`, and the pipeline's new social block never fires. Proven
by `test_social_layer_is_a_complete_no_op_with_default_settings` (default
settings, no client wired, `force_social_check=True` — still no
community data) and `test_social_service_off_by_default_even_when_wired`
(a real social client wired into the scanner, flag off — zero calls,
`_layers["social_intel"]` is `False`).

Suite: **900 passing** (865 + 35: collector matching/mapping/degrade
tests, settings defaults/validation/env round-trip, pipeline gate/budget/
cooldown/force-bypass tests, a full merged-profile integration test, a
LunarCrush-failure-preserves-CoinGecko test, the dormancy no-op test,
controller wiring tests, and `merge_community_profiles` unit tests).

## 2026-07-20 — Fixed 8 confirmed bugs from an external code review

Operator pasted an 11-item external code review and asked me to verify
each before acting. Investigated every claim directly (grep/read the
actual code, ran ruff and pyright myself) before touching anything:
8 were real, 2 (Helius owner-resolution cache key + zip alignment) were
already fixed in an earlier session with tests/comments proving it, and
1 (`Storage.archive()` discarding a return value) didn't match the
current code at all — no `change` variable exists in that function.
Reported the verdict, operator said fix the real ones.

`meme_intelligence/collectors/security_data.py`:
- `_flag()` mapped any unrecognized GoPlus value to `False` ("not a
  risk") instead of `None` ("unknown") — silently reading a malformed
  or novel API value on `is_honeypot`/`has_blacklist`/`selfdestruct`/
  etc. as reassuring instead of reducing confidence (Rule 8). Now only
  an exact `"1"`/`True` or `"0"`/`False` is a confirmed value.
- Burn-address detection matched the substring `"dead"` anywhere in an
  address — both EVM hex and Solana base58 addresses can innocently
  contain those four characters, silently excluding a real whale from
  concentration math. Replaced with an exact-match set (EVM ∪ Solana
  canonical burn/incinerator addresses), matching the existing
  `_RENOUNCED_OWNERS` pattern.

`meme_intelligence/core/provider_pool.py`:
- All-providers-cooling-down previously raised
  `AllProvidersFailedError` with an empty causes dict ("no providers
  available"), hiding the real reason. Cooldown skips now record why.
- Only `CollectorError`/`TransientCollectorError` were caught, so any
  other exception from a provider killed the whole failover loop —
  contradicting the pool's own "continue with the rest" contract.
  Added a bounded `except Exception` (never `BaseException` —
  `CancelledError`/`KeyboardInterrupt`/`SystemExit` still propagate
  immediately, verified by test) that logs it as a likely provider bug
  and moves on.

`meme_intelligence/collectors/base.py` + `__main__.py` (pure typing,
zero behavior change): `BaseCollector.__aenter__` now returns `Self`
instead of the base class, fixing 10 pyright errors in `__main__.py`
where subclass methods were flagged unknown after an `async with`.
`_gather_assessments()` gained an explicit return type and its three
call sites gained a narrowing `assert` before unpacking.

11 ruff issues fixed, all in test files (ambiguous loop variables, one
unused import, late imports moved to the top after checking for
circular-import risk).

Built via 4 parallel fix agents (one per disjoint file group) + 5
independent adversarial reviewers split across code-review/
security-review lenses — zero findings, nothing needed a second round.
Independently re-verified myself afterward: 921/921 passing across 3
separate runs, `ruff check .` clean, pyright 0 errors on the two typing
fix files, and every diff read line-by-line against what was promised.

Suite: **921 passing** (900 + 21).

## 2026-07-20 — Fixed mind "memory" frozen (cross-process analog reload), take 2

Operator screenshots: /mind "memory: 5815 coins" identical across 8+ hours
of ONE continuous monitor run (uptime 3h22m and 11h42m share a start), while
resolved (30066->31870), graded (29568->31372) and authority graded-rug-calls
(22620->24200) all grew. "Something isn't right, fix it."

Root-caused directly in code: "memory" = self._analog.size (the FAISS analog
index). It only grows via resolve_outcome, which runs in the backtest CRON,
never the monitor. resolved/graded/hit-rate read live from the shared SQLite
store (so they grow cross-process), but the analog index is loaded once at
monitor boot and never reloaded -> frozen for the run. Worse, the monitor's
unconditional shutdown persist() wrote that stale boot copy back over the
cron's grown file, pinning it permanently. Functional, not cosmetic: the
monitor's live p(rug) analog vote ran on a stale fraction of what the bot had
actually learned.

This is the same failure the reverted 58611d7 addressed (reverted as
collateral in the earlier frustration-driven "reverse everything", which was
really about the freshness-gate zero-alerts day, not this fix). Re-landed
cleanly against the moved tree and HARDENED after review.

First attempt reproduced a CRITICAL a review caught: a single _models_dirty
flag was latched by the monitor's own retrain_if_due (the monitor DOES retrain
every cycle -- warm-start), which re-froze the reload AND re-armed the clobber.
Final design splits ownership:
- _analog_dirty (index) vs _models_dirty (scaler/classifier/archetypes/state)
  vs the pre-existing _ensemble_dirty -- three independent guards.
- _analog_dirty set only by instant-learning inserts and a FULL rebuild; a
  warm-start leaves it untouched; persist() resets it after flushing so the
  reload re-arms.
- _maybe_reload_analog (top of evaluate_coin + get_learning_metrics) reloads a
  peer-grown index on mtime change, keyed to index_meta.joblib (written last)
  to avoid a torn view; refuses a feature-version-mismatched index (split
  deploy) and latches off; swallows torn/corrupt reads (Rule 7).
- AnalogMemory.save now atomic (tmp + os.replace) with feature_version in the
  metadata.
- Drift branch no longer force-sets _ensemble_dirty (the monitor never grades,
  so persisting its reset would clobber the cron's accuracy window).

Reviewed across security/data-integrity + concurrency-correctness; the
critical was fixed and every invariant re-verified against the resolved file.
Scope held to the learning layer (service.py + analog.py + tests) -- no
controller/__main__/cron/deploy changes; the shutdown callback stays as-is,
made safe by the ownership guard. NOTE: the FIRST monitor restart after
deploy still writes/uses the stale value once (old code runs that shutdown),
then it grows correctly from then on -- a one-time reset, not a failure.

Suite: **930 passing** (921 + 9).

## 2026-07-20 (later) — /check lifecycle banner (RUGGED/DEAD)

Operator: /check on an already-rugged or dead coin answered "zone=early"
with nothing indicating the pool was gone. Root cause understood, not a
bug: MomentumAnalyzer._entry_zone only knows age + 24h pump extension, so
a young corpse classifies as EARLY. Constraint from the operator: "don't
want it to interfere with it sending me the coins" -> display-only change
in telegram_commands._format_check_card (nothing in the scan/alert path
imports it).

New _lifecycle_line reuses the alert engine's dead floor
(alert_engine.dead_liquidity_usd — the same rule as _token_death_rule, so
/check and death alerts can never disagree): finite liquidity below the
floor => "STATUS: DEAD" as the card's second line; "STATUS: RUGGED" only
when a destructive finding or failed live sell probe confirms a blocked
exit (Rule 8 — no invented cause); unknown/NaN liquidity => no banner.
Momentum line on a dead pool annotated "(stale — pool is dead)".

Suite: **934 passing** (930 + 4).

Same day, earlier: the "zero coins since deploy" scare resolved as NOT a
regression — the 1h buy-side freshness gate is the operator's own choice
(see 2026-07-17/07-18 entries), the memory fix (823f22d) is isolated to
the learning layer, and the scanner funnel was healthy in his journal
(17-20 pools/cycle). An earlier suggestion this session to widen the gate
to 24h was WRONG and retracted before he applied it. Alert-type breakdown
grep (sinks.py "telegram alert sent" line) offered for confirming what
the delivered alerts actually are; operator moved on. /mind + /status
verified healthy post-deploy: memory growing (7633), rug precision 0.96
over 25,527 graded calls, veto EARNED+ON.

## 2026-07-20 (later still) — /check DUMPED banner (the trader's "dead")

Operator tested the fresh lifecycle banner on DrFy…pump and still saw
plain zone=early. Live DexScreener data explained it: -86% in 24h, $4.3k
mcap, but $4.9k liquidity still in the pumpswap pool — dead to a trader,
invisible to the drained-pool DEAD rule ($500 floor), which was working
as designed. Added the third state rather than bending the floor:
`AlertThresholds.check_dumped_drop_percent` (default 80, env
MEMEINTEL_ALERTS_CHECK_DUMPED_DROP_PERCENT, 0=off) → "STATUS: DUMPED —
price -86% in 24h (pool still holds $4.9K)". Display-only by contract
(only the /check card reads it — operator constraint: never interfere
with coin sending). DEAD/RUGGED take precedence on a drained pool;
unknown change/liquidity trigger nothing (Rule 8). Momentum zone note:
"(stale — coin already dumped)". Suite: **938 passing** (934 + 4).

## 2026-07-24 — Cron re-anchored to Beirut mornings (operator moved to Lebanon)

The operator noticed alerts thinning at the same time every day. Traced:
the three cron jobs clustered 12:15–13:45 UTC (backtest 12:15, daily
13:05, backup 13:45) — on the 1 GB droplet that cluster briefly starves
the live monitor, and after his move to Lebanon it landed 15:15–16:45
Beirut, mid-afternoon. A repo-wide sweep confirmed the ONLY clock anchors
are the three lines in `deploy/install-cron.sh` (no Python code has any
hour-of-day behavior; the credit-gate budgets deliberately roll on UTC
days and are untouched).

New schedule, same cadences, zero behavior change: backtest
`15 4,10,16,22` (04:15 UTC run feeds the daily report 50 min later,
preserving the old 12:15→13:05 pairing), daily `5 5` (08:05 Beirut),
backup `45 5` (08:45 Beirut). The whole cluster now sits 07:15–08:45
Beirut = midnight–2 AM US Eastern, the deadest meme-market window. Cron
stays in UTC (Rule 21 — no CRON_TZ); winter DST means everything arrives
an hour earlier locally, accepted. OPERATOR.md now records the timezone
so future sessions reason in Beirut time. Applied on the droplet by
re-running `bash deploy/install-cron.sh` (idempotent). Suite unchanged.

## 2026-07-28 — /winners: read-only "what did my winners look like" card

Operator asked to "feed it recent successful coins and see why they
succeeded beginning to end." For coins the bot watched, that study already
happens automatically (every resolved coin's trajectory is graded and
folded into the analog index/archetypes); what was missing was any way for
the OPERATOR to see it. For coins the bot never watched it stays
impossible — no historical-trajectory data source exists, and feeding a
winner's end-state would only teach "successful coins look successful."

Built under his standing constraint ("don't mess with how the bot
thinks") as a strictly read-only surface: new `/winners` Telegram command
compares the last 50 PUMP-bucket coins against the last 50+50 RUG/DUMP
coins at their EARLIEST stored snapshot (holders, liquidity, 1h volume,
buy share, top-10 concentration, dev outflow, rug-signal rate, median
peak return), medians only, "not enough data" over fabricated numbers
(Rule 8), and an explicit survivorship caveat. Nothing in the scan/alert/
learning path imports the new module; the only store change is a SQL-side
`bucket=` filter on `resolved_records` (read-only). The walk runs via
asyncio.to_thread (safe since the 2026-07-21 cross-thread store fix) with
a 15-min card cache + in-flight lock so repeated taps never stall the
event loop or re-walk the table. Suite: **948 passing** (941 + 7).

## 2026-07-28 — Pitched coins now report their outcome (interest widened + ack line)

Operator rule: "When it sends me a coin and it gets rugged I want it to
acknowledge that it was rugged. I want it to send me an alert and also
learn from it." The LEARNING half was already automatic (backtest cron ->
resolve_outcome(is_rug=True) -> RUG label, deployer blacklist, analog
memory — the 0.96 rug precision is this) — nothing changed there. The
ALERT half had a real gap: INTEREST_ALERT_TYPES contained only the two
HIGH tiers, so a coin that reached his phone as a MEDIUM pitch
(early_opportunity / momentum / smart_money_accumulation — all above his
medium delivery floor) never counted as "pitched", and its death/rug
post-mortem was demoted to LOW and filtered off the phone.

Changes (alert routing/rendering only — zero scoring/veto/learning
changes): (1) INTEREST_ALERT_TYPES widened to every buy-side type — only
DELIVERED alerts are recorded, so history membership means the pitch
actually reached him; the old "MEDIUM tiers are provisional research
notes" exclusion predates his medium delivery floor and is explicitly
overridden by this operator rule. Same-batch interest likewise now
accepts any tier. (2) New controller._acknowledge_prior_pitch: a
full-priority protective alert on a coin with a delivered buy-side alert
on record gains one reason line — "outcome of the bot's own call: this
coin reached you as <type> on <time>" — display-only, fails open.
Accepted tradeoff, stated to the operator: every pitched coin that dies
now sends exactly ONE full-priority post-mortem (the death rule already
collapses the warning pair into one event and archives); if that proves
noisy the set can be narrowed to rug-evidence types. Suite: **950
passing** (948 + 2).

## 2026-07-28 — /dev: deployer rap-sheet lookup (read-only)

Operator: "is there a way to search the developer of a coin and see if
he's known for rug pulls." Automatic detection already existed at three
layers (GoPlus same-creator honeypot count in the security score and
safety checklist; the learning layer's confirmed-rug deployer blacklist;
the rug engine's deployer signal vetoing buy-side alerts) — what was
missing was any operator-facing lookup. New `/dev <address> [chain]`
Telegram command: resolves a coin to its recorded deployer (or accepts a
wallet address directly), then renders the bot-witnessed rap sheet —
coins watched from that wallet with outcome counts (rug/dump/flat/pump,
unresolved reported honestly), the confirmed-rug blacklist entry with
last-seen date, and the latest coins. Read-only: three new SELECT-only
LearningStore methods (creator_of, coins_by_creator, blacklist_entry);
nothing in the scan/alert/learning path changed. A coin the bot never
watched reports "no deployer on record" rather than guessing (Rule 8).
Suite: **954 passing** (950 + 4).

## 2026-07-28 — Fix train/serve skew: evaluate coins on their trajectory

The analog index and the LightGBM classifier are TRAINED on full-trajectory
fingerprints — slope, volatility, and acceleration summarized across a
coin's whole snapshot series (features.py). But every live call site passed
a SINGLE fresh snapshot, so evaluation produced a fingerprint whose every
slope/volatility feature is zero — a vector shape the training set never
contained. service.py's own comment admitted it ("a 1-2 snapshot
trajectory barely has a shape yet"). The models were being asked to judge
coins in a representation they were never trained on, which depresses all
measured directional skill (the 0.13 hit rate is a floor, not a ceiling).

Fix: opt-in `evaluate_coin(include_stored_history=True)` merges the coin's
stored trajectory with the caller's fresh snapshot, deduped by
age_seconds (the caller's read wins a collision), bounded by the new
`learning.max_evaluation_snapshots` (200) via a new `snapshots_for(limit=)`
that keeps the most recent N. Enabled at the four production call sites
(veto, learning feed, /check, mind CLI); every other caller — including all
tests that already pass a full series — is unaffected.

Risk handling, because this changes inputs to the ARMED p(rug) veto:
merging FAILS OPEN (any store problem returns the caller's snapshots, so a
history read can never break the veto), and first sightings are provably
unchanged — at first sight the store holds at most the same snapshot the
caller passed, so the merged series equals the passed one and the
insert-once graded prediction that feeds /mind and the ensemble weights is
undisturbed. Only re-evaluations (veto on a re-checked coin, /check,
watchlist rechecks) gain the richer input, which is exactly where the
trajectory exists to be seen. Suite: **958 passing** (954 + 4).

## 2026-07-29 — Implausible forward returns were being taught as PUMPs

Live data (operator's droplet, 147,676 outcome rows): 624 rows (0.42%) carry
impossible returns — top offenders +1.7e11%, +1.1e11%, +4.0e9%. Cause: the
forward return is `100*(price-base)/base` against the token's FIRST recorded
price, with no plausibility guard. Observed pairs: base 3.70e-11 -> later
0.0634 (Agamemnon), base 8.13e-09 -> later 9.000048 (W26), base 1.31e-07 ->
later 5.28 (USOH). One of the two prices in each pair is a bad datum — the
"later" values clustering at ~$5.00/$9.00 do not look like memecoin prices.

Why it mattered beyond a broken scoreboard: `refresh_outcomes` feeds the same
`change` to `learning_service.resolve_outcome`, and `_bucket_for_return`
labels anything >= pump_return_percent (50) as PUMP. So a few hundred coins
that never pumped were written into the analog index and the classifier's
training set as winners.

Fix: `BacktestSettings.max_measurable_return_percent` (0 disables; validated
to exceed success_price_change_percent). Beyond the ceiling the return is
discarded as UNMEASURABLE — the outcome row is still written for audit, but
with `price_change_percent=None`, which makes the existing `if change is not
None` learning guard skip it for free.

**Ceiling corrected same day by the operator: "woah woah a 1000x return is
possible."** He was right — the first default (100,000% = 1000x) sat exactly
where his best real outcomes live, and discarding a genuine monster would
delete the single most valuable record this system can hold. Re-anchored on
physics from a ~$20k detection (discovery floor is $15k liquidity): $1B peak
= 50,000x = 5.0e6%; a DOGE-tier $10B = 500,000x = 5.0e7%. Observed corruption
starts at 26,000,000x (2.7e9%). Default is now 100,000,000% (1,000,000x) —
~20x above a DOGE-tier miracle and ~27x below the smallest impossible value.
Parametrized tests pin 20x / 1000x / 50,000x as must-survive cases.

Scope correction, stated to the operator: at 0.42% contamination this is a
real data-integrity bug worth fixing, but it does NOT explain the reported
0.13 hit rate — an earlier message overstated its likely impact before the
magnitude was known. Fix is forward-only; ~624 historical rows are left in
place rather than risking a migration on a live DB for 0.4% of the data.
Suite: **964 passing** (960 + 4).

## 2026-07-29 (later) — Review of the return guard found it deleted real RUGs

A 5-lens review + adversarial verification of `1cf3aef` found three defects
in the guard itself. All fixed; the critical one made the guard a net
NEGATIVE for a rug-veto system.

1. **CRITICAL — the guard deleted confirmed rugs.** `_bucket_for_return`
   checks `is_rug` FIRST, so an implausible return on a DRAINED pool never
   produced a wrong PUMP — it produced a CORRECT RUG. Nulling `change` then
   tripped the pre-existing `if change is not None` gate and skipped
   `resolve_outcome` entirely, taking the rug with it. Since
   `refresh_outcomes` is the ONLY caller of `resolve_outcome`, and that is
   the only path to `blacklist_deployer` / `_on_rug_upgrade`, the coin was
   never resolved at all: no RUG label, no rug fingerprint in the analog
   index, no graded rug call for the ARMED veto's earned authority, and no
   deployer blacklisting behind `/dev`. Worse, `base_price` is fixed per
   token (MIN snapshot id), so a bad baseline made EVERY window exceed the
   ceiling — the coin went permanently invisible. Reproduced by execution
   before and after. Fix: `resolve_outcome` accepts
   `forward_return_percent: float | None`, returns early only when the
   magnitude is missing AND it is not a rug; the backtester now feeds it
   when `change is not None OR survived is False`. A rug resolves with an
   honest `None` magnitude instead of a fabricated one.
2. **HIGH — NaN bypassed the guard.** `abs(nan) > ceiling` is False, so a
   NaN price sailed past, `change is not None` held, and
   `_bucket_for_return` returned FLAT (`nan >= 50` and `nan <= -50` are both
   False) — while SQLite stored NaN as NULL, so the audit trail and the
   training set disagreed. Fix: reject non-finite unconditionally, NOT
   behind `ceiling > 0` (else disabling the ceiling reopens the hole).
3. **MEDIUM — an unmeasurable row burned its window forever.**
   `INSERT OR IGNORE` + `if window in existing` meant one transient bad tick
   permanently deleted that window's evidence, including a real winner's.
   Fix: `record_outcome` upserts ONLY over a NULL `price_change_percent`, and
   the skip test now checks for a real value — a settled window still stands
   (Part 24 S14), an unmeasurable one stays open for a later good reading.

Suite: **971 passing** (966 + 5).

## 2026-07-29 (later still) — Bug hunt: the discovery path could never alert

A 10-agent bug hunt across five dimensions (liveness, recent-diff,
data-integrity, security, resources) found the cause of the recurring
"Telegram goes quiet" incidents. Two fixes shipped; the hunt's remaining
findings are recorded below as not-yet-verified leads.

1. **CRITICAL — GeckoTerminal reports no market cap, so every
   discovery-path coin was judged untradeable.** `AutomationRules
   ._untradeable` read only `pair.market_cap`, and GeckoTerminal's
   `new_pools` feed — the main discovery source — returns
   `market_cap_usd: null` for *every* pool while always populating
   `fdv_usd`. Verified live 2026-07-29: 0/20 vs 20/20. Running the real
   parser and the real `AutomationRules` over a live payload, 20/20 pools
   returned `_untradeable() == True`, 3 of which cleared the $5k discovery
   liquidity floor — so real candidates hit this gate every cycle and had
   all buy-side events stripped at `notification_engine.py:330`, while the
   cycle log still read "N candidates, N analyzed".

   This is why alerts arrived in bursts: the pump.fun feed, the watchlist
   re-check and the insufficient-data retry all resolve pairs through
   `MarketDataService` (DexScreener first, which *does* return `marketCap`),
   so those paths kept alerting. Only discovery was dead.

   `TokenAnalyzer._effective_mcap` had used a market_cap-else-FDV fallback
   for valuation since Part 7, so the **scorer and the alert gates silently
   disagreed about what a coin is worth**. Resolution: promote that rule to
   `DexPair.effective_market_cap` (Rule 18 — extend, one definition) and
   read it from every gate that asks a coin's size — `_untradeable`,
   `_oversized`, `_market_cap_check`, and the two paid-credit gates in
   `ResearchPipeline`, which had been refusing wallet/social lookups on the
   same coins.

   Not a Rule 8 fabrication: FDV is a *measured* provider value, absence
   still yields `None`, and a coin with neither field is still blocked
   (confirmed against the live payload — the one pool still blocked has
   $0 liquidity, stopped by the liquidity gate). FDV >= market cap by
   construction, so every ceiling check stays conservative. The checklist
   line renders "Market cap (FDV)" when that is the source, so no figure
   reaches the operator with hidden provenance.

   **Why the green suite never caught it:** every fixture hardcodes
   `market_cap=400_000.0`, and
   `test_missing_liquidity_or_market_cap_is_never_sent` passed
   `market_cap=None` *alone*, leaving `make_pair`'s default `fdv=420_000`
   in place — it asserted that a coin with a KNOWN valuation was
   untradeable, never testing the case its own docstring claimed. The
   fixture now nulls both fields and still pins the Rule 8 requirement.

2. **CRITICAL — provider outages were recorded as token deaths, and then
   as rugs.** `MarketDataService.get_best_pair` returns `None` for two
   opposite things: no provider knows the token, and *every provider
   failed* (it swallows `AllProvidersFailedError` internally,
   `market_service.py:77-81`). `_live_measurement` read that `None` as
   "the token is dead — that IS the outcome" and returned a fabricated
   measurement of price $0 / liquidity $0.

   Verified by execution against the real service: a total outage yields
   `change = -100.0`, `survived = False`, `is_rug = True`,
   `OutcomeBucket.RUG` — and `_on_resolved` then blacklists the deployer
   **permanently**. A 30-second DNS blip on the droplet fails DexScreener
   and GeckoTerminal together, and the backtest cron runs 4x/day, so one
   bad minute mass-labelled healthy coins as rugs and blacklisted innocent
   deployers, writing straight into the memory the ARMED p(rug) veto draws
   its earned authority from.

   Fix: call `get_token_pairs`, which *propagates*
   `AllProvidersFailedError`, and keep the two cases apart. An outage
   records nothing and leaves the window open for a later good reading
   (Rule 8); an empty-but-successful lookup keeps its established meaning
   of a dead token. Logged at WARNING instead of silently fabricating
   (Rule 13). Rug detection itself is unchanged — a token that genuinely
   lost its pair still resolves as a RUG, pinned by a control assertion so
   this guard can never be mistaken for suppressing real rugs.

Suite: **979 passing** (971 + 8).

### Review of the two fixes (6 agents, 3 confirmed / 9 refuted)

3. **HIGH — one provider's silence was recorded as a token death.** Predates
   both fixes and is unchanged by them, but it defeated the point of fix 2.
   `ProviderPool.call_with_provider` returns the first result that does not
   RAISE, and an empty list does not raise. DexScreener answers HTTP 200
   `{"pairs": null}` for an unindexed token (verified live), parsed to `[]`
   at `market_data.py:243` — so GeckoTerminal is never asked. For a coin
   discovered THROUGH GeckoTerminal, "DexScreener has not indexed it" was
   indistinguishable from "no market left", producing a fabricated -100% /
   RUG and a permanent deployer blacklist that `record_outcome`'s
   NULL-only upsert could never correct. New
   `MarketDataService.get_token_pairs_confirmed` sweeps the remaining
   providers on an empty answer, reports `[]` only when every provider
   answered empty, and raises when emptiness is unconfirmed. Sweep runs only
   on the empty path (Rule 11). **The rug decision rule is untouched** — only
   the evidence standard for concluding death changed.
4. **MEDIUM — FDV could score a green PASS against the market-cap floor.**
   FDV >= market cap, so below the floor it is conclusive (warn is sound) but
   above it proves nothing: locked/vesting supply gives FDV $420k on a true
   $21k cap, and a pass counts toward the "passed X/Y" header. That case now
   renders `unknown` — shown, not scored. `_untradeable` and the ceiling stay
   on `effective_market_cap`.
5. **MEDIUM — the mutating branch was the quiet one.** The outage branch
   (records nothing) logged WARNING while the death branch (permanently
   blacklists a deployer) logged nothing, making a run that blacklisted N
   wallets byte-identical in the log to a healthy one. Now WARNING (Rule 13).

Refuted on inspection: the challenge to the edited fixture in
`test_missing_liquidity_or_market_cap_is_never_sent`; the claim that an
attacker-chosen FDV buys an alert the old code would have denied; that the
FDV fallback starves the paid-credit budget or arms uncapped AI verification;
that `_oversized` contradicts its docstring; and that `effective_market_cap`
should also fall back on `0`/NaN.

Suite: **986 passing** (979 + 7).

### Open leads from the same hunt (NOT yet verified — do not treat as fact)

The hunt's spend budget ran out before 4 of its 5 adversarial verifiers
ran, so the following are raw finder output with a known-high false-positive
rate. Verified-and-unfixed items first:

- **Alert suppression is entirely unlogged.** `AutomationRules` has no
  logger at all, so `_untradeable` / `_oversized` / `_too_old` delete
  buy-side alerts with no trace at any log level. This is precisely what
  made finding 1 undiagnosable from a phone. Fixing it is the highest-value
  observability work outstanding (Rule 13).
- **`get_learning_metrics` walks the whole table on the event loop.**
  `resolved_records()` is called with no limit (`service.py:768`), plus 2
  SQL queries per resolved coin, from `/mind` and from the veto gate every
  30 min. `/winners` passes `limit=`; `/mind` never got the same treatment.
- **Telegram updates are handled strictly serially** — a slow `/check`
  blocks a following `/dump` from even being downloaded from Telegram.
- Filtered-by-min-priority alerts are recorded as *delivered*; SIGTERM is
  not honored during the cycle/backoff sleep (90s hang then SIGKILL); a
  typo'd `MEMEINTEL_LOG_LEVEL` crashes the process into an unbounded
  systemd restart loop; `/status` reports layer *wiring*, not health.

Unverified finder claims worth checking before acting: Telegram auth is
chat-scoped rather than user-scoped; `deploy/setup.sh` may create a
world-readable `.env` holding the trading wallet key; HDBSCAN holding the
GIL through `asyncio.to_thread`; several claims against the 2026-07-28/29
backtesting commits. Raw output is not committed — re-run the hunt to
regenerate it.

## 2026-07-29 (evening) — "Fix all the bugs": seven verified findings closed

Every item below was verified against the code (and in several cases by
execution) before being touched. Nothing here alters scoring, the alert veto,
or rug DECISION logic — the operator's standing constraint.

1. **`AutomationRules` had no logger at all.** Every buy-side alert it deleted
   vanished at every log level; the journal read "N candidates, N analyzed,
   0 alerts". This is what hid the GeckoTerminal blackout for days.
   Suppression now names the gate and the tripping value (unknown renders as
   "unknown", never `$0`), covering risk veto / untradeable / oversized /
   too_old / decline. A batch that sends everything stays silent.
2. **A typo in `MEMEINTEL_LOG_LEVEL` crash-looped the service.**
   `setup_logging` is the second statement of `_run`; `ValueError: Unknown
   level` escaped `main()` before any handler or sink existed, and
   `Restart=always` + `StartLimitIntervalSec=0` restarted it every 10s
   forever with Telegram silent. Unrecognized levels fall back to INFO with a
   warning emitted *after* the handlers exist.
3. **Filtered alerts were recorded as delivered.** A sink returns `None` for
   an event below its min priority; `dispatch` only excluded `False`. The
   interest gate and `_acknowledge_prior_pitch` read that history, so both
   could cite a pitch the operator's phone never showed him. Fixed without
   losing the audit trail the interest gate deliberately creates (demoted LOW
   protective alerts still belong in history): additive `delivered` column
   (NULL on existing rows = delivered), `dispatch_detailed()` returning
   `(delivered, filtered)` with `dispatch()` unchanged, and
   `alert_history(delivered_only=True)` at both consumers. A filtered event no
   longer stamps the cooldown.
4. **SIGTERM did not interrupt the sleeps.** `request_stop` only set an event,
   and binding it to SIGTERM replaces the default disposition, so the process
   no longer dies on SIGTERM; the backoff reaches 300s against systemd's
   invisible 90s default. A phone-issued `systemctl restart` during an outage
   meant 90s of apparent hang then SIGKILL, skipping the exit-stack unwind.
   Both sleeps race the stop event; the unit states `TimeoutStopSec=45`.
5. **`/status` reported wiring, not health.** `_layers` is built once in
   `__init__` and never mutated, so pump.fun read "ON" while its WebSocket had
   been dead for hours — `PumpPortalClient.connected` existed and nothing read
   it. Added a `health` block (connected state + when an alert last reached an
   external sink) rendered as "ON but DISCONNECTED" / "last alert delivered".
6. **The mind-layer metrics walk was ~84,000 queries on the event loop.**
   `get_learning_metrics` walked `resolved_records()` then issued `coin_id()` +
   `get_prediction()` per coin — three queries each plus a full snapshot-history
   load it discarded, since it reads only the bucket and the payload. Run
   synchronously by `/mind` and the veto gate every 30 min. Replaced with one
   JOIN (`LearningStore.graded_predictions`). Measured on 3,000 coins x 12
   snapshots: **0.437s -> 0.018s, 25x, byte-identical rows**, pinned by an
   equivalence test. Deliberately NOT limited — this accuracy is what earns the
   p(rug) veto its authority, and truncating it would move that operating point.
7. **A slow `/check` blocked an emergency `/dump`.** `_poll_once` awaited each
   handler inline and `_poll_forever` only re-polls after it returns, so a
   multi-minute `/check` meant a following `/dump` was not even *downloaded*
   from Telegram. Handlers now run as supervised tasks, capped at 8 in flight
   (past which dispatch runs inline as backpressure), with a `_trade_lock` at
   the `_do_buy`/`_do_dump` chokepoint so money-moving commands keep their old
   strict serialization. Shutdown grants a 5s grace period before cancelling.

### Security findings closed

- **Telegram authorization was chat-scoped only**, and `.env.example` tells the
  operator to "add it to your group/channel" — every group member inherited
  `/buy` and `/dump`. Added optional
  `MEMEINTEL_TELEGRAM_COMMANDS_ALLOWED_USER_IDS`; empty preserves the old
  behaviour (correct for a private chat, where chat id IS user id). Startup
  warns when the chat id is negative and no allow-list is set.
- **`deploy/setup.sh` left `.env` world-readable** (0644 via `cp` under umask
  022) holding the trading wallet private key. Now `chmod 600` on every run.
- **API keys were passed as `$1`** to the enable-* scripts, landing in
  `~/.bash_history` and `/proc/<pid>/cmdline`. They now prompt with `read -rs`.
- **`AlertEvent.render()` interpolated the token symbol raw** into stdout ->
  journald; a symbol is attacker-chosen on-chain metadata, so ANSI escapes and
  newlines could rewrite the terminal and forge log lines. `_sanitize_identity`
  moved into `notification_engine` (sinks imports it, not the reverse).
- **An unreadable wallet balance read as 0 SOL.**
  `get_sol_balance_lamports` returned a hard `0` when the RPC result was
  missing, so a funded wallet was told "Refused: wallet holds 0.0000 SOL" and
  `get_spendable_balance_sol` returned `0.0` instead of the `None` its own
  docstring promises. Now raises `CollectorError`; a genuinely empty wallet
  still reads 0.
- **`run()`'s cycle history grew forever** — one `CycleStats` per cycle, each
  holding every delivered `AlertEvent`, inside a loop the daemon never exits.
  ~1,900 entries/day against `MemoryMax=880M`. Now a bounded deque.
- **`/dev` defaulted to chain solana**, reporting "never watched" for EVM coins
  the bot had analysed. It now infers the chain from the address format.
- **Two more Rule 8 holes in the outcome recorder.** Non-finite liquidity was a
  confirmed drain (`nan >= floor` is False -> `survived=False` -> permanent RUG
  + deployer blacklist) — the same NaN hole closed for `change` in `36b16ba`
  but missed for `liquidity`. And an unsettled row could not record a confirmed
  drain, leaving the audit row and the mind layer disagreeing about one event.
  The first attempt at the second fix was wrong and an existing test caught it
  (it let a SETTLED window be overwritten); the guard is now scoped to
  unsettled rows only.

Suite: **1020 passing**.

### Outstanding — these need an operator decision (they change what the bot LEARNS)

Verified as real, deliberately NOT fixed without consent:

- **Analog index labels use the FIRST resolved horizon.** `_on_resolved` fires
  when a coin first resolves — usually the 1h window — and
  `_refresh_final_bucket` returns RUG-override else the LONGEST horizon. So a
  coin that was FLAT at 1h and PUMP at 720h carries a FLAT fingerprint in the
  analog memory forever (unless it later rugs), while `_rebuild`,
  `compute_metrics` and `veto_gate` all use the 720h `final_bucket`. Partially
  self-healing: a FULL rebuild re-labels the index; a warm-start does not.
- **Training fingerprints see the post-prediction trajectory.** `_on_resolved`
  and `_rebuild` extract from `snapshots_for(coin_id)` with no limit, including
  snapshots captured after the label horizon. At serve time only the
  pre-decision trajectory exists. Same family as the Fix-1 train/serve skew.
- **The rug engine cannot abstain** — "no evidence" publishes as P(rug)=0.0.
  Fixing this in isolation was tried (`781d009`) and REVERTED (`83c6eff`)
  because abstention shrinks the rug source's accuracy sample and roughly
  doubles its blend weight, moving the ARMED veto's operating point unannounced.
  Any retry must handle the weight shift in the same change.
- **The interest gate now includes `momentum`** (`21d5467`, built to the
  operator's "tell me when a coin you sent me rugs" rule). This codebase calls
  momentum "the largest alert category by far (thousands/day)", so one delivered
  momentum alert grants a coin permanent interest and un-gates every protective
  alert on it. That is the operator's stated preference vs. re-warn spam — his
  call, not a defect to silently revert.
- **A re-opened window can flip a RUG label, but the deployer blacklist it
  created is permanent** (`mark_deployer_counted` is one-way).
- **Live-fetched outcomes have no staleness bound**: the snapshot path rejects
  measurements outside the window tolerance, the live path records whatever the
  price is at cron time.
- **The backtest cron never reloads the analog index mid-run**, then persists
  its boot-time copy over any rebuild the monitor performed.
- **Unmeasurable windows are re-measured on every cron run forever** — a
  permanently corrupt baseline price can never produce a return, so it costs a
  live API call four times a day indefinitely (Rule 11).

### Could not verify in this environment

- **HDBSCAN may hold the GIL through `asyncio.to_thread`.** The retrain is
  already dispatched off the loop with that exact intent; the claim is that
  hdbscan's Prim's/KD-tree path never releases the GIL, so the loop freezes
  anyway. `hdbscan` is not installed in the review sandbox — needs measuring on
  the droplet before acting.
- **`RotatingFileHandler` on a shared log file.** The cron jobs redirect stdout
  to separate files, but `setup_logging` still installs a rotating handler on
  `logs/meme_intelligence.log` in every CLI process, which the daemon also
  holds open. Rotation from a cron process could rename the daemon's file. Low
  impact (some lines land in a rotated-away file); not worth a risky change
  without evidence it is happening.

## 2026-07-29 (night) — Live rug guard on open positions (auto-sell armed)

The operator lost a position to a rug and asked for a new rug engine. Before
building one, three things were established about the existing one — it is not
badly designed, it is starved:

1. **The hard rug engine is blind to liquidity at alert time.**
   `_deterministic_risk_veto` (controller.py) builds a SINGLE `CoinSnapshot`
   with four fields — `age_seconds=0.0`, `volume_1h_usd`, `holder_count`,
   `dev_outflow_usd` — and no `liquidity_usd` at all. `_check_liquidity_removal`
   needs a trajectory, so the signal that actually catches a rug pull can never
   fire on the alert path. Only contract facts, deployer reputation and dev
   outflow can. A competent rugger passes all of those and then pulls the LP.
2. **The learned P(rug) veto is off by default** (`veto_enabled=False`), so
   screen #3 — the one backed by the graded-call history — never votes unless
   the operator sets it.
3. **The dev-dumping signal needs the Helius wallet lookup**, which is
   credit-gated, so `dev_outflow_usd` is `None` for most coins and that signal
   cannot fire either. (The operator noted the Helius key is in use; the key is
   plugged in, the data mostly is not reaching the rug engine.)

**Operator decisions (asked before building, Rule 20):** protect money already
committed rather than only improving pre-alert screening, and *"auto-sell, then
tell me"*.

### What was built

`analyzers/rug_watch.py` — pure verdict engine (hold / warn / exit) over a
liquidity trajectory plus sell-route status. No I/O, no clock, no database, so
every threshold and confirmation rule is testable.

`workflow/holdings_guard.py` — a fast poll loop over open positions only,
which can call `execute_sell_all` by itself.

**Why a second engine (Rule 4/18/19):** the existing `learning/rug_engine.py`
answers "would this coin rug?" *before* an alert. This answers "is it rugging
right now, and can I still get out?" Prediction and detection have opposite
error costs — an over-eager screen costs a missed opportunity, an over-eager
watcher sells a healthy position — so they get separate thresholds and separate
code. **Nothing in the pre-alert rug engine, the scoring engine or the veto was
changed.**

**The asymmetry that shapes the design:** a vanished sell route is the END of a
rug, not the start — once Jupiter finds no path out, an auto-sell is futile.
The actionable moment is liquidity draining hard *while a route still exists*.
So route-gone warns loudly (nothing to execute) and a confirmed liquidity
collapse with a live route triggers the exit.

**Safety model**, because this is the only place the bot spends money without a
tap:

- Armed twice or not at all: `MEMEINTEL_RUG_WATCH_ENABLED` starts the watch,
  `MEMEINTEL_RUG_WATCH_AUTO_SELL` lets it trade. A half-armed config raises
  `ConfigurationError` at startup rather than looking armed.
- Positive evidence only (Rule 8): a failed read, unknown liquidity or an
  unprobed route never move the verdict. Uses `get_token_pairs_confirmed`, so
  "every provider agrees there is no pair" is a measured zero while "nobody
  could answer" is not — the exact distinction the 2026-07-29 outage fix added.
- `min_confirmations >= 2` is enforced by config validation: one provider tick
  must never be able to liquidate a position.
- Once per position; the attempt is recorded BEFORE the trade so a crash
  mid-sell cannot double-sell.
- `/rugwatch off` disarms from the phone. Re-arming needs an `.env` edit and a
  restart — a deliberate speed bump.

### The gap that would have made it useless

`set_holding` was only ever called by `/holding`. A coin bought through `/buy`
or an alert's Buy button was **never marked as held**, and the guard, the
interest gate and protective-alert priority all key off holdings — the bot
could buy a coin and then never guard it. A live buy now registers the position
at **broadcast** (not confirmation: a buy whose confirmation times out has very
likely landed, and is exactly the coin most in need of watching); a **confirmed**
sell releases it. Both directions err toward keeping the watch.

### Still open on the pre-alert side

Feeding the hard rug engine the real stored liquidity trajectory at veto time
(finding 1 above) is a contained change that would light up
`_check_liquidity_removal` on the alert path. NOT done here: it changes which
coins get vetoed, i.e. how the bot thinks, which needs the operator's word.
Same for arming `veto_enabled`.

Suite: **1062 passing** (1020 + 42).

## 2026-07-29 (late) — "Still sending me dumb coins": the score rewards ignorance

Two rounds of threshold tuning did not fix it, which was the clue. The cause
is structural, not a matter of numbers.

`compute_weighted_score` renormalizes over the categories that HAVE data
(`total = weighted_sum / available_weight`). A coin nobody can measure is
therefore scored on whichever one or two categories resolved — and outranks a
fully-analysed coin. Measured on the real pipeline:

| coin | score | coverage |
|---|---|---|
| only `security` measurable | **90.0** | 15% (classified `elite_opportunity`) |
| fully-analysed decent coin | 74.0 | 100% |
| fully-analysed GREAT coin | 88.3 | 100% |

And through the actual `ResearchPipeline`: deleting every volume/trade field
from a healthy coin **raised** its score from 92.9 (70% coverage) to 93.3
(55%). **The less the bot knows about a coin, the better it looks** — and
brand-new junk is what it knows least about.

This is why raising `MEMEINTEL_ALERTS_OVERALL` made things worse: at 85 it
removes the fully-analysed 88.3 coin before it touches the 90.0 unknown.
Tightening the score threshold selectively deletes the GOOD coins.

**The asymmetry that makes it a bug rather than a preference:**
`workflow.insufficient_data_min_coverage` (0.5) already encodes that below
half coverage an AVOID is "a data gap, not a verdict" and must not be
trusted. The identical evidence was still trusted to conclude ELITE
OPPORTUNITY. Refusing to conclude "bad" while happily concluding "great" from
the same thin data is indefensible under Rule 8.

### Fix

- `AlertThresholds.min_coverage` (0.0 = OFF, Rule 18) suppresses buy-side
  alerts below the floor; unknown/non-finite coverage counts as below it.
  Protective warnings are never floored.
- **Every buy-side alert now names its own coverage and what was missing**,
  floor or no floor — `⚠️ Evidence: only 55% of the framework had data
  (missing: community, momentum, narrative)`. The score alone cannot
  distinguish "great coin" from "coin we could barely measure"; this makes
  the difference visible on the phone. Below 70% it is scored as a MISS in the
  "passed X/Y" header rather than a silent note.
- The suppression log names the coverage floor that blocked a coin.

Recommended `.env` value 0.65: blocks a coin missing narrative on top of
community/foundation (55%) while keeping a normal fresh launch (70%). Left OFF
by default because the last two strictness changes were my judgment rather
than measurement, and the operator has been silenced once already by a guess.

### Also this round

- `hard_min_liquidity_usd` / `hard_min_market_cap_usd`: floors that actually
  SUPPRESS. The existing `opportunity_min_*` floors only ANNOTATE (2026-07-12
  "send it through and let me know") — but the settings comment claimed they
  suppressed, so anyone setting them got no blocking at all. Comment corrected;
  hard floors added alongside rather than changing what the old ones mean.
- `momentum_min_security_score` documented in `.env.example`: momentum is the
  largest alert category and ships with NO security floor, so a mintable /
  freezable / unlocked coin can fire one purely for going up. The guard exists
  in code ("rising price on a coin with bad security is bait, not a signal")
  and was simply never switched on.

Suite: **1079 passing**.

## 2026-07-29 (late night) — The bot is blind to the two facts that decide a rug

The operator sent a rugcheck.xyz screenshot of a coin his bot had pitched him:
**DANGER 65**, single holder **88.45%**, LP **100% unlocked**. His bot scored
that coin's security a **perfect 100.0/100** and its rug engine fired nothing.

### Diagnosis (proven, not inferred)

Neither component was wrong about the facts it had. `GoPlusClient._parse_solana`
returns `holder_count=None`, `top_holder_percent=None`, `lp_locked_percent=None`
for a fresh pump.fun mint — GoPlus does not index them that early. So of the
four security sub-scores only `contract` resolved (authorities cleanly
renounced), and `SecurityAnalyzer` renormalizes over the sub-scores that HAVE
data, making `contract=100` the entire score at **25% coverage**.

`RugEngine` behaved correctly too: its `top_holder_concentration` and
`liquidity_unlocked` signals refuse to fire on `None` (Rule 8). They are fully
written and weighted and **have never fired once**, because they have never had
a number to look at.

His own database confirmed this is universal, not one bad coin
(`deploy/security_evidence_report.py`): of **500 coins he was alerted about,
499 sat at 25-49% security coverage**, and security **passed 100% of buy-side
alerts**. It is not a gate, it is a constant.

### A coverage cap was tried, measured, and removed

The first fix attempt capped the security score by its own coverage
(`50 + 50 * coverage`), mirroring `RiskAnalyzer._MIN_COVERAGE_FOR_LOW_RISK`
which has enforced exactly that since Part 9 ("Unknown is not safe"). It was
measured against the live database **before** being switched on, and the
measurement killed it twice over:

1. **It would have silenced him.** Of the 339 coins that cleared the security
   gate, the cap blocked **338 — 99%**.
2. **It was the wrong shape, not merely mis-tuned.** With the real facts
   present:

   | | known-bad (88% holder, 0% LP) | known-good (3%, 95%) | separation |
   |---|---|---|---|
   | no cap | 73.8 **blocked** | 100.0 passes | **26.2** |
   | cap on | 73.8 blocked | 82.5 passes | 8.7 |

   The existing scoring already separates good from bad by a wide margin the
   moment it HAS the facts; the cap *compressed* that. An adversarial review
   independently found why: because the ceiling rises with ANY evidence, a coin
   whose 88.45% concentration is **confirmed** outscored the same coin with that
   fact unknown. Rewarding the discovery of damning evidence is not a threshold
   to tune.

Deleted rather than left disabled — no dead mechanism, no latent bug surface,
no perverse gradient (Rule 21). The same review also found, on that code, that
the setting's VALUE was inert (only zero vs non-zero had any effect, against
its name and docstring) and that a float-accumulated `available_weight < 1.0`
could cap a fully-measured coin. Both died with it.

**Do not re-introduce a coverage cap without re-running the measurement.** The
tool that produced the 99% figure (`security_cap_impact.py`) was itself removed
because, once the setting was gone, `Settings.from_env` silently ignored the
unknown variable and the script compared a config against itself — it would have
printed "now BLOCKED: 0 (0%)", the exact inverse of the truth, and read as
clearance to ship the cap back on.

### The actual conclusion

**The bug was never the scoring.** It is that the facts are missing. Collect
holder concentration and LP status from chain and the existing analyzer blocks
the screenshot coin at 73.8 on its own, with no new mechanism, and two dormant
rug signals finally get inputs.

`tests/test_security_thin_evidence.py` pins both halves: today's behaviour
honestly (the coin scores 100 at 25% coverage and IS pitched) and the acceptance
criterion for the fix (with the facts known it must fall below the gate). The
fixture is the real captured GoPlus payload.

### Next, and not yet built

On-chain collection of top-holder concentration and LP burn/lock status.
Design notes and the traps identified so far:

- **The exclusion trap.** Raw largest-account balances are NOT concentration.
  The LP vault, the incinerator/burn address and pump.fun bonding-curve accounts
  routinely hold most of supply and are not "a holder". Counting them makes every
  healthy coin look 90% concentrated, which would silence the operator a third
  time. This is the single highest-risk part of the work.
- **LP scope honesty.** The verified `lpReserve` burn formula (Raydium AMM v4,
  offset 720) does not transfer to CPMM/CLMM/Whirlpool/DLMM, where it would read
  a meaningless integer and print it as a percentage. Those must return `None`.
  Pre-graduation pump.fun coins have no LP pool at all.
- **Burned vs locked** are different claims. Burned is verifiable without trust;
  verifying a lock needs a locker program layout. Be explicit about which
  `lp_locked_percent` actually means.
- Must ship OFF and be measured with `deploy/security_evidence_report.py` before
  being switched on — the same discipline that caught the cap.

The operator supplied a reference implementation (MIT-licensed `Rug-check`) that
computes both from chain and whose 94 tests pass; its offsets are claims to be
verified live, not facts to be copied.

Suite: **1085 passing**.

## 2026-07-30 — On-chain holder concentration + LP burn: built, shipped OFF

The open project from the previous handoff. GoPlus returns no holder and no LP
data for a fresh mint, so the two facts that decide a rug were `None` on 499 of
the operator's 500 alerted coins, the security score was a constant ~100 that
passed 100% of buy-side alerts, and `RugEngine`'s `top_holder_concentration` and
`liquidity_unlocked` signals had never once had an input.

New: `collectors/onchain_security.py` (the collector), `OnChainSecuritySettings`,
a gate + Rule 9 merge in `workflow/pipeline.py`, wiring through
`controller.py` / `__main__.py`, and `deploy/onchain_facts_probe.py` (the
read-only "what would it report" measurement). 57 new tests; suite 1085 -> 1142.

**Ships OFF** (`MEMEINTEL_ONCHAIN_SECURITY_ENABLED=false`). It changes which
facts reach the analyzer and therefore which coins can earn an alert, so it gets
measured against his real database before it is switched on — the discipline that
caught the coverage cap.

### Verified live before writing the decoder, not after

Raydium AMM v4 `LiquidityStateV4` decoded from a live mainnet pool and
cross-checked so a wrong offset could not pass: 752 bytes under program
`675kPX9M…1Mp8`; `baseVault@336` and `quoteVault@368` each own a token account
whose `mint` equals `baseMint@400` / `quoteMint@432`; `lpMint@464` resolves via
`getTokenSupply` (garbage would not); `lpReserve@720` = 55465149717186 against
supply 55459414131915 -> 0.0103% burned. Re-confirmed across five v4 pools. A
survey of 25 other AMM programs found no 752-byte accounts (CPMM 637, Whirlpool
653, DLMM 904, DAMM v2 1112, CLMM 1544, PumpSwap 300) — but four programs could
not be scanned, so **length is the second check behind the program-id check**,
never the only one.

### Three live findings that changed the design

1. **The hardcoded custody address list is load-bearing.** The intended design
   was a single structural rule: exclude any holder whose owner account is
   program-owned (a PDA), since AMM vaults and bonding curves are custody rather
   than holders. That rule is necessary but NOT sufficient — Raydium's v4 vault
   authority `5Q544fKr…ge4j1` is **System-owned with zero-length data**, a
   signer-only PDA that is structurally identical to a person. On one real mint
   its vault held 94.6% of supply, so dropping the address constant would report
   a healthy graduated coin as 94.6% concentrated and block it. Both mechanisms
   are required and there is a test naming this counterexample.

2. **A real wallet can have no account at all.** Holders rent-drained to zero
   lamports return `null` from `getMultipleAccounts` while still holding tokens
   (observed at 1.30% and 0.35% of two supplies). Absence is therefore treated as
   a wallet: calling it custody would delete genuine holders and UNDER-state
   concentration, the direction that lets a rug through.

3. **`getTokenLargestAccounts` is disabled on public RPC** — HTTP 429 with
   `x-ratelimit-method-limit: 0`, deterministically, while cheap calls on the
   same connection succeed. So concentration needs a Helius key. The collector
   refuses the census up front when it has no key rather than spending the retry
   ladder (4 attempts, ~30s) per coin on a guaranteed failure inside the scan
   cycle — found by actually running the probe, not by reading the code.

### The screenshot coin does NOT reproduce, and this layer does not catch it

This is the uncomfortable part and it must not be quietly dropped. The coin that
motivated the whole project — rugcheck DANGER 65, "single holder 88.45%", "LP
100% unlocked" — was investigated live:

* The 88.45% was **the pump.fun bonding curve**, not a whale. The coin graduated
  off the curve the same day as the screenshot; the curve reads exactly 0 now. A
  curve is custody, so a *correct* census reports single digits (3.49% today) and
  the coin **passes** the gate.
* On chain the LP is **100% burned**, not unlocked; that report was generated
  pre-graduation, when no LP mint or pool existed yet.
* The coin did lose ~91% of its pool SOL — to holders dumping, not an LP pull.

So concentration collection would not have saved that trade under any defensible
exclusion policy, and nobody should claim otherwise. `test_security_thin_evidence
.py` now carries this correction in its docstring. What survives intact is the
narrower, still-valuable claim: the analyzer separates a genuinely concentrated
coin from a healthy one by 26 points once it HAS the facts, and a live coin with
a genuine **49.14%** single holder was found during the same investigation — the
check does fire on real concentration.

Two honest limits, documented in the module and in `.env.example` rather than
buried: LP burn covers **Raydium AMM v4 only**, and current pump.fun coins
graduate to **PumpSwap** (~300 bytes), so their LP status reports `None`. And a
burned LP is not liquidity safety — the screenshot coin proves a fully-burned
pool can still be drained by dumping.

### Deliberately NOT built

* **PumpSwap LP.** The pAMM pool appears to keep an equivalent `lp_supply` at
  offset 203 of its 301-byte account, arithmetically consistent on one pool
  (it exceeds minted LP by exactly 100, the min-liquidity lock). One reviewer
  explicitly flagged the *semantics* as unvalidated. An unvalidated formula is
  how a wrong number ships — this is the highest-value next step, and it needs
  the same live cross-checking the Raydium layout got.
* **A true holder count.** `getTokenLargestAccounts` sees at most 20 accounts, so
  `SecurityProfile.holder_count` is never written from here; a census size there
  would be a fabricated population figure. A complete enumeration via
  `getProgramAccounts(memcmp mint@0, no dataSize filter — Token-2022 accounts are
  170/165/191 bytes, so a dataSize:165 filter returns ~0.7% of them)` would give
  a real count AND a completeness proof (amounts must sum to `getTokenSupply`),
  but it is an unbounded call on a coin with many holders and the operator
  watches his Helius spend. Needs his sign-off on cost first.
* **`treat_zero_burn_as_unlocked`** exists as a lever, defaulting True. A pool
  whose LP is locked in a locker rather than burned reads 0% and takes a
  30-point deduction. True catches the genuinely-unlocked rug, the dominant case
  on fresh Solana pools; flip it if the probe shows healthy coins caught by it.

### Review pass on the same build (4 reviewers + verification): 8 findings fixed

A code + security review of the above found real defects, including one that
would have reproduced the project's cardinal failure. Recorded because the
reasoning matters more than the diff.

**1. CRITICAL — the exclusion rule silenced 11 of 11 live coins.** The design
rested on one structural test: exclude a holder whose owner account is owned by a
*program*. Every AMM vault authority defeats it. They are signer-only PDAs —
System-owned, zero-length data, indistinguishable from a person. A reviewer ran
the real collector against the 14 newest Solana pools from the bot's own
discovery feed: **11 of 11 resolvable coins lost every buy-side alert**, because
vault authorities for Raydium CPMM (`GpMZbSM2…`), Meteora DAMM v2 (`HLnpSz9h…`,
which owns 390k token accounts), Meteora DBC (`FhVo3mqL…`) and a pump.fun-era
infrastructure address (`BwWK17cb…`, 46,732 SOL) were each counted as a whale
holding 50-94% of supply. The same shape as the two previously-reverted
"stricter" changes.

**The fix is better than the address list it replaces:** an address that is not a
valid ed25519 curve point provably has no private key, so nobody holds it —
Solana PDAs are chosen off-curve for exactly this reason. Independently
re-verified: all five authorities above are off-curve; three known real holder
wallets are on-curve; the System Program address is on-curve (hence the address
list is kept for it). Costs no RPC call and needs no list maintained as new AMMs
appear. Exclusion now runs three tests — off-curve, known address, program-owned
— and each covers a case the others cannot see.

**2. HIGH — filling these fields trips the rug veto, not just the score.** A
merged `top_holder_percent` over 30 scores 15 rug points and `lp_locked_percent`
under 50 scores 20; both clear `ai.verify_skip_rug_score` (10), and the
scanner's deterministic risk veto then strips EVERY buy-side alert. So the
80-point security gate is almost never the deciding mechanism: a coin with a
genuine 49% holder scores 83 (passes) and goes silent anyway. **The rug engine
was not touched** — that is the operator's no-touch zone. Instead this is now
documented everywhere it matters and the probe reports it as "newly RUG-VETOED".
Whether these facts SHOULD trip a hard veto is an operator decision, and it is
the main thing to settle with him before enabling the layer.

**3. HIGH — the denominator understated concentration.** Custody was removed from
the numerator but not the denominator, so on a coin whose curve holds 85% a dev
holding 67% of the tradeable float read as 10% "of circulating supply" — which
the analyzer treats as healthy, and which puts the top-10 thresholds
arithmetically out of reach. That converts an honest "unverified" into an
affirmative all-clear, the one direction Rule 8 forbids. Reporting against the
float instead would inflate every pre-graduation coin and risk the silencing
failure, so above `max_custody_share_for_concentration` (50%) **neither figure is
published**, and below it the custody share travels with the number in `notes`.

**4. CRITICAL — cache-key contamination.** `_program_owned`'s key was
`ownerkind:{len}:{owners[0]}`: two coins sharing a top holder (routine when one
bundler is the largest holder of several of its own launches) with the same owner
count collided, and a cached classification was zipped positionally onto a
different coin's owner list — deleting a real 40% whale, or promoting a bonding
curve to a holder. This is the same fabricated-whale defect already fixed twice
in `wallet_data.get_top_holders`. Both cache keys now carry a sha256 of the exact
address list, and the TTL was cut from 600s to 120s to match the account list's.

**5. MEDIUM — a crash that would abort the scan cycle.** With `jsonParsed`, a
node that cannot parse an account returns the documented `[base64, "base64"]`
LIST for `data`; `(info.get("data") or {}).get(...)` then raises AttributeError,
which is not a `CollectorError`, so it escaped the collector, propagated through
the pipeline and killed the cycle at that coin. Reachable via a Token-2022
extension a validator's parser does not know — and most current pump.fun mints
are Token-2022. Now type-checked at every level.

**6. MEDIUM — the mandated probe measured the wrong things.** It passed
`pool_address=None` unconditionally, so the LP half was never exercised and
printed "LP unknown" for 100% of rows *by construction* — which its own docstring
pre-excused as expected, turning a structural blind spot into false reassurance.
It also assessed without a `DexPair`, so liquidity was never observed (baseline
read 100.0 where production reads 82.2) and the alert engine's liquidity
sub-gate was invisible, and it compared only against the security gate, so it
reported coins as "still pass" that lose every alert. It now resolves the live
pool, assesses with the pair, and reports all three mechanisms.

**7. MEDIUM — `force_onchain_security` was never wired.** Three docs promised
"/check and holdings bypass the cooldown and budget" while only tests set the
flag, so the facts were collected *only* in unattended scanning of sub-1h coins
and never where a human would catch a wrong number. Now wired at every site that
already forces a wallet lookup.

**8. `treat_zero_burn_as_unlocked` now defaults FALSE.** Per finding 2 the
consequence of a 0% reading is a total buy-side veto, not the "30-point
deduction" the original comment claimed in three places. And the upside is
mostly absent: 0 of the 100 newest Solana pools measured are Raydium AMM v4, so
this read rarely produces a number on his real population.

The 5th agent, an adversarial verifier meant to try to refute all of the above,
died on an API error. Findings 1 and the off-curve fix were therefore verified by
hand instead (live, against the named addresses); the rest were each accompanied
by an executed reproduction and were confirmed by reading the code. Suite 1085 ->
1155.

**Standing conclusion: do not enable this layer yet.** The measurement is the
next step, not the switch, and the veto question needs the operator's answer.

### Field failure, same day: the probe measured nothing and said so convincingly

The operator ran `deploy/onchain_facts_probe.py --limit 25` on the droplet. It
printed `!! no MEMEINTEL_HELIUS_API_KEY set`, then `facts filled in 0`,
`concentration unknown 25`, `LP status unknown 25`, and closed with "the layer is
not adding anything and enabling it would be pointless."

**All of that was wrong, and his key was configured the whole time.** The probe
called `Settings.from_env()`, which reads `os.environ` only. `load_dotenv()` is
called by `get_settings()` — and *only* by `get_settings()`. So the probe ran
unauthenticated, every census refused up front by design (the public endpoint
disables `getTokenLargestAccounts`), and the refusal was then summarised as
evidence about the data.

This is the `security_cap_impact.py` trap wearing different clothes: a
measurement tool that reports a confident conclusion about a mechanism it never
actually exercised. That one printed "0% blocked" after its setting was deleted;
this one printed "adds nothing" after failing to authenticate. Both read as
clearance — one to ship, one to abandon.

Fixed to use `get_settings()`, and `tests/test_onchain_facts_probe.py` now pins
it: the probe must not contain `Settings.from_env()`. The same file also guards
the read-only database access, the pool-address wiring, and hostile stored JSON.

Two things his run DID establish, on 25 real alerted coins: the security scores
were 82.2 / 93.3 / 100.0 with nothing below the 80 gate — the "security is a
constant that passes everything" diagnosis, confirmed on live data — and the
probe's own plumbing works end to end against the live database.

Note for anyone writing the next deploy script: `Settings.from_env()` does not
load `.env`. Use `get_settings()`, and run from the repo root, since the `.env`
path is resolved relative to the working directory.

## 2026-07-30 (later) — The 8 vetoed coins were identified on chain. The veto is correct.

The operator ran the probe with his key working: 25 coins, 13 with facts, **8 of
those 13 would lose every buy-side alert**. The probe printed STOP, because 8 of
13 is the shape of the exclusion bug that has silenced him twice. So each of the
8 was identified individually against live chain state before touching anything.

**Every one of the 8 top holders is a real keyed wallet, not custody.** The
decisive evidence was stronger than the off-curve test: each is a verified
**ed25519 signer** on its own transactions. On-curve shows a key *could* exist;
a valid signature shows one *does*, and no PDA can produce one.

Three of them share an identical rug template, which is why they clustered at
50.02 / 50.29 / 50.46%:

* Token-2022 mint, 100,000,000,000 supply, mint and freeze authority revoked.
* The deployer receives 100% of supply in the mint transaction.
* It sends exactly 12.5% to each of four freshly created sibling wallets and
  keeps exactly **50.0000%**. 50 + 4x12.5 = 100, which is why top-1 lands at ~50%
  and top-10 at ~99.9%.
* The excess above 50% is the dev buying its own coin back through PumpSwap —
  reconciled to the exact sum of three self-signed swap deltas on C0IN.
* The three deployers are three DIFFERENT addresses, so "one missed
  infrastructure account" is falsified for that cohort.

The others: 棒哥's 99.91% holder is a bundler that took 20% in the first swap off a
Meteora DBC curve and consolidated 7-8 sniper wallets into itself over 96 txs
($91k volume in a ~10-minute life). TNOS/EiaWUDEd's 46.59% holder paid 23.02 SOL
into a pump.fun curve; six keyed wallets took ~79% of supply **in a single slot**
and its bag has not moved a unit since. BBT's 40% holder is the deployer itself —
minted 100% to its own ATA, revoked authorities, created the Orca pool, then
hand-transferred 20/20/16% to three wallets within 160 seconds, leaving 96%
dev-controlled; the ticker is additionally a U+202E right-to-left-override spoof
of "The Bitcoin Bull" with a fabricated $41B FDV.

**The exclusion logic was verified working on all 11 coins examined.** Every
custody account present — PumpSwap pools, an Orca Whirlpool, pump.fun bonding
curves — was caught by BOTH the off-curve and the program-owned test
independently. Zero custody leaked into any reported figure. The three control
coins reported clean are genuinely clean: censuses enumerated 100.000000% of
supply, so their 2.31% / 5.53% / 0.18% maxima are true maxima, and two of the
three have since moved balance materially (one exited entirely, one is steadily
selling) — affirmative real-wallet evidence.

So the STOP was a false alarm from a crude ">50% of coins with facts" heuristic.
On this population the layer does what the operator asked for.

### But top10_holder_percent is degenerate, and is now not emitted

Independently measured on 8 live coins with full verified censuses:

* **6 of 8 had <= 12 real non-custody holders**; median 7 on the fresh ones.
  With fewer than ten holders the "top ten" IS every holder, so top10-of-float
  came out at exactly **100.0000% on all six** — zero variance.
* Against total supply it equalled **(100 - custody share) to four decimal
  places** on those same six. It does not measure concentration; it measures how
  far through graduation a coin is.
* It is anti-informative on a thin coin. Mint 43YaAJ2X reported top10 = 8.33%
  (distribution 100/100, rug 0, PASS) at 09:42. By 10:30 its three largest token
  accounts were CLOSED and an off-curve account held 99.938% of supply. The
  figure certified healthy distribution half an hour before a total exodus.

A value carrying no information that can still cross a threshold is a fabricated
fact (Rule 8), so the collector no longer emits it and the analyzer's top10
thresholds get no input from this layer. Note this did NOT cause the 8 vetoes —
all eight had top-1 above 30, which is sufficient on its own — but it would have
caused trouble later, and it makes the PASS verdicts less trustworthy than the
VETO ones.

### Open, and needing the operator's decision rather than a guess

1. **MARS is NOT a false positive — corrected 2026-07-30.** It was initially
   flagged as the one probable miss because its top holder
   `cornGuBy1GCTZ5vot2iRnKVX6Fr341zvnHDMBWR2goN` owns **33,718 distinct token
   accounts**, which is the classic infrastructure tell. Chased down: it is the
   sole signer and fee payer on all three transactions that built the position,
   so a private key exists, and its behaviour is identical across sampled
   transactions spanning months — spend exactly 0.0005 SOL of WSOL, receive a
   large token balance, **never sell**. It is a fixed-stake micro-sniper bot
   buying the first slice of every new bonding curve, where 0.0005 SOL buys a
   huge share of a zero-price curve; MARS is its 2,222nd position and its 0.93
   SOL balance fits exactly. MARS itself is worthless ($0.22 liquidity, $1 FDV),
   bought out entirely by bots at ~10% each. One key genuinely controls 64.38%
   of a dead coin, so the veto is correct. **That makes it 8 of 8 vetoes correct,
   not 7**, and the "holds implausibly many mints" heuristic is NOT supported by
   this case — a sniping bot with a key is a real holder that can dump. Do not
   add that heuristic on MARS's evidence.
2. **One signal alone can silence a coin.** `RugSignalWeights.
   top_holder_concentration` is 15.0 and `ai.verify_skip_rug_score` is 10.0, so
   concentration — the newest and most artifact-prone input in the system —
   unilaterally strips every buy-side alert with no corroborating second signal.
   Changing that means touching the rug engine, which is the operator's no-touch
   zone; it needs his explicit consent.
3. **The denominator.** Concentration is still a share of TOTAL supply with
   custody removed only from the numerator, guarded by
   `max_custody_share_for_concentration=50`. A reviewer measured that guard
   suppressing values too small to matter (0.006-4.2%) while leaving the harmful
   graduated-coin case untouched, and argues for float denomination under a new
   field name with a minimum-float guard, wired score-only until the cut point is
   re-fitted to his labelled outcomes. That is a real redesign and is NOT done.
4. **An unreconciled figure.** TNOS/EiaWUDEd's reported top10 of 99.00% did not
   match a fresh census (92.05% including the pool, 81.62% excluding it) while
   its top-1 matched to four significant figures. Most likely a different
   snapshot moment; unexplained, and recorded rather than guessed at.
