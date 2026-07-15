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

## 2026-07-13 — DexScreener boost radar (Project 5) + channel isolation

Built at the operator's request ("notify me the second any token gets a boost
bigger than 100 on DexScreener"): `workflow/boost_watcher.py` polls the free,
keyless boost feed (~30s edge cache) and alerts ONCE per token per crossing,
Solana-only by default (`MEMEINTEL_BOOST_WATCHER_*`, off by default, `monitor
--boosts`). A boost is PAID promotion — the alert text says "attention, not
endorsement" and it never enters analysis or trading. **Post-ship fix:** the
`boost` alert type initially rode the `discoveries` Telegram category and its
unfiltered volume drowned out the (rare, heavily-gated) real opportunity
alerts — the operator experienced it as "the bot only sends boosted tokens."
Boosts now have their own `boosts` category, isolated from vetted picks by
construction, with a regression test pinning the separation.

## 2026-07-14 — Boost radar removed (operator decision, one day after shipping)

The DexScreener boost radar (built 2026-07-13) flooded the operator's phone
with alerts on day-old, near-dead coins. Root cause is inherent, not a bug:
boosts are PAID promotion with zero quality screening, and the most common
buyer of a boost is a dying coin trying to attract exit liquidity — so a
radar for boost crossings is structurally a radar for exactly the coins the
operator never wants to see. The channel-category isolation (2026-07-13 fix)
didn't help in practice because the operator runs a single default chat (no
`MEMEINTEL_ALERT_DELIVERY_TELEGRAM_ROUTES`). Operator verdict: "remove it
completely." Removed: `workflow/boost_watcher.py`, `BoostWatcherSettings`,
`monitor --boosts`, the `boosts` alert channel, `DexScreenerClient.
get_boosts()`, and their tests. KEPT: the on-demand `/boost <address>`
Telegram command (separate earlier request; pull-based, cannot flood) and
its `get_token_boost()` lookup. Stray `MEMEINTEL_BOOST_WATCHER_*` lines in
the droplet `.env` are ignored harmlessly (verified). Lesson recorded: an
unscreened high-frequency signal must not share the operator's single alert
surface with vetted picks, no matter how it is categorized internally.

## 2026-07-14 — Smart-wallet tracking starts as a FREE holder clock, not a paid trade stream

**Decision (operator-approved):** begin the smart-wallet roadmap by recording
the top-holder wallets of every analyzed token from data the bot already
fetches, NOT by subscribing to live per-trade streams.

**Why:** the scoped plan was to ride PumpPortal's WebSocket for per-trade
buyer wallets "for free." Verifying against PumpPortal's docs before building
(Rule 8) showed that is wrong: `subscribeTokenTrade`/`subscribeAccountTrade`
are METERED — 0.01 SOL per 10k events against an API key + linked wallet
funded ≥0.02 SOL; only `subscribeNewToken`/`subscribeMigration` are free.
Realistic cost for a hot watchlist: tens to hundreds of $/month, violating
the operator's free-only constraint. Presented the options; the operator
chose the free pivot.

**What was built (Part 17 groundwork — the data clock only):**

- GoPlus responses always contained the top-holder wallet ADDRESSES; the
  parser kept only percentages. `_top_holders()` now retains
  (address, percent) as `SecurityProfile.top_holders` — same burn/locked
  exclusions as the concentration math, zero new API calls.
- `wallet_sightings` gained `source`/`percent` columns (fresh schema +
  `_MIGRATIONS` for the droplet's existing DB — verified against a
  simulated old database). `record_wallet_sightings()` keeps accepting the
  legacy 3-tuple shape (Rule 18) and is documented as append-only/no-dedup:
  callers dedup upstream, reputation queries aggregate with DISTINCT.
- `workflow/smart_wallets.py` `SmartWalletRecorder`: passive, off by
  default (`MEMEINTEL_SMART_WALLET_ENABLED` / `monitor --smart-wallets`),
  records each token's FIRST holder snapshot once (bounded dedup; empty
  holder lists and failed writes leave the token unmarked so a later
  recheck retries), never raises into the scan (Rule 7). Wired like the
  other optional services: constructed in `__main__`, passed into
  `ContinuousScanner`, called next to `record_security_facts`.

**Deliberately NOT built yet (Rule 2):** reputation scoring over outcomes,
the live smart-money alert, and any seeding from public smart-wallet lists —
those come after 2–4 weeks of accumulated sightings + outcome labels. The
paid PumpPortal `subscribeAccountTrade` stream becomes cheap (~$5/mo for ~50
wallets) and worth revisiting only AFTER a reputation list exists.

## 2026-07-14 — Stale-coin re-pitch, round two: peak-decline suppression + honest re-alert framing

The operator reported the decline-suppression fix (2026-07-12 era, commit
d3e88ec) didn't fully stop day-old coins re-arriving as fresh finds. A
read-only investigation found three cooperating mechanisms, each fixed
additively:

1. **The one-step decline check had an escape hatch.** `_score_declining`
   compared only against the immediately-preceding snapshot; a collapsed
   coin creeping back +2-3 points per recheck read as "improving" on every
   single look and re-pitched for days while far below its own peak. Fix:
   `Storage.peak_score()` (all-time-high final score) feeds a new
   `_below_peak` condition — weak-tier buy-side alerts stay suppressed
   until the score returns to within
   `MEMEINTEL_ALERT_ENGINE_PEAK_DECLINE_SUPPRESSION_POINTS` (default 15) of
   the peak. Strong tiers stay exempt (same contract as the decline check);
   `_score_drop_rule`'s copy is unaffected (it is genuinely about the last
   look, so it keeps the one-step comparison).
2. **Flatness scored as momentum.** `_trend_consistency` gave the full
   "consistent trend" 90 to any all-non-negative triple — a stale coin
   drifting sideways scored like a climber. Fix: all three windows inside
   `MEMEINTEL_MOMENTUM_FLAT_TREND_BAND_PERCENT` (default 2%) now score a
   low 40 ("no trend evidence"). Same idea in wallet intelligence: a flat
   price range only reads as "buying during consolidation" with ≥5 priced
   buys behind it — a dead-quiet coin is flat too (Rule 8).
3. **Re-alerts read like discoveries.** Nothing in the alert copy revealed
   that the bot had pitched the same coin before. Every alert on a token
   with prior alert history now carries a "Seen before: N prior alert(s) —
   first alerted 2d 4h ago" line (Telegram/Discord and console renderers);
   annotation is best-effort and never blocks delivery (Rule 7).

## 2026-07-14 — Wallet reputation connector: data clock × measured outcomes

Operator-requested follow-on to the morning's data clock: the join that
turns recorded holder sightings into wallet reputation scores. Built as
`analytics/wallet_reputation.py` — read-and-compute only, no new tables.

- **Reuses, never redefines (Rule 17/18):** token win/loss labels use the
  EXACT `BacktestSettings` thresholds `evaluate_predictions` already
  grades with (+50% best window = win; liquidity death or -50% worst
  window = loss; neither = undetermined, counted toward nothing). Scores
  come from the pre-existing Part 17 `wallet_reputation()` formula that
  had been waiting for data since it was written.
- **Honest gaps (Rule 8):** holder snapshots carry no entry timing and no
  USD sizes, so `early_entry_rate`/`median_position_usd` stay None and a
  scored wallet's coverage tops out at 0.60 (win rate + rug avoidance +
  consistency). A wallet below `MEMEINTEL_SMART_WALLET_MIN_RESOLVED_FOR_
  REPUTATION` (default 3) resolved tokens gets NO score — one lucky pick
  is not a track record. Reputation is computed per sighting source
  (Rule 9); manual CLI sightings never blend into the clock's join.
- **Surfaces:** `python -m meme_intelligence reputation` (local-only
  read) and a reputation section on the `/wallets` Telegram command (top
  3, honest denominators, degrades to one line on failure). Wallet
  strings from GoPlus are sanitized before echoing into Telegram — the
  same injection lesson as token names (short strings bypass truncation).
- **Deliberately still NOT built:** persisting scores, feeding
  `reputations` into the live scan, and the smart-money alert — those
  wait until real data has been watched for a few weeks. The multi-agent
  review fleet couldn't run (account spend limit); an inline adversarial
  review covered the same lenses and produced the sanitization fix plus
  three regression tests (NULL-price deaths, /wallets failure path,
  injection guard).

**Second-opinion pass (same day, operator-requested, fleet capped at 10
agents):** 17 findings raised, 6 verified-confirmed, all fixed:

1. **No hindsight credit (the big one).** The join credited wallets
   sighted AFTER a token's outcome was already measured — restart
   re-records and late-recheck holder snapshots capture post-pump
   chasers, and a reproduced chaser wallet scored 86/100 on wins measured
   two weeks before it was ever seen. Sightings whose first record
   postdates the token's first measured outcome are now a separate
   `hindsight` bucket: no credit, surfaced honestly in the report
   (unparseable timestamps also land there — no proof of early means no
   credit, Rule 8).
2. The whole join now aggregates IN SQLite (`wallet_reputation_rollup`/
   `_totals` + a covering index): the Python-side join materialized
   ~330MB and stalled the shared event loop (scan + trade buttons) for
   3+ seconds at realistic table sizes on the 1GB droplet. Memory is now
   O(scored wallets). The index is created AFTER `_migrate()` — putting
   it in the schema script crashed startup on pre-migration databases
   ("no such column: source"); the old-database migration test caught
   that before it reached the droplet.
3. CLI `reputation --min-resolved 0` crashed with a raw
   ZeroDivisionError (bypassed settings validation); both the CLI and
   `compute_wallet_reputations` now reject it loudly. Nonsense `--top`
   values clamped (previously sliced from the wrong end and fabricated
   the "… and N more" count).
4. Test hardening from surviving-mutant analysis: price-only losses
   (worst ≤ -50% without death), exact-threshold boundaries, sort
   direction, and CLI-render sanitization are now all pinned.
   `goplus_holders` now has one canonical definition
   (`DEFAULT_SIGHTING_SOURCE`) imported by writer and readers.

## 2026-07-14 — Buy-side ceilings ON by default ($100k mcap / $50k liquidity) + 24h freshness gate

Operator complaint (verbatim intent): "it sends me coins with around 100
million to 1 billion market cap for some reason. Make the market cap below
100k and liquidity of your choice. Also before it sends me anything on the
telegram I want it to make sure it's not older than 1 day."

Three changes, all in the existing suppression chain (Rule 18 — extend,
never rewrite):

1. **Ceiling defaults flipped ON.** `opportunity_max_market_cap_usd` now
   defaults to **100,000** (his number) and `opportunity_max_liquidity_usd`
   to **50,000** (our choice: a sub-$100k-mcap coin with a pool deeper than
   $50k has already had its move, and $50k is the existing
   `SecurityThresholds.healthy_liquidity_usd` anchor — past "healthy depth"
   is past "early"). The mechanism itself already existed (built 2026-07-12,
   `d8fffe9`) but shipped 0 = OFF per Rule 18; the operator has now
   explicitly asked for it to be on, so a plain `git pull` + restart is
   enough — no `.env` edit on the droplet. Setting either to 0 still turns
   that ceiling off (the escape hatch is tested). NOTE for droplet ops: an
   explicit `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_*=0` line in `.env` would
   override the new defaults back OFF — none is known to exist, but check
   if the operator still reports oversized coins after deploying.
2. **New freshness gate** `opportunity_max_age_hours` (default **24**,
   `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS`, 0 = OFF): a buy-side alert
   (both HIGH tiers + early_opportunity, momentum, smart_money_accumulation)
   on a pool older than the window is suppressed entirely — a day-old coin
   is never a fresh find, whatever its numbers do. Discovery already
   rejected old pools at intake; this closes the WATCHLIST-RECHECK path
   that kept re-pitching day-old coins (the same hole the peak-decline
   suppression narrowed — this closes it by age, unconditionally).
   Protective alerts (emergency/risk/death/whale-exit/etc.) are never
   age-gated — an old coin the operator holds still gets its warnings.
3. **"Pool age" safety-checklist line** on every surviving buy-side alert:
   ✅ with the age when verified ("Pool age 3h"), ❔ "Pool age: not
   verified" when the source never reported a creation time. Rule 8
   decision: an UNKNOWN creation time does NOT trip the gate (absent data
   is not evidence of age, matching the ceiling's unknown-never-trips
   contract) — but the gap is surfaced on the alert instead of hidden.
   A future/invalid timestamp counts as unverified, never as age.

Test fixtures updated to match the new reality: the canonical strong-coin
fixture in `test_automation.py`/`test_controller.py` is now $80k mcap /
$45k liquidity (under both ceilings — the exact profile the operator wants
pitched); direct `AutomationRules(...)` constructions in tests now inject
the frozen test clock (the age gate is the first alert rule that reads
wall-time). Suite: **877 passing** with all optional deps installed
(8 new age-gate tests).

Adversarial verification: a 3-agent review fleet (correctness /
operator-intent / edge-cases lenses, capped per the operator's standing
3–5 agent limit) ran over the diff before commit. Confirmed findings, all
fixed in the same session:

1. **Deploy-safety (the big one):** flipping the ceiling defaults ON meant
   a pre-existing `.env` floor line above $50k/$100k would have tripped the
   floor<=ceiling startup check and CRASH-LOOPED the droplet on a plain
   `git pull` deploy — killing the protective rug alerts too. The check no
   longer raises: a conflicting FLOOR is disabled with a loud logged
   warning (the ceiling is the operator's newer directive, and the floor's
   original 0-liquidity job is covered by the untradeable hard block).
   Related: a liquidity ceiling set below `strong_candidate_min_liquidity_
   usd` (25k) makes HIGH tiers structurally unreachable — now a loud
   startup warning (never a crash).
2. **Checklist honesty:** the "Pool age" line's warn branch was unreachable
   (the gate suppresses over-age alerts before any checklist renders), so
   with the gate turned OFF a 30h-old pool rendered a green "✅ Pool age
   30h". Gate-off now renders an ℹ️ note ("Pool age 30h — freshness gate
   off"), never a pass — a ✅ must not endorse staleness.
3. **Rule 13:** buy-side suppression (veto/untradeable/oversized/too-old)
   was silent; `AutomationRules.evaluate` now logs each drop with the
   token, the dropped alert types, and the specific gate + values — so a
   stray `.env` override or mis-set ceiling is diagnosable from logs.
4. **Test integrity:** `test_no_momentum_alert_in_late_zone` had become
   vacuous (its 3-day-old fixture was age-suppressed before the LATE-zone
   logic ran) — fixture moved inside the window; the 24h age default is
   now asserted explicitly alongside the ceiling defaults.
5. Stale `_oversized` docstring ("default 0.0 = OFF") corrected.

**Deploy note:** `load_dotenv` gives the FIRST occurrence of a key in
`.env` precedence, so stale `MEMEINTEL_ALERTS_OPPORTUNITY_MAX_*` lines
would silently override the new defaults — the update block sent to the
operator (and now recorded in the owner's manual Part 1 deploy loop and
OPERATIONS.md step 3b) deletes any such lines before restarting. Suite
after fixes: **878 passing**.

## 2026-07-14 (late evening) — Max-effort review of the ceiling/freshness change: all 15 findings fixed, staleness door built

Operator ran `/code-review` + `/security-review` at max effort over the
ceiling/freshness commit (a 25-agent-capped fleet: 10 finder angles, 1-vote
adversarial verification, security pass), then said "fix all at once."
Security review: **no vulnerabilities** (new checklist line renders only
static text + a formatted number; suppression logic cannot be abused to
elevate alerts or reach trading; log lines use lazy %-formatting with no
secrets). Code review: 29 verified candidates merged to 15 findings — every
one fixed in this batch:

1. **Config warnings now reach the log file.** They fire during
   `get_settings()`, before `setup_logging()` — previously lastResort
   stderr only. Warnings are kept on `AlertThresholds.config_notes`
   (non-field attribute) and `__main__._run` re-emits them once handlers
   exist.
2. **Deploy docs contradiction fixed.** The manual's Part 1 deploy loop and
   README now point at `claude/bot-owners-manual-0a16e5` and include the
   `sed -i '/MEMEINTEL_ALERTS_OPPORTUNITY_MAX/d' .env` stale-line cleanup
   (`.env` FIRST-occurrence precedence); OPERATIONS.md gained deploy-checklist
   step 3b (changed defaults vs stale .env lines) and its "fails loudly"
   troubleshooting row now notes the floor/ceiling self-heal exception.
3. **Coin age, not just pool age.** `_too_old` now takes the LARGER of pool
   age and how long the bot has tracked the token (`tokens.first_seen`, new
   `Storage.token_first_seen`, threaded from `_process_result`, read before
   the run's snapshot so a first look stays None) — a week-old coin migrating
   to a fresh pool no longer reads as a fresh find (Rule 8: tracked-for-3-days
   is EVIDENCE of age, a known lower bound).
4. **Suppression logging deduped + suppression work skipped when no buy-side
   event fired.** The scanner's provisional evaluate passes `quiet=True`;
   the classification chain (and its clock reads) only runs when a buy-side
   alert actually exists.
5. **Equality boundaries closed** (`<` → `<=`): floor == ceiling now counts
   as a conflict (floor yields, warned); ceiling == strong-candidate depth
   floor now warns (only exactly-$X coins could ever reach HIGH).
6. **No None-format crash / no double clock read**: `_oversized`/`_too_old`
   now RETURN their reason strings (computed once from the same values that
   tripped), instead of evaluate() re-deriving them — this also fixed the
   misleading "vs max 0" log when one ceiling is off.
7. **`_format_age` floors to displayed precision** — never "60m", never a
   green "Pool age 24h" on a pool that passed the gate by two minutes;
   h→d switch consistent at 48h. Pinned by tests.
8. **Reuse:** `_is_new_launch` now calls the shared `_hours_since`/
   `_pool_age_hours` instead of re-implementing the subtraction; the wider
   consolidation (seven age computations, three duration formatters across
   modules) is DEFERRED — cross-module churn for cleanup is a Rule 3 risk
   worth its own pass.
9. **THE WATCHLIST STALENESS DOOR IS BUILT** (handoff Part 14, the approved
   design, unchanged): `workflow.watchlist_max_age_days` (default 3,
   `MEMEINTEL_WORKFLOW_WATCHLIST_MAX_AGE_DAYS`, 0 = off). Implemented once in
   `watchlist_review.stale_watchlist_reason()`, used by BOTH the scanner
   recheck and `review_entries` (daily routine / `watchlist --refresh`).
   Stale coins are archived BEFORE any provider call is spent (the freshness
   gate already guarantees their buy-side alerts could never send — rechecking
   them was pure API burn). Holdings exempt; archive-not-delete.
10. **Known, accepted:** with the 24h gate ON, the fully-verified
    `high_priority_opportunity` tier is effectively retired (community data
    takes days to appear; by then every coin is past the gate) —
    `strong_candidate` is the operational HIGH tier. Consistent with the
    operator's fresh-coins-only directive; revisit only if a fast community
    source ever lands. Also accepted: the self-heal lives in `__post_init__`
    where env-vs-default provenance is unknowable — an explicit floor loses
    to the default ceiling; fine until thresholds become runtime-editable.

Tests and docs updated throughout (stale comments, missing caplog assert on
the self-heal warning, the manual's step-4 checklist enumeration + Part 2.12
/ Part 13 / Part 14 staleness-door status). Suite after this and the follow-up round: **886 passing**.

## 2026-07-14 (night) — Re-review round: 2 staleness-door bugs found and fixed

The fix batch itself was re-reviewed (4-agent find-and-verify fleet:
evaluate-refactor, staleness-door, regressions, security lenses). Three
lenses returned clean, including security. The staleness-door lens confirmed
two bugs in the NEW code, both fixed with regression tests:

1. **Resurrection loop (CONFIRMED).** `update_watchlist`'s UPSERT never
   reset `added_at`, so a coin re-discovered after a staleness archive kept
   its day-0 timestamp and was instantly re-archived on the next pass —
   "a truly revived coin re-enters via fresh discovery" was impossible. The
   UPSERT now restarts the clock ONLY on the archived→live transition; a
   live entry's added_at is untouched.
2. **EVM case-variant miss (PLAUSIBLE, EVM-only).** `token_first_seen` used
   exact chain+address matching, unlike `find_token`/`is_holding` which
   fall back to case-insensitive matching for 0x addresses — a checksummed
   re-analysis of a lowercased-recorded token silently lost its tracked age
   and the freshness gate fell back to pool age alone. Same 0x fallback
   added (Solana base58 stays exact — case-sensitive by design). A third
   verification round then caught that exact-match-FIRST self-shadows: the
   case-variant's own snapshot upsert creates a second tokens row whose
   fresh first_seen wins every later exact lookup, collapsing a 30-day
   tracked age to hours. Final form: for 0x addresses the lookup is always
   MIN(first_seen) across case variants (chronological — timestamps are
   aware-UTC isoformat text); pinned by a duplicate-row regression test.
   The fourth round then surfaced the two remaining consequences of the
   same root cause (duplicate tokens rows per EVM case variant): peak_score
   and score_history still matched exactly (peak-decline suppression read
   None for a checksummed re-sighting), and the lower(address) scan had no
   index (measured ~7000x slowdown at 200k rows on the event-loop thread).
   Final form: one shared `Storage._address_match_sql()` fragment used by
   token_first_seen / peak_score / score_history (0x = case-insensitive
   across variants, Solana exact by design) backed by a new
   `idx_tokens_chain_lower_addr` expression index in the schema (original
   columns — safe outside the post-migration list). A fifth round then
   swept every remaining Storage lookup feeding the alert path and found
   the last two consumers of the same root cause, both CONFIRMED with
   serious (EVM-only, currently dormant) consequences and both fixed the
   same way: `alert_history` (feeds the interest gate — a casing flip hid
   the HIGH alert that granted interest, demoting every later protective
   alert to LOW, below the phone's delivery floor) and
   `latest_security_facts` (a honeypot flip straddling a casing flip
   diffed against None — no security-change alert, and the new baseline
   buried the change permanently; newest-baseline-wins across variants,
   token_id tiebreak for determinism). All EVM-only paths; the production
   Solana-only config was never affected. A sixth verification round on
   the final state returned zero findings.

Suite: **888 passing**.

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
