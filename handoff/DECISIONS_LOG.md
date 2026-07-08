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

## Deferred, with reasons

### Social data collectors (Parts 5, 19, and the "community" gate everywhere)

X/Twitter's official API starts at $200/mo for read access. This is a
real budget decision, not a technical blocker, and was explicitly left to
the user rather than silently built around. Cheaper aggregator APIs
(e.g. LunarCrush-style) exist as a middle option. **Status: unresolved,
needs a user decision.** Until then, every community/narrative-dependent
score honestly reports partial coverage or "no data" rather than being
faked — this was a deliberate design choice (Rule 8), not a bug.

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

### AI/LLM reasoning layer (Parts 13 §4, 22 §4, 23)

The qualitative judgment slots — meme strength, narrative scoring,
bull/bear case prose — are structurally wired to receive AI-layer output
(`FoundationInputs`, the narrative score slot in `ScoringEngine.evaluate()`)
but nothing currently calls an LLM. `ai/prompts.py::ANALYST_SYSTEM_PROMPT`
is written and tested (contains all Part 16 rules) and is ready to be
used as the system prompt once API-calling code is added. Needs an
Anthropic API key (not yet provided).

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
