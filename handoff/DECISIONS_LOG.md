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
