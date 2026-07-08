# Next Steps — Unbuilt Specification Parts

> **Note on part numbering:** the project owner originally described the
> full specification as "35 parts." In practice, 32.5 numbered parts were
> actually delivered (1 through 33, plus an addendum labeled "Part 32.5"
> that the owner explicitly marked as the final delivery — "last part").
> There is no Part 34 or 35 content; the "35" was an estimate stated
> before delivery began. Treat Part 32.5 as the end of the specification.

Every file in this folder is the **original specification text, copied
verbatim**, unmodified and unsummarized, exactly as it was delivered by
the project owner. Do not paraphrase these when using them — build
directly against the literal text, the same way Parts 1–18 were built.

Note on overlap: several of these parts (20, 21, 22, 23, 30, 31, 32, 32.5)
were written as consolidation/architecture parts and substantially
overlap with functionality already built in Parts 1–18. Read them anyway
— they sometimes carry specific requirements not present elsewhere (e.g.
Part 31's "Framework Consistency Lock" is why the scoring weights are
what they are — see `../DECISIONS_LOG.md`). Where a part is pure
restatement of already-built work, note that in your build plan and move
to the genuinely new material within it.

| File | Part | Title | New-functionality estimate |
|---|---|---|---|
| PART_19_NARRATIVE_INTELLIGENCE.md | 19 | Narrative Intelligence & Viral Potential Prediction Engine | ✅ **Built** — `analyzers/narrative_analyzer.py` (see `../STATUS.md`); judgment slots await the AI layer |
| PART_20_MASTER_PROMPT_ASSEMBLY.md | 20 | Complete Master Prompt Assembly & Final Deployment Instructions | Low — consolidates Parts 1–19; scoring/report format already built |
| PART_21_TECHNICAL_INFRASTRUCTURE.md | 21 | Technical Infrastructure Blueprint & Anti-Throttling Architecture | Low — architecture already built; deployment/hosting guidance is new |
| PART_22_BOT_DEVELOPMENT_BLUEPRINT.md | 22 | Bot Development Blueprint & Software Architecture | Low — folder structure/module split already matches this |
| PART_23_AI_AGENT_INTEGRATION.md | 23 | AI Agent Integration Blueprint & Intelligence Pipeline | ✅ **Built** — `ai/reasoning.py`, live-verified (see `../STATUS.md`); §7-8 memory/feedback land with Part 24 |
| PART_24_BACKTESTING.md | 24 | Backtesting, Performance Tracking & AI Self-Improvement System | ✅ **Built** — `analytics/backtesting.py` (see `../STATUS.md`); metrics mature as the scanner accumulates outcomes |
| PART_25_RISK_MANAGEMENT_UPSIDE.md | 25 | Professional Risk Management & High-Upside Opportunity Framework | Medium — risk doctrine already incorporated; explicit opportunity-ranking formula not separately implemented |
| PART_26_MOMENTUM_DETECTION.md | 26 | Advanced Entry Signal & Momentum Detection Engine | Low — momentum analyzer already built in Part 14 against this doctrine |
| PART_27_LAUNCH_SCANNER.md | 27 | Automated Token Discovery & Early Launch Scanner | Low — discovery engine already built; first-5-minute/first-hour framing is new |
| PART_28_PORTFOLIO_MANAGEMENT.md | 28 | Portfolio Management, Tracking & Opportunity Rotation System | Medium — watchlist tiers/thesis tracking exist; explicit rotation ranking formula does not |
| PART_29_ALERT_INTELLIGENCE.md | 29 | Real-Time Alert Intelligence & Notification System | ✅ **Built** — `alerts/sinks.py`, alert history + performance (see `../STATUS.md`); add a bot token/webhook to activate delivery |
| PART_30_SYSTEM_OPTIMIZATION.md | 30 | Complete System Optimization & Final Professional Deployment Framework | Low — consolidation part |
| PART_31_IMPLEMENTATION_BLUEPRINT_AND_CONSISTENCY_LOCK.md | 31 | Implementation Blueprint for AI Coding Agents + Framework Consistency Lock | Already applied — this is the part that determined current scoring weights |
| PART_32_DATA_SOURCE_BLUEPRINT.md | 32 | Data Source & API Integration Blueprint | Low — architecture already built |
| PART_32_5_MULTI_SOURCE_DISCOVERY.md | 32.5 | Multi-Source Discovery & Anti-Throttling Architecture | Medium — Pump.fun integration specifically is not built |
| PART_33_SECURITY_RUG_DETECTION_ENGINE.md | 33 | Security & Rug Detection Intelligence Engine | Already applied — this determined current security sub-weights |

## Suggested build order

Given what's already built, the genuinely high-value next targets are:

1. ~~**Part 19** (Narrative Intelligence)~~ — ✅ built: the structural
   scoring layer fills the master score's `narrative` slot; the LLM
   judgment input wires in with Part 23
2. ~~**Part 23 + Part 22 §4** (AI Agent Integration)~~ — ✅ built and
   verified against the live API: the qualitative slots
   (`FoundationInputs`, `NarrativeInputs`, bull/bear reasoning) are now
   real AI judgments via `report --ai` / `plan --ai`
3. ~~**Part 29** (Alert Intelligence)~~ — ✅ built: Telegram/Discord
   sinks, §7 format, §10 ranking, alert history + performance; delivery
   activates when a bot token/webhook lands in `.env`
4. ~~**Part 24** (Backtesting)~~ — ✅ built: outcome windows, prediction
   grading, §4 metrics, signal analysis, weight experiments (report-only
   under the Part 31 lock); judgments mature as data accumulates
5. **Part 32.5's Pump.fun integration** — a genuinely new discovery
   source, additive to the existing `DiscoveryEngine`

Parts 20, 21, 22 (minus §4), 25, 26, 27, 28, 30, 31, 32, 33 are lower
priority since they're substantially already reflected in the codebase —
treat them as a verification pass (does the current implementation
actually satisfy this text?) rather than a from-scratch build.
