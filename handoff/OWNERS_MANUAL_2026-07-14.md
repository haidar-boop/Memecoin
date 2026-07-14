# MEMECOIN BOT — COMPLETE OWNER'S MANUAL
**Refreshed 2026-07-14 · covers every package, setting, command, and decision through commit `9d25f8f`**

---

# Part 1 — What This Is and How to Use It

This is the full handoff document for the Meme Coin Intelligence System: a
Solana-first meme-coin scanner, scorer, and alerting desk that runs 24/7 on
the operator's $6/mo DigitalOcean droplet (1GB RAM, systemd service
`meme-intelligence`, repo at `~/meme-intelligence`) and talks to the operator
through Telegram. **It never trades on its own — every real trade is a
button the operator taps.** Wallet keys live only in the droplet `.env`,
never in git or chat.

**The branch that matters: `claude/ceiling-and-boost`.** The droplet pulls
it. It supersedes `claude/memecoin-onboarding-yrvjbg`.

**Deploy loop (the only one you need):**
```bash
cd ~/meme-intelligence
git pull origin claude/ceiling-and-boost
sudo systemctl restart meme-intelligence
```

**Read order for a new session:** this manual → `handoff/PROJECT_RULES.md`
(the 21 mandatory engineering rules) → `handoff/OPERATOR.md` (who runs this
and his hard constraints) → `handoff/DECISIONS_LOG.md` (every "why", dated).
The 21 Rules are never optional; Rule 8 (never fabricate data — unknowns stay
None and reduce confidence) and Rule 3 (never break working code) explain
most of the design choices you'll see below.

**The honest one-line assessment of the whole system** (agreed with the
operator, backed by measured metrics): the rug-detection side is excellent
and battle-tested (precision 0.97 over 11k+ graded calls — the seatbelt);
the pick-winners side is a thin, real-but-small edge (hit rate 0.54, n=555 —
the crystal ball). The bot's job is to filter out the thousands of coins
that die and hand the operator a slightly-better-than-even shot on the
survivors, with every trade decision remaining his.

---


# Part 2 — Everything That Happened: 2026-07-12 → 2026-07-14

A complete, honest changelog of the work done in this period, in order, with
the reasoning. Every item is committed on `claude/ceiling-and-boost` (the
branch the droplet now runs — it superseded `claude/memecoin-onboarding-yrvjbg`).

## 2.1 Buy-side size ceiling (`d8fffe9`)
**Complaint:** "it sends coins already at millions of market cap after 5 min."
**Fix:** configurable liquidity/market-cap CEILING for buy-side alerts
(`MEMEINTEL_ALERTS_OPPORTUNITY_MAX_LIQUIDITY_USD` / `_MAX_MARKET_CAP_USD`,
shipped 0 = off; since 2026-07-14 the defaults are ON at $50k liquidity /
$100k market cap, and a 24h `_MAX_AGE_HOURS` freshness gate was added — see
DECISIONS_LOG 2026-07-14). Above the ceiling the move already happened, so the
opportunity alert is suppressed; protective alerts still fire. Complements the
earlier floor (untradeable 0-liquidity coins).

## 2.2 /boost Telegram command (`ff91fd8`)
Operator asked: "just tell me the boost and how much of it." On-demand lookup
of a coin's DexScreener paid-boost total. Pull-based — answers only when
asked. **This command still exists** (it survived the radar removal below).

## 2.3 Decline re-pitch suppression, round one (`d3e88ec`)
**Complaint:** day-old declining coins re-pitched as fresh opportunities.
**Fix:** when a coin's score dropped ≥15 points since the last look, the weak
buy-side tiers (`early_opportunity`, `momentum`, `smart_money_accumulation`)
are suppressed. The strong tiers (`strong_candidate`,
`high_priority_opportunity`) stay exempt on purpose: a coin that clears the
strict bar despite declining is a contradiction the operator should see.

## 2.4 DexScreener boost radar — built (`1ea4660`), channel-isolated (`19d1a20`), REMOVED (`9d25f8f`)
Built at operator request: alert the second any Solana token crosses 100
boosts. Within a day it flooded the phone with paid-promo pings on day-old,
dying coins. First mitigation: boost alerts got their own channel category —
didn't help, because the operator runs a single default chat (no custom
routes). Root cause was inherent: **boosts are paid promotion with zero
quality screening, and the most common boost buyer is a dying coin trying to
attract exit liquidity.** Operator verdict: "remove it completely." All radar
code, settings, tests, and the `--boosts` flag are gone. Stray
`MEMEINTEL_BOOST_WATCHER_*` lines in the droplet `.env` are ignored
harmlessly. **Lesson (recorded in DECISIONS_LOG): an unscreened
high-frequency signal must never share the operator's single alert surface
with vetted picks.**

## 2.5 Stale-coin re-pitch, round two (`bf804a4`)
**Complaint:** still receiving day-old coins pitched as fresh. Read-only
investigation found three cooperating mechanisms; all fixed additively:
1. **Peak-decline suppression.** Round one compared only against the
   immediately-preceding snapshot; a collapsed coin creeping back +2-3 points
   per recheck read as "improving" forever. New: `Storage.peak_score()`
   (all-time-high) feeds `_below_peak` — weak tiers stay suppressed until the
   score returns to within `MEMEINTEL_ALERT_ENGINE_PEAK_DECLINE_SUPPRESSION_
   POINTS` (default 15) of the coin's own peak.
2. **Flat drift no longer scores as momentum.** All three price windows
   inside `MEMEINTEL_MOMENTUM_FLAT_TREND_BAND_PERCENT` (default 2%) now score
   a low 40 instead of the full "consistent trend" 90; similarly a flat price
   range only reads as wallet-intel "accumulation" with ≥5 priced buys behind
   it. A stale coin drifting sideways no longer looks like a climber.
3. **Honest re-alert framing.** Every alert on a coin with prior alert
   history carries "Seen before: N prior alert(s) — first alerted 2d 4h ago"
   (phone + console). A legitimate re-alert no longer masquerades as a
   discovery.

## 2.6 Smart-wallet data clock (`9495ca7`)
The first step of the smart-wallet roadmap. **Critical honest note:** the
original plan (PumpPortal per-trade streams) turned out NOT to be free —
their docs meter `subscribeTokenTrade` at 0.01 SOL per 10k events with a
funded linked wallet (realistically $50-500/mo). The operator chose the free
pivot: keep the top-holder wallet addresses GoPlus already returns on every
analyzed token (the parser used to throw them away). Off by default
(`MEMEINTEL_SMART_WALLET_ENABLED`); records each token's FIRST holder
snapshot once into `wallet_sightings` (source `goplus_holders`). Passive:
never alerts, never scores, never touches trading; failures never disturb
the scan.

## 2.7 /wallets Telegram command (`c5e5b35`)
Phone-visible progress readout for the data clock: sightings / distinct
wallets / tokens covered, how long it's been running, how recently it last
recorded, plus (after 2.8) the reputation section.

## 2.8 Wallet-reputation connector (`831a61a`), hardened (`a142952`)
Joins the data clock's sightings against Part 24's measured outcomes and
scores wallets with the pre-existing Part 17 `wallet_reputation()` formula.
Key properties:
- Win/loss labels reuse the EXACT backtest thresholds (+50% best window =
  win; liquidity death or -50% worst window = loss; neither = undetermined,
  counts toward nothing).
- A wallet needs ≥`MEMEINTEL_SMART_WALLET_MIN_RESOLVED_FOR_REPUTATION`
  (default 3) resolved tokens for ANY score. Unmeasurable dimensions (entry
  timing, USD size) stay None — coverage tops out at 0.60.
- **No hindsight credit** (found by a capped 10-agent adversarial review):
  a wallet first sighted AFTER a coin's outcome was measured is a post-pump
  chaser and earns nothing; such pairs are excluded and reported separately.
- The whole join runs inside SQLite behind a covering index (a Python-side
  join measured ~330MB / 3s+ at realistic table size — would have stalled
  the loop that runs scanning AND the trade buttons).
- Surfaces: `python -m meme_intelligence reputation` CLI + the /wallets
  reputation section. Scores stay honestly empty until the clock and the
  6-hourly `backtest --refresh` cron have overlapping data (weeks).
**Still deliberately NOT built:** persisting scores, feeding reputations
into the live scan, the "2+ smart wallets just bought this" alert.

## 2.9 Reviews and their findings (how quality was enforced)
Three adversarial review passes ran this period (multi-agent where the spend
limit allowed, inline otherwise). Confirmed-and-fixed findings included: NaN
"percent" values from GoPlus poisoning the top-holder sort; the controller
wiring and old-database migration paths having zero test coverage; Telegram
sanitization of GoPlus-originated wallet strings; the hindsight-credit flaw;
a CLI ZeroDivisionError; and an index that would have crashed startup on the
droplet's pre-migration database (caught by the migration test before
deploy). Findings that verifiers REFUTED were deliberately not "fixed" —
review noise is rejected, not appeased.

## 2.10 Git snapshot tag
Local tag `snapshot-2026-07-13` marks commit `19d1a20` (boost radar +
channel fix, before smart-wallet work). A full repo archive was also
delivered to the operator as a file.

## 2.11 Mind-layer report card (operator-run, 2026-07-13)
`memory 8497 coins | resolved 13308 | graded 12808 | hit rate 0.54 (n=555) |
rug precision/recall 0.97/0.95 | brier 0.14 | p(rug) veto ON, authority
EARNED`. Honest interpretation, agreed with the operator: the rug veto is
excellent and battle-tested (the seatbelt); the directional edge is thin and
small-sample (the crystal ball). The bot's realistic job: filter out the
thousands that die, hand over a slightly-better-than-even shot on survivors.

## 2.12 APPROVED NEXT STEP (not yet built): the watchlist staleness door
Operator's own theory, confirmed in code: the watchlist has only three exit
conditions (dead liquidity <$500, score falls to Avoid, pairs vanish) — a
mediocre "undead" coin lingers forever, stays in the recheck rotation, and
re-alerts whenever its numbers wobble. Agreed design: archive ANY coin after
N days on the watchlist (default 3, configurable), operator holdings exempt,
archive-not-delete (history preserved; re-discoverable if it truly revives),
ON by default. Build this next.


---


# Part 3 — Core Models, Enums, Discovery, and the AI Layer

### Core: models (`meme_intelligence/core/models.py`)

All dataclasses are frozen; `None` always means "source did not report this" — never zero (Rule 8).

- `TokenIdentity` — chain + address (+ optional name/symbol); the universal token key throughout the system.
- `DexPair` — normalized DEX pair snapshot. Every metric optional: `price_usd`, `liquidity_usd`, `fdv`, `market_cap`, `volume_24h/6h/1h`, `price_change_24h/6h/1h`, `buys/sells_24h/1h`, `buyers_24h`/`sellers_24h` (GeckoTerminal only), `pair_created_at`, `dex_id`, `url`.
- `SecurityProfile` — normalized contract facts (GoPlus today plus live Jupiter probe). Honeypot/tradability (`is_honeypot`, `cannot_buy`, `cannot_sell_all`), permissions (`is_mintable`, `ownership_renounced`, `hidden_owner`, `has_blacklist`, `is_freezable`, `balance_mutable`...), taxes (`buy/sell_tax_percent`, `tax_modifiable`), manipulation flags, distribution (`holder_count`, `top_holder_percent`, `top10_holder_percent`), `creator_address`, `lp_locked_percent`, and live-probe fields `live_buy_route_found` / `live_sell_route_found` / `live_round_trip_loss_percent`.
- `TopHolder` (`address`, `percent` 0–100) — `SecurityProfile.top_holders: tuple[TopHolder, ...]`. Burn/locked addresses excluded at parse time; program accounts (pools, bonding curves) are deliberately NOT — filtering them is the reputation step's job.
- `PumpFunLaunch` / `PumpFunCoinState` — launchpad discovery record and traction snapshot; SOL-denominated because launch events carry no USD conversion.
- `CommunityProfile`, `OnChainProfile`, `WalletIntelData` (with `WalletHolding`, `TokenTrade`, `TokenTransfer`), `LiquidityProbeResult` — same nullable-facts pattern. In `LiquidityProbeResult`, sell-side fields stay `None` when no buy route was found (not-applicable, not unknown).
- `CategoryScores` (7 fields matching `ScoringWeights` 1:1; validates 0–100) → `compute_weighted_score()` renormalizes weights over scored categories and returns `WeightedScoreResult(total, coverage, missing)`; raises `InsufficientDataError` when nothing scored. `classify()` is pure banding (defaults 90/80/70/60); red-flag AVOID overrides happen upstream in the scoring engine.

### Core: enums, errors, cache, rate limiter, logging

- Enums (`core/enums.py`, all `str`-valued): `Classification` (ELITE_OPPORTUNITY/STRONG_CANDIDATE/WATCHLIST/SPECULATIVE/AVOID), `RiskTier` (ACCEPTABLE_UNCERTAINTY/SERIOUS_WARNING/DESTRUCTIVE — permanent taxonomy; DESTRUCTIVE invalidates), `AlertPriority` (CRITICAL/HIGH/MEDIUM/LOW), `WatchlistTier` (TIER_1_HIGH_PRIORITY/TIER_2_DEVELOPING/TIER_3_RESEARCH_ONLY/ARCHIVED), `EntryZone` (EARLY/CONFIRMATION/LATE/UNCLEAR), `ResearchMode` (FAST_SCAN/STANDARD/DEEP_INVESTIGATION), plus `ConfidenceLevel`, `MarketPhase`, `CommunityRating` (ARTIFICIAL = red-flag), `MarketCapStage`, `ValuationClassification`, `SetupType`, `ConvictionLevel`, `MarketRegime`, `CheckStatus` (UNKNOWN ≠ pass), `RiskCategory`, `RiskPosture`, `PreferredAction`, `WhaleType`, `AccumulationVerdict`, `ScanLayer` (DISCOVERY/SECURITY/INTELLIGENCE/ALERT), and the Narrative* / Sentiment / Catalyst family.
- Errors (`core/errors.py`): `MemeIntelError` base; `ConfigurationError`; `CollectorError` (non-retryable, carries `status_code`) ⊃ `TransientCollectorError` (retryable) ⊃ `RateLimitedError` (carries `retry_after_seconds`; retry sleeps at least that long); `AllProvidersFailedError` (carries per-provider `causes`); `InsufficientDataError` (raised instead of fabricating).
- `TTLCache` (`core/cache.py`) — async-safe in-memory TTL+LRU cache; defaults `max_entries=2048`, `default_ttl=30.0`s; `get`/`set`/`get_or_set`; injectable `time_func` for tests.
- `RateLimiter` (`core/rate_limiter.py`) — token bucket; `RateLimiter.per_minute(rpm, burst=5)` convenience; every collector must `await acquire()` before each request.
- `setup_logging(level="INFO", log_dir="logs")` — console + `logs/meme_intelligence.log` RotatingFileHandler (5 MB, 3 backups); safe to call repeatedly; `get_logger(name)` returns children of `meme_intelligence.*`. Also in core: `provider_pool.py` (failover pool behind `AllProvidersFailedError`) and `retry.py`.

### AlertEvent (`alerts/notification_engine.py`)

- Frozen dataclass: `priority` (AlertPriority), `alert_type` (str), `token`, `title`, `reasons: tuple[str,...]`, `scores: dict[str, float|None]`, `monitoring` (next checks), `why_it_matters`, `detected_at` (stamped at dispatch when unset), `checklist` (pre-rendered ✅/⚠ lines on buy-side alerts; missed soft checks annotate, never suppress — operator rule 2026-07-12), `history_note` (pre-rendered "3 prior alerts — first 2d 4h ago"; stamped by the controller BEFORE recording the current batch so only prior alerts count; empty on first alerts — operator complaint 2026-07-14). `render()` produces the delivered text.
- `gate_events_by_interest()` downgrades protective alerts to LOW unless the operator has interest — but a HIGH opportunity alert in the same batch grants interest immediately.

### Scanners (`meme_intelligence/scanners/`)

- `DiscoveryEngine` (`discovery.py`) — Layer 1. `evaluate(pools)` dedupes (one entry per base token, deepest-liquidity pool wins), applies hard gates, returns `(TokenCandidate list sorted by score desc, RejectedPool list with reasons)`. Hard filters from `DiscoverySettings`: `liquidity_usd is None` → rejected ("cannot verify pool"); `< min_liquidity_usd` (default 5000) → rejected; age `> max_age_hours` (24) → rejected. Discovery Score = 4 components × 25: freshness (linear decay over window), liquidity (scaled `min_liquidity_usd`→`target_liquidity_usd` 50000), volume (`min_volume_24h_usd` 1000 → `target_volume_24h_usd` 50000), activity (buys+sells vs `target_txns_24h` 200). Unknown component = 0, never guessed. Discovery is NOT confirmation.
- `LaunchMonitor` (`launch_monitor.py`) — Pump.fun funnel (`PumpFunSettings`, opt-in via `enable_in_monitor=False`). Basic filter: accepted launchpads (`launchpads="pump"`), dev-buy ≤ `max_creator_buy_percent` 20%. Tracked launches (cap `max_pending=500`, `pending_ttl_hours=24`; stale entries expired BEFORE the capacity check — otherwise a full table rejects forever) are rechecked every `recheck_interval_seconds=120`, budget `max_rechecks_per_cycle=8`. Promotion gates: mcap growth ≥ 1.5×, `min_usd_market_cap=10000`, `min_reply_count=5`, last trade ≤ 30 min. Promoted candidates sit in `READY` (TTL 72 h) until independent market confirmation; 3 consecutive frontend 404s drops a launch.

### AI reasoning (`meme_intelligence/ai/reasoning.py`)

- `AIJudgmentService.judge(result, mode)` sends the condensed `build_intelligence_snapshot()` (never raw API payloads) to Anthropic with `JUDGMENT_SCHEMA`-constrained JSON output. Returns `AIJudgment` (FoundationInputs + NarrativeInputs slots, `bull_case`/`bear_case`, `confidence`, `model`, `mode`) or `None` on any failure/refusal/invalid response — pipeline continues on deterministic evidence. It feeds inputs into the locked scoring framework; it never overrides a computed score.
- Schema: 12 nullable 0–100 slots (meme_strength, narrative, brand, dev_communication, long_term, memorability, shareability, emotional_impact, cultural_timing, community_participation, community_creativity, long_term_strength), 3 nullable bool risk flags (short_term_hype_risk, trend_dependency_risk, copycat_risk), `narrative_category`/`narrative_stage` enums, `narrative_summary`, `bull_case`/`bear_case` arrays, `confidence` + `confidence_reason`. Range-checked in `_parse()` (schema can't express min/max); bools explicitly rejected as scores; prose passes the Part 16 `check_language` banned-language guard.
- `AISettings` defaults: `model="claude-opus-4-8"`, `max_tokens=4096`, `effort="high"`, `requests_per_minute=10`, `timeout_seconds=120`, `min_confidence=20` (below → judgment discarded), `enable_in_monitor=False` (judging every token is expensive), `verify_opportunities=True`, `verify_skip_rug_score=10.0`.
- When it runs: only for candidates firing `high_priority_opportunity`/`strong_candidate` alerts (workflow/controller.py ~line 688), and only after every free screen passed — the paid call is the LAST check. Any fired rug signal (rug score ≥ `verify_skip_rug_score`; smallest signal weight is 10) skips the spend. Result is cached per (chain, address) in `_ai_verified`; a vetoed token is NOT cached so verification can run on a later clean recheck. Post-judgment the token is re-scored through the locked weights and the SAME gates run again — a score drop silently kills the alert. `enrich_with_ai` also skips when `security.is_destructive` or a judgment already exists (pipeline.py:275).
- `build_judgment_service()` returns `None` without `MEMEINTEL_ANTHROPIC_API_KEY`; constructs `AsyncAnthropic(max_retries=0)` deliberately — SDK internal retries would bypass the project RateLimiter and stack timeouts. Also in `ai/`: `prompts.py` (ANALYST_SYSTEM_PROMPT + banned-language list), `comparison.py`, `report_generator.py`.

---


# Part 4 — Collectors: Every Data Source

### Collectors layer — `meme_intelligence/collectors/`

#### base.py — `BaseCollector`
- Common ancestor for every HTTP client. Provides per-provider `RateLimiter.acquire()` before each request, optional `TTLCache` (cache hit skips the request entirely — no rate token consumed), `retry_async` exponential backoff (defaults: 4 attempts, base 0.5s, max 8s), 10s total timeout, `async with` / `close()` session lifecycle.
- Error taxonomy (`meme_intelligence/core/errors.py`): 429 → `RateLimitedError` (honors `Retry-After`, clamped 0–120s); 5xx / timeout / `aiohttp.ClientError` → `TransientCollectorError` (retried); other non-200 or bad JSON → `CollectorError` with `status_code` attached. Callers use `exc.status_code == 404` to convert "not listed" into `None` (a data gap, not a failure — Rule 8).
- `_get_json(path, ..., error_status_as_json=frozenset())`: `path` may be absolute URL; POST when `json_body` given; statuses in `error_status_as_json` return the parsed error body instead of raising (only Jupiter uses this).
- `redact=` tuple scrubs secrets (e.g. the Helius key embedded in the URL path) from every error message before it can reach a log (Rule 16).
- Sessions use `trust_env=True` + default SSL context, so `HTTPS_PROXY`/`SSL_CERT_FILE` work.

#### market_data.py — DexScreener, GeckoTerminal, CoinGecko
- `DexScreenerClient` (`https://api.dexscreener.com`, free, no key; budget 240 req/min vs documented 300). `get_token_pairs`, `search_pairs`, `get_pair`, `get_token_boost` (scans `token-boosts/top/v1` + `latest/v1`, ~30 entries each, 60s TTL; `None` means "not in the current boosted set", NOT zero boosts; boosts are paid promotion, never endorsement).
- `GeckoTerminalClient` (`https://api.geckoterminal.com`, free; budget 25/min vs ~30). `get_new_pools` (10s TTL — primary Layer-1 discovery), `get_trending_pools` (60s), `get_token_pairs` (30s; chain REQUIRED — missing chain raises `CollectorError` so a pool skips to the next provider).
- Invariant: both `get_token_pairs` implementations filter to pairs where the queried token is the BASE side — quote-side pairs describe the counterparty token (bug-hunt finding). GeckoTerminal network ids are translated back to canonical chain ids via `from_geckoterminal_network` (aliases: ethereum↔eth, polygon↔polygon_pos, avalanche↔avax, bnb↔bsc) or cross-verification and CoinGecko lookups silently break off-Solana.
- `CoinGeckoClient` (free; optional demo key via `x-cg-demo-api-key` header, `MEMEINTEL_COINGECKO_API_KEY`; budget 10/min). `get_majors()` → `MajorsSnapshot` (BTC/ETH/SOL 24h, 120s TTL) for the market-environment check; `get_community_profile(token)` → `CommunityProfile` or `None` (chain unmapped in `_COINGECKO_PLATFORMS` or 404). 404s are negatively cached 600s under `<key>:404`. Reddit zeros with no subscriber base are treated as untracked, not zero activity.
- Both DexScreener and GeckoTerminal normalize into the shared `DexPair` model; malformed entries are logged and skipped, never abort the batch.

#### market_service.py — `MarketDataService`
- Wraps market providers in a `ProviderPool` (failure_threshold=3, cooldown_seconds=60): `get_token_pairs` fails over automatically; `get_best_pair` returns deepest-liquidity pair or `None` on `AllProvidersFailedError`.
- `cross_check_liquidity(pair)` → `(True|False|None, note)`: agree within `_AGREEMENT_FACTOR = 2.0` → `True`; disagree → `False` (confidence must drop); no second source → `None` (unknown, never treated as confirmed). $0-vs-$0 counts as agreement (dead pool). Provenance is tracked per pair in a 4096-entry LRU so the verifier excludes the provider that actually answered (failover means it isn't always `providers[0]`).
- `search_pairs` tries providers that expose the method (DexScreener only); empty result = "no evidence", not uniqueness confirmation. `health()` exposes the pool snapshot.

#### security_data.py — `GoPlusClient`
- Free GoPlus API (`https://api.gopluslabs.io`; budget 20/min, 300s TTL). `get_token_security(chain, address)` → `SecurityProfile | None`. Solana (`SOLANA_CHAINS = {"solana","sol"}`) and EVM (`CHAIN_TO_GOPLUS_ID` numeric ids) use different endpoints AND field layouts (`_parse_solana` vs `_parse_evm`).
- Parsing quirks: booleans are `"1"`/`"0"` strings, empty = unknown (`_flag`); percentages are fractions of 1 (`_fraction_to_percent`, NaN/inf → `None`); envelope code must be 1 (complete) or 2 (partial — still usable); result keys matched case-insensitively; empty result → `None`.
- Holders: `_holder_percents` (concentration math — excludes locked + burn addresses, keeps address-less entries) vs `_top_holders` → `TopHolder` tuple (also drops address-less entries; kept separate on purpose — merging would silently shift existing security scores, Rule 3). `_lp_locked_percent` sums locked/burned LP share. Solana: `non_transferable` maps to `cannot_sell_all` (honeypot equivalent); authorities read via `{status}` sub-dicts.

#### pumpfun.py — PumpPortal WS + frontend API
- `PumpPortalClient` (NOT a `BaseCollector` — long-lived stream, `wss://pumpportal.fun/api/data`, free/keyless). ONE data connection per client — multiple connections risk a temporary ban. Lifecycle: `start()` (idempotent, spawns `pumpportal-listener` task) → `drain_launches()` / `drain_migrations()` each scan cycle → `close()`. Subscribes `subscribeNewToken` + `subscribeMigration`; bounded deques (2048 launches / 512 migrations, oldest dropped). Reconnect backoff 1s→60s; backoff resets only after a message actually arrives (a handshake-then-drop server would otherwise be hammered at 1/s). Outage events are lost (no replay) — accepted gap. Discovery is never confirmation: launches enter the pipeline only after a market provider sees them.
- `PumpFunFrontendClient` (`https://frontend-api-v3.pump.fun`, unofficial, keyless; host has rotated before so base URL is config; budget 30/min). `get_coin_state(token)` → `PumpFunCoinState | None` (404 → `None`; 60s TTL). Curve progress derived from `real_token_reserves` against the 793.1M-token protocol constant.

#### jupiter_data.py — `JupiterClient`
- Jupiter Swap API (`https://api.jup.ag`); REQUIRES a key (`MEMEINTEL_JUPITER_API_KEY`, `x-api-key` header, $0 "Free" plan ≈1 rps; budget 50/min). Statuses 400/404/422 are parsed as JSON "no route" bodies (`error_status_as_json`), returned as `None`, never a failure.
- `check_round_trip_liquidity(mint, probe_sol_amount=0.3 SOL, slippage_bps=500)`: quote buy in SOL (`SOL_MINT`), then quote selling back. No buy route → route not found. Full-size sell fails → CONFIRM with a 5% (`sell_confirm_fraction`) sell before condemning: tiny sell also fails = real honeypot; tiny sell succeeds = thin pool, sellable, loss unknown. Full round trip returns `live_round_trip_loss_percent`.
- `get_quote(..., use_cache=False)` and `build_swap_transaction` serve live trading: quote cache TTL is 45s, so live trades MUST pass `use_cache=False`; swap builds cap `dynamicSlippage` at `max_slippage_bps=500` and priority fee at 1,000,000 lamports; `simulationError` raises.

#### wallet_data.py — Helius, Birdeye, `WalletDataService`
- `HeliusClient` (keyed, metered; key embedded in URL path — passed via `redact` so errors never leak it; budget 120/min). `get_top_holders(mint)`: `getTokenSupply` + `getTokenLargestAccounts` + `getMultipleAccounts`, aggregated BY OWNER (a whale split across token accounts must not be understated), burn owners excluded, owners cache keyed by sha256 fingerprint of the account list (stale-owner misalignment fix). `get_recent_transfers` uses the Enhanced Transactions API (`https://api.helius.xyz`). `_token_amount` falls back through `uiAmountString` and `amount/10**decimals` (nullable `uiAmount`).
- `BirdeyeClient` (keyed, metered CU budget; `X-API-KEY` + `x-chain: solana` headers; budget 20/min). `get_token_overview` (holder count, unique 24h wallets, 120s TTL); `get_recent_trades` (swaps with wallet/side/USD, 60s TTL). Non-`success` payload raises `CollectorError`.
- `WalletDataService.gather(token)` → `WalletIntelData`: each provider call wrapped in its own try — a partial failure degrades that field, never drops already-fetched data or the source tag. Requires at least one provider.

All rate limits, base URLs, and keys live in `meme_intelligence/config/settings.py` (`MEMEINTEL_*` env vars); nothing is hardcoded in the collectors beyond safe defaults.

---


# Part 5 — Analyzers and the Master Score

### Shared conventions — `common.py`

- `SubScore(category, requires_signal=False)`: accumulates evidence per category. Two styles: deduction (base 100, subtract per finding) and signal (average 0-100 `signal()` calls, then subtract deductions). `requires_signal=True` returns `None` when no signal was recorded (no fabricated perfect scores). Zero known facts → `score()` is `None` — absent data is reported, never scored.
- `Finding(category, severity: RiskTier, message, deduction)`; `flag_destructive()` adds a `RiskTier.DESTRUCTIVE` finding with 0 deduction (handled by overrides, not arithmetic).
- `observe(field, value)` counts known vs unknown facts; `confidence_from_facts(known, unknown)` → MEDIUM if known ratio >= `MEDIUM_CONFIDENCE_KNOWN_RATIO = 0.40`, else LOW. HIGH is deliberately unreachable until multi-source confirmation exists (Rule 9).
- Every analyzer weights sub-scores, drops `None` categories, renormalizes over available weight, and reports that weight as `coverage`. All raise `InsufficientDataError` when coverage is 0. `scale`/`scale_inverted` map value ranges linearly to 0-100.

### `scoring_engine.py` — master score

- `ScoringEngine(ScoringWeights, ClassificationBands).evaluate(security, community=, onchain=, foundation=, token_structure=, risk=, momentum_score=, narrative_score=, timing_score=)` → `MasterAssessment` (final_score, coverage, classification, overrides, decision_trace, confidence).
- Weights (Part 31 lock, `ScoringWeights` defaults): foundation/security/community/blockchain/momentum/narrative 0.15 each, timing 0.10. `foundation` = average of `FoundationAssessment.overall_score` and `TokenAssessment.overall_score` when both exist.
- Bands (`ClassificationBands`): elite ≥90, strong_candidate ≥80, watchlist ≥70, speculative ≥60, else AVOID.
- Red-flag overrides force AVOID: any security destructive finding; `community.is_artificial`; `risk.category is RiskCategory.EXTREME`.
- Decision tree (6 questions): contract sub-score <30 or destructive → reject; liquidity sub-score <40 → cap SPECULATIVE; artificial community → reject; onchain overall <40 → cap SPECULATIVE; narrative <40 → note only; risk EXTREME → reject, HIGH → cap SPECULATIVE. Unknown answers never fail — they reduce confidence.
- `derive_timing_score(pair, token, onchain)`: averages pair age (≤24h→90, ≤7d→70, ≤30d→50, else 30), stage (EARLY 85/GROWTH 60/MATURE 30), phase (ACCUMULATION 80/EXPANSION 70/UNCLEAR 50/DISTRIBUTION 20).
- Gotcha: `_weighted()` clamps to 100.0 — float renormalization overshoot once crashed `classify()` and aborted the whole cycle.

### `security_analyzer.py`

- `SecurityAnalyzer(SecurityThresholds, SecuritySubWeights).assess(SecurityProfile, market: DexPair|None)` → `SecurityAssessment`. Sub-weights: contract 0.25, liquidity 0.20, distribution 0.20, developer 0.20, manipulation 0.15.
- Destructive (overall forced to 0 and later forces AVOID): honeypot, cannot_buy, cannot_sell_all, live Jupiter buy-route-but-no-sell-route, round-trip loss ≥90% (`extreme_round_trip_loss_percent`), fake_token, is_airdrop_scam.
- Key thresholds (`SecurityThresholds`): max/extreme tax 10/25%, min/healthy liquidity $5k/$50k, LP lock min/good 50/80%, top holder warn/max 10/20%, top10 50/70%, min holders 50, creator warn/max 5/10%.
- Bands: ≥90 Excellent, ≥75 Good, ≥50 Moderate Risk, ≥25 High Risk, else Extreme Risk. Liquidity USD comes from `market` (DexPair), not GoPlus.
- `security_monitor.py` diffs stored facts against each new `SecurityProfile` (`extract_facts`/`merge_facts`/`detect_security_changes`): CRITICAL flips (honeypot, mint authority, balance_mutable...), HIGH flips (blacklist, pausable, proxy...), numeric worsenings (LP lock −30pts CRITICAL, tax +5pts HIGH, concentration +10pts HIGH). Known→unknown keeps the last known value.

### `momentum_analyzer.py`

- Four lenses at 0.25 each (price, volume, social, onchain); social is usually "no data" until social collectors land.
- Price: base signal 40 + 24h% change; consistency: all 1h/6h/24h positive → 90, 24h up but 1h red → 35, mixed → 60; **flat-trend rule**: all three windows inside ±`flat_trend_band_percent` (2.0%) → 40, not 90 — fix for stale coins re-alerting as fresh entries (2026-07-14 operator complaint). 1h spike ≥30% deducts 15.
- Volume: acceleration = (volume_1h×24)/volume_24h; ≥1.5 → 90, ≤0.5 → 30. Onchain lens: 1h-vs-24h buy-ratio shift ±0.05 → 85/60/35; volume_quality <40 deducts 25 ("fake momentum").
- Entry zones: 24h change ≥100% → LATE (action always MONITOR); pair age ≤24h → EARLY; overall ≥45 → CONFIRMATION; else UNCLEAR. Actions: <30 AVOID, ≥70 in EARLY/CONFIRMATION → CONSIDER_RESEARCH_ENTRY, ≥45 WAIT_FOR_CONFIRMATION, else MONITOR.

### `wallet_intelligence.py`

- `WalletIntelligenceAnalyzer(WalletIntelSettings, SmartMoneySubWeights).assess(WalletIntelData, pair, reputations=)` → `WalletAssessment`. Five lenses at 0.20 each: quality_wallets (net buyers >$10 dust scaled to `target_accumulating_wallets`=10), historical_success (needs Part 24 reputations, else no data), entry_timing (buy volume ≤ price-range midpoint: ≥60% → 80, ≤40% → 40, else 60; flat range <2% needs ≥5 priced buys for the 80 — same 2026-07-14 stale-coin fix), holding_behavior (30 + 60×stable-whale fraction; RISK whales deduct 10 each, max 2), risk_signals (≥30% identical-size trades → −30; one wallet ≥60% of buy volume when buys ≥$500 → −30; ≥2 top holders still adding → −15).
- Whale classification (`whale_min_percent`=1%): pool address / `KNOWN_EXCHANGE_OWNERS` (4 hardcoded Solana wallets) → CUSTODIAL (excluded from whale math); ≥5% (`risk_whale_percent`) → RISK; active in trades → TRADING; else LONG_TERM. Exchange flows are **lower bounds**.
- Pump-and-exit finding: 24h price +≥50% while whale outflow ≥$500. Accumulation verdict: any SERIOUS_WARNING risk_signals finding → ARTIFICIAL; ≥max(3, target/2) net buyers and buys>sells → HEALTHY; else MIXED/UNKNOWN.
- `wallet_reputation(WalletTrackRecord)` → `(score, coverage)` or `None`: win_rate 0.25, early_entry_rate 0.20, risk management 0.20 (median position $50–$250k → 75 else 40), rug_avoidance_rate 0.20, consistency 0.15 (≥30 days active and ≥5 tokens → 80 else 45); renormalized over measured components; no track record → `None`, never neutral.
- Wallet analysis costs paid API credits: `enable_in_monitor=False` keeps it out of the continuous scanner.

### `onchain_analyzer.py`

- Six sub-scores (`OnChainSubWeights`): holder_health 0.20, smart_money 0.20, whale_behavior 0.15, developer_activity 0.15, volume_quality 0.15, token_flow 0.15. Smart-money/whale/token-flow slots stay "no data" until `enrich_onchain_profile()` fills them from a wallet assessment; `derive_onchain_profile(pair, security)` builds the rest for free.
- Volume quality: trades-per-trader scaled 3→10 (≥10 → −30 wash-trading); volume-per-holder $500→$5000 (≥$5000 → −25); buy ratio <0.35 → 30 signal, >0.85 → 60 (bundled entries suspicion), ≥0.60 → 100.
- Phase (`_classify_phase`): 24h change >+10% and buy ratio ≥0.55 → EXPANSION; |change| ≤10% and ratio ≥0.5 → ACCUMULATION; ratio <0.5 and change ≤0 → DISTRIBUTION; else UNCLEAR. Feeds timing and momentum.

### `opportunity_ranker.py`

- `OpportunityRanker(OpportunityWeights).rank(CategoryScores, risk_score)` → `OpportunityRank`. Second, upside-tilted axis — never modifies the Part 31-locked master score. Weights: growth_potential 0.30 (← narrative), momentum 0.25, foundation 0.20, risk 0.15 (as 100−risk_score), timing 0.10. Missing factors drop out and renormalize.

### Other analyzers (brief)

- `community_analyzer.py`: 5 lenses × 0.20 (engagement, growth, loyalty, creativity, dev_relationship); confirmed fake community → `is_artificial` → master AVOID. Ratings: ≥85 EXCELLENT, ≥70 STRONG, ≥50 AVERAGE, else WEAK.
- `foundation_analyzer.py`: combines qualitative `FoundationInputs` (AI/analyst 0-100 slots) with the community score; weights meme_strength 0.20, narrative 0.20, brand 0.15, community_quality 0.20, dev_communication 0.15, long_term 0.10.
- `narrative_analyzer.py`: viral score and narrative-intelligence score (each 5 lenses × 20%); the intelligence score fills the master `narrative` category. Sentiment is classified but never deducted (already counted in community loyalty — documented double-count avoidance).
- `risk_analyzer.py`: 0-100 where **higher = riskier** (inverted vs everything else); weights security 25/market 20/community 15/token 20/execution 20; bands ≥75 EXTREME, ≥50 HIGH, ≥25 MODERATE, else LOW_RELATIVE; a mostly-unverifiable profile is floored at HIGH. Also `PortfolioRiskManager` and `emergency_flags()`.
- `token_analyzer.py`: economic structure (valuation, liquidity depth, supply, volume sustainability) with `MarketCapStage` classification (EARLY 85 / GROWTH 65 / MATURE 40 stage signals); averaged into the master `foundation` input.

Files: `/home/user/Memecoin/meme_intelligence/analyzers/*.py`; all weights/thresholds live in `/home/user/Memecoin/meme_intelligence/config/settings.py` (env-overridable, `MEMEINTEL_*`), nothing is hardcoded except documented signal anchors inside the analyzers.

---


# Part 6 — The Scan Cycle, Watchlist, and Orchestration

### meme_intelligence/workflow/ — the orchestration layer

Five modules: `controller.py` (24/7 loop), `pipeline.py` (shared per-token analysis), `watchlist_review.py` (shared recheck), `daily_routine.py` (once-a-day desk run), `smart_wallets.py` (passive holder recorder).

### ContinuousScanner scan cycle (controller.py)

`ContinuousScanner.run()` loops `_run_cycle()` every `workflow.monitor_interval_seconds` (default 45s), with exponential error backoff 5s→300s that resets on success; one failing cycle never kills the loop (broad `Exception` backstop — `CancelledError`/`KeyboardInterrupt` still propagate). Per cycle:

- **Discovery**: `scan_new_pools(gecko, DiscoveryEngine, workflow.network_list)`; a `CollectorError` here degrades to an empty candidate list — the rest of the cycle still runs. Top `workflow.top_candidates` (default 5) go to the pipeline; keys already in `_seen` or `_retry_pending` are skipped.
- **Pipeline** (`ResearchPipeline.analyze_pair`): GoPlus security (returns `None` → token NOT marked seen — retryable later) → optional Jupiter round-trip probe (Solana + `liquidity_probe.enabled`) → wallet intelligence (Solana + service wired) → community → onchain/token/momentum/narrative analyzers → `RiskAnalyzer` → `ScoringEngine.evaluate` → `OpportunityRanker`. Every entry point (scanner, daily routine, CLI, Telegram `/check`) scores through this one class.
- **`_process_result`** in order: read `previous_score` + `peak_score` (before this run's snapshot, so a first look never reads "below peak"); provisional rules pass; free screens; `record_snapshot`; **security-facts diff** (`detect_security_changes` vs `latest_security_facts`, then `record_security_facts(merge_facts(...))`); **smart-wallet recording**; watchlist tiering/archiving; final `AutomationRules.evaluate` (with `peak_score`, `ai_verification_inconclusive`, `deterministic_risk_veto`, `operator_interest`); AI annotation; `_verify_events` market cross-check; security-change events appended (bypass market verification, pass interest gate — a HIGH alert in the same batch counts as interest); mute check (fails OPEN); **history-note** (`_history_note`, "N prior alert(s)… first alerted 2d 4h ago", best-effort, prior alerts only); dispatch; `record_alert` + journal per delivered event; `_feed_learning`.

### Veto/verification gating

- `_deterministic_risk_veto` (zero-cost) runs whenever ANY buy-side alert would fire: risk alerts already firing (`emergency_review`/`risk_warning`) → veto; `RugEngine` score >= `ai.verify_skip_rug_score` (default 10.0) → veto; then `_mind_layer_veto` (fires only if `learning.veto_enabled`, earned authority via `veto_gate` with `veto_min_accuracy` 0.70 over `veto_min_samples` 10, cached `veto_metrics_ttl_seconds` 1800, and P(rug) >= `veto_min_p_rug` 0.85 — abstains on any error).
- `_copycat_veto` (one paid search) only for HIGH tiers (`high_priority_opportunity`/`strong_candidate`): `_find_established_duplicate` flags a same-symbol/name token with liquidity >= max(`copycat_min_liquidity_usd` 100k, `copycat_liquidity_ratio` 10x candidate). Verdicts cached per token; outages never cached.
- **AI verification**: two independent flags — `ai.enable_in_monitor` (judge every token, default False) vs `ai.verify_opportunities` (default True; judges only HIGH-tier gate-passers). An `or` between them was a real bug — do not reintroduce. `_ai_verified` caches per token for the scanner's lifetime, storing whether the verdict was inconclusive (judgment discarded / call failed) so the veto holds on rechecks. Vetoed tokens are NOT cached — they can be verified later. Paid call is always LAST, after all free screens.
- `_verify_events`: alerts in `_VERIFIABLE_ALERT_TYPES` (`high_priority_opportunity`, `strong_candidate`, `early_opportunity`, `momentum`) get `market.cross_check_liquidity`; disagreement downgrades HIGH→MEDIUM (else →LOW), unavailability annotates "unverified" — never a silent pass.

### Operator interest

`_operator_interest`: True if `storage.is_holding(token)` (Telegram `/holding`) or any prior alert in `INTEREST_ALERT_TYPES`. Fails OPEN (lookup error → full priority). Gates protective/risk alerts when `alert_engine.risk_alerts_require_interest` (default True).

### Watchlist tiers, rotation, archiving

- `TIER_FOR_CLASSIFICATION` (watchlist_review.py): ELITE/STRONG → TIER_1_HIGH_PRIORITY, WATCHLIST → TIER_2_DEVELOPING, SPECULATIVE → TIER_3_RESEARCH_ONLY; AVOID has no tier.
- Recheck every `workflow.watchlist_recheck_cycles` (10) cycles, up to `watchlist_review_limit` (10) entries, **least-recently-updated first** (bug fix: tier/score ordering starved everything below top-N forever). Tier-3 entries are skipped in the scanner (daily routine handles them).
- **Three archive conditions**: (1) a SUCCESSFUL `get_token_pairs` call returns no pairs ("no active trading pairs remain") — provider outages raise and are skipped, never archived (a 2-failure blip once permanently archived healthy tokens; use `get_token_pairs`, not `get_best_pair`); (2) re-assessment falls to AVOID; (3) liquidity < `alert_engine.dead_liquidity_usd` (500) — a dead token also never re-enters the watchlist regardless of score. **NO staleness door exists yet** — approved as the next build.

### Launch funnel & retries

- Launch monitor active only when pumpportal + pumpfun + market_service are all wired (no market service → stage off, warning logged). Flow: stream → `collect_launch_candidates` (traction rechecks) → independent market confirmation via `get_best_pair` (discovery is never confirmation) → same pipeline. Not indexed / no security data → `defer()`; already seen → `confirm()`.
- **Insufficient-data retry** (`_finalize_or_reschedule`): AVOID with no overrides and coverage < `insufficient_data_min_coverage` (0.5) on a pool younger than `insufficient_data_max_age_minutes` (120) is rescheduled every `insufficient_data_retry_minutes` (15) instead of being marked `_seen`. `_retry_pending` stores (due-time, **case-preserved address** — Solana base58 is case-sensitive, the lowercased key is dedup-only, gotcha), giveup-deadline. `_repace_retry` after any non-terminal outcome prevents per-cycle provider hammering.

### Bounded caches

`_BoundedKeySet`: OrderedDict-backed FIFO-evicting set/map, capacity `workflow.max_tracked_keys` (50000). Used for `_seen`, `_retry_pending`, `_ai_verified`, `_copycat_verdicts`; `SmartWalletRecorder` uses `smart_wallet.max_seen_keys` (5000). No `remove()` — stale entries are tolerated dead weight. Copycat cache uses the `_UNSET` sentinel so a cached `None` ("no duplicate") isn't a miss.

### SmartWalletRecorder (smart_wallets.py)

Passive-only, behind `settings.smart_wallet.enabled`. Records GoPlus top holders (already fetched, zero API cost) once per token, side `"hold_top10"`, source `DEFAULT_SIGHTING_SOURCE`, via `record_wallet_sightings`. Never raises. Empty holders leave the token unmarked so a later recheck with data still records. Dedup is in-memory; post-restart duplicates are handled by DISTINCT at query time. Trade streams deliberately unbuilt (PumpPortal per-trade streams are metered — DECISIONS_LOG 2026-07-14).

### DailyRoutine & shutdown

`DailyRoutine.run()`: majors check → regime (BULL if BTC 24h >= `risk_on_btc_change_percent` 2.0, BEAR if <= -`risk_off_btc_drop_percent` 3.0, else NEUTRAL; provider failure → UNKNOWN) → discovery/analysis/intake → `review_entries` → `DailyReport.render()` journaled. AVOID rejections are journaled for learning. Graceful shutdown: SIGINT/SIGTERM set `_stop` via `request_stop()`; current cycle finishes; Telegram listener start/stop failures never affect scanning. Learning retrain runs via `asyncio.to_thread` (a synchronous rebuild once stalled emergency `/dump`).

---


# Part 7 — Alerts: Types, Vetoes, Telegram Commands

### Alerts subsystem — `meme_intelligence/alerts/`

### Alert types (`notification_engine.py`, `AutomationRules`)
- `emergency_review` — CRITICAL. Destructive finding from `emergency_flags(security, onchain)` (e.g. honeypot/LP pull). Only alert that survives token death.
- `risk_warning` — HIGH. Serious-but-not-destructive flags from `emergency_flags` (first 4 reasons).
- `high_priority_opportunity` — HIGH. All 5 gates (`overall>=85`, `security>=80`, `onchain>=75`, `liquidity>=70`, `community>=70`, defaults in `AlertThresholds`) have data and pass, and no caveat/veto.
- `strong_candidate` — HIGH. Only `community` unverified (`_STRONG_CANDIDATE_ALLOWED_UNVERIFIED`), score >= `strong_candidate_overall` (88), no caveats. Caveats that demote either HIGH tier to `early_opportunity` MEDIUM: liquidity below `strong_candidate_min_liquidity_usd` ($25,000, unknown also vetoes), AI confidence < `strong_candidate_min_ai_confidence` (40), `ai_verification_inconclusive=True`, or `deterministic_risk_veto` (bug fix: the fully-verified tier used to bypass all vetoes).
- `early_opportunity` — MEDIUM. Gates-with-data pass but some unverified, or a HIGH tier held back by caveats. Provisional, explicitly not a recommendation.
- `momentum` — MEDIUM. `momentum.overall_score >= 70` and entry zone not `LATE`; skipped if destructive.
- `smart_money_accumulation` — MEDIUM. `AccumulationVerdict.HEALTHY`, quality_wallets sub-score >= 60, not destructive.
- `whale_exit` — HIGH. `whales_selling >= 2`, or negative whale net flow with >= 1 seller.
- `insider_risk` — HIGH. `AccumulationVerdict.ARTIFICIAL`.
- `community_fake` — HIGH. `community.is_artificial`.
- `score_drop_review` — HIGH. Score fell >= `score_drop_review_points` (15) vs previous snapshot.
- `token_death` — MEDIUM. Liquidity finite and below `dead_liquidity_usd` ($500). One post-mortem replaces everything except same-batch CRITICAL `emergency_review`; NaN/None liquidity is never death (Rule 8).
- `security_change` — via `events_from_security_changes()`, one event per severity (CRITICAL/HIGH/MEDIUM) so a critical change is never buried in a medium digest; carries `master_score`.

### Suppression/veto chain in `AutomationRules.evaluate` (order matters)
1. **Dead token**: `_token_death_rule` short-circuits — returns only CRITICAL emergencies + the post-mortem, then interest-gates.
2. **Hard suppression of ALL buy-side types** (`_BUY_SIDE_ALERT_TYPES` = the two HIGH tiers + early_opportunity, momentum, smart_money_accumulation) when any of: `deterministic_risk_veto` set (rug engine COMBINED score / firing risk alert / honeypot / earned mind p(rug) vote — never a single soft flag); `_untradeable` (liquidity OR mcap None/NaN/<=0 — missing counts as untradeable); `_oversized` (liquidity > `opportunity_max_liquidity_usd` or mcap > `opportunity_max_market_cap_usd`; defaults ON since 2026-07-14 at 50000/100000, 0 = OFF, unknown never trips it); `_too_old` (pool older than `opportunity_max_age_hours`, default 24h, 0 = OFF; unknown creation time never trips it — the checklist shows "Pool age: not verified" instead).
3. **Weak-tier suppression** (`_DECLINE_SUPPRESSED_TYPES` = early_opportunity, momentum, smart_money_accumulation; strong tiers exempt): `_score_declining` (one-step drop >= 15) OR `_below_peak` (score >= `peak_decline_suppression_points` (15) below the token's all-time peak — closes the "collapsed coin creeps back +2-3/recheck for days" bug). First-ever look (`previous_score`/`peak_score` None) never suppresses.
4. **Safety checklist** attached (never suppresses) to surviving buy-side alerts: sellable, mint authority, freeze authority, sell tax vs `checklist_sell_tax_max_percent` (15%), deployer honeypot history, liquidity vs `opportunity_min_liquidity_usd` comfort floor (0.0 = note only), market cap floor (only if set), top-wallet concentration note ("normal for a new launch" when pool younger than `checklist_new_launch_minutes` = 60). Icons pass ✅ / warn ⚠️ / note ℹ️ / unknown ❔; header "passed X/Y" counts only pass+warn.
5. **Interest gate** (`gate_events_by_interest`, enabled by `AlertEngineSettings.risk_alerts_require_interest=True`): if `operator_interest=False` and no HIGH `INTEREST_ALERT_TYPES` (`high_priority_opportunity`, `strong_candidate`) in the same batch, all `_PROTECTIVE_ALERT_TYPES` (emergency_review, risk_warning, score_drop_review, token_death, whale_exit, insider_risk, community_fake, security_change) demote to LOW with a "informational only" reason — below external sinks' min-priority, so the phone never buzzes.

### NotificationEngine (dispatch + cooldown)
- Cooldown key `(chain, address.lower(), alert_type, priority)`; window `AlertEngineSettings.cooldown_seconds` = 900s. Priority is in the key so a MEDIUM security_change never eats a later CRITICAL one. Entries pruned when map > 256.
- Events ranked by `rank_alert` (Part 29 S10 weights: impact 40 / confidence 30 / urgency 20 / novelty 10); confidence = `30 + 20*len(reasons)` capped 100.
- Delivery accounting: sinks return True/False/None (filtered). When any sink has `external=True` (Telegram/Discord), only external sinks count as delivery — ConsoleSink (`external=False`) is a log. Cooldown is stamped ONLY after a real delivery, so a Telegram outage retries after the window instead of silently losing the alert. One sink raising never aborts the batch.

### Sinks (`sinks.py`)
- Channel categories (`ALERT_CHANNELS`): discoveries (both HIGH tiers, early_opportunity, new_token_discovery), smart_money (smart_money_accumulation, whale_exit), security (emergency_review, risk_warning, security_change, token_death, insider_risk, community_fake), momentum (momentum), reports (score_drop_review + any unknown type). `routes` maps category→chat id/webhook URL; unrouted falls to default. `parse_routes("category=dest,...")` rejects unknown categories loudly.
- `format_alert`: header per priority (CRITICAL RISK EVENT / HIGH IMPORTANCE UPDATE / MONITOR UPDATE / INFORMATION UPDATE), token block, "Seen before:" line from `event.history_note` (a re-alert must never read as a new discovery), event summary, why-it-matters, evidence, checklist, scores, risk assessment (CRITICAL/HIGH→High, MEDIUM→Medium, LOW→Low), monitoring.
- `TelegramSink` / `DiscordSink`: both `min_priority=MEDIUM` default (LOW filtered, returns None), both built on `BaseCollector` (retries/rate limiting/redaction). Telegram text capped 4000; alert carries `feedback_keyboard`: 👍/👎 (`fb:1:<addr>`/`fb:0:<addr>`), copy-address button (Bot API 7.11 `copy_text`, 256-char cap), and when `buy_presets_sol` non-empty a `Buy N◎` row (`buy:<addr>:<amount>`) plus `💥 Dump all` (`dump:<addr>`). callback_data capped at 64 bytes — an oversized address gets no button, never a truncated one (would route to the wrong token). Discord wraps in a code fence capped 1900, `allowed_mentions: {parse: []}` (token names injected @everyone pings), webhook URLs redacted as credentials.
- **Sanitization**: `_sanitize_identity` (drop non-printables/newlines, backticks→apostrophes, 64-char cap, empty→"unknown") applied to every untrusted token name/symbol/reason; contract address (validated charset) stays exact.

### Telegram commands (`telegram_commands.py`, `TelegramCommandListener`)
Long-polls `getUpdates` (single consumer per token; sink only sends). Only the configured chat id is honored — strangers logged (id only) and ignored. Replies are plain text, no parse_mode, previews off, capped 4000. On startup `_discard_backlog` drains and DISCARDS all pending updates (a buffered `/buy` replayed on restart would be a real trade); a failed drain retries and holds back live polling. `edited_message` is never dispatched (editing a `/buy` would fire a second trade). Addresses must pass `classify_address` (base58 32-44 = solana, 0x+40 hex = evm) before any use.

- `/status` — uptime, cycle stats, layer on/off (trading shows off/dry-run/LIVE), DB counts.
- `/why <address>` — last 3 alerts with reasons, latest score, watchlist tier, holding/muted state, red flags (`_RED_FLAG_FACTS` + failed Jupiter sell probe). Reply carries copy-address button.
- `/check <address> [chain]` — runs the pipeline now; one at a time (`_check_lock`, second caller told to retry), 60s per-token result cache (64 entries); card mirrors the `quick` CLI plus advisory mind line (p(rug), confidence, n).
- `/holding` / `/unhold` / `/holdings` (alias `/positions`) — mark/release/list held coins; held coins keep protective alerts at full priority.
- `/watchlist` — top 8 tracked entries by tier.
- `/boost <address> [chain]` — DexScreener paid-boost amount, explicitly framed as paid promotion.
- `/mind` — learning-layer report card: memory size, hit rate, rug precision/recall, Brier, ensemble accuracies, p(rug) veto authority line (`veto_gate` vs `veto_min_accuracy`/`veto_min_samples`), feedback tallies (advisory only — never a training label).
- `/wallets` — smart-wallet data-clock stats plus reputation section (`compute_wallet_reputations`, top 3 wallets scored only once one has `min_resolved_for_reputation` resolved tokens; wallet strings from GoPlus are sanitized, not just truncated).
- `/mute` / `/unmute <address>` — suppress/restore ALL alert delivery for a token; analysis continues.
- `/buy <address> <sol>` and `/dump` (alias `/sell`) — execute via `TradeIntent` through `ctx.executor`; guarded by `_trading_guard` (`MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED` + executor wired).
- Buttons: `fb:*` records 👍/👎 feedback, linked to the newest alert within 7 days (`_FEEDBACK_ALERT_WINDOW`), deliberately never written to `alerts.outcome` or learning labels. `buy:*`/`dump:*` buttons dedupe on `(message_id, callback_data)` (bounded FIFO of 500) so a double-tap never fires two trades; claim happens after the guard, before execution; popup ack never asserts success — "see chat for the result".
- Poll loop: total error isolation, exponential backoff 2s→60s reset on success; a poison update advances the offset so it never replays forever.

### Gotchas
- `AlertEvent.history_note` and `checklist` are pre-rendered strings stamped upstream (scanner) — `render()`/`format_alert` just print them.
- Console prints everything including LOW; external sinks default MEDIUM+, so interest-gated LOW alerts exist only in console/history.
- The getUpdates offset lives in memory only — restart safety depends entirely on `_discard_backlog`.
- Missing data is never a pass anywhere: unknown gate ≠ pass, unknown liquidity vetoes strong_candidate but never trips the oversized ceiling or death floor.

---


# Part 8 — Trading: Plans, Buy/Dump Buttons, Safety Caps

### meme_intelligence/trading/ — trade planning and manual execution

Three files: `trade_planner.py` (research plans, never orders), `execution.py` (operator buy/dump executors), `solana_rpc.py` (minimal Solana JSON-RPC client). The package docstring states the core invariant: this package **never places orders on its own** — every real trade is an explicit operator action from Telegram (Part 13 Section 8; DECISIONS_LOG 2026-07-10, Project 6). There is no code path that initiates a trade without an operator command or button tap.

### trade_planner.py — TradePlanner (Spec Part 8)

- `TradePlanner(settings: TradingSettings, weights: TradeScoreWeights).build_plan(pair, security, *, discovery, onchain, community, token, regime)` returns a frozen `TradePlan` — explicitly "NOT an order"; `render()` prefixes "[research only, not advice]".
- Trade score: weighted mean over components `setup_quality, security, community, onchain, market_conditions, risk_reward`; missing components are excluded and `score_coverage` records the available weight fraction (Rule 8 — no fabricated data).
- Discipline rules: destructive security finding → `ConvictionLevel.NO_TRADE` and `SetupType.WATCH_ONLY`; coverage below `min_confirmation_coverage` caps conviction at SPECULATIVE; `MarketRegime.BEAR` downgrades conviction one level via `_DOWNGRADE`. WATCH_ONLY setups get `max_position_percent=None` regardless of conviction.
- Constants: `FOMO_QUESTIONS` (5), `PROFIT_DISCIPLINE` (4), `_CONFIRMATION_TASKS` (unknown-field → human task, max `_MAX_CONFIRMATIONS = 6`), invalidation list capped at 10.
- `TradeJournalEntry` dataclass defines the journal schema (Part 8 Section 12).

### execution.py — DryRunExecutor vs LiveExecutor

Both expose `execute_buy(TradeIntent)` and `execute_sell_all(mint, chain)`, returning operator-facing strings (errors are returned, not raised — `TradeError` is caught internally).

- `DryRunExecutor` (`live = False`): signs nothing; journals a `trade_intent` row via `storage.add_journal` and replies "DRY RUN — no real trade executed…". Journaling failures are swallowed (Rule 7).
- `LiveExecutor` (`live = True`): Solana-only. Constructor parses `private_key_base58` with `solders.Keypair.from_base58_string` (lazy import; bad key → `ValueError` with no secret leaked). Holds an `asyncio.Lock` — one trade at a time per wallet.
- Buy path: reject non-finite/≤0 amount (explicit `math.isfinite` — `nan` passes both `<=0` and `>cap` comparisons, bug-hunt 2026-07-11); refuse above `max_buy_sol`; read live balance via `get_sol_balance_lamports` and require `balance >= lamports + _FEE_BUFFER_LAMPORTS` (7,000,000 lamports ≈ 0.007 SOL fee headroom); fresh Jupiter quote (`use_cache=False`); then `_execute_swap`.
- Dump path: reads full raw token balance via `get_token_balance_raw`, quotes token→SOL for 100% of position, swaps. There is no partial sell.
- `_execute_swap` staged reporting rule: once a tx is broadcast, never hide the signature. Pre-broadcast failures → "nothing was spent". `asyncio.CancelledError` during send or confirm journals/logs the signature (derived deterministically from signed bytes via `_signature_of` even before the RPC responds — bug-hunt 2026-07-12) with "do NOT re-tap". Confirmed on-chain failure → "safe to retry"; RPC-unknown outcome or confirm timeout → "Do NOT retry; check Solscan". Signature is journaled (`trade_buy`/`trade_sell`) immediately after broadcast, before confirmation.
- `_confirm` polls `signature_status` every 2 s until `confirm_timeout_seconds` (default 45); `confirmed`/`finalized` → True; on-chain `err` → `TradeError`.
- `_safe()` redacts the wallet pubkey from replies; the private key is never logged (Rule 16).

### solana_rpc.py — SolanaRpcClient

`BaseCollector` subclass (rate limiting, retries, timeouts) over Helius (`https://mainnet.helius-rpc.com`); API key goes in the URL query and is registered in `redact` so logs never show it. Methods: `get_sol_balance_lamports` (non-numeric balance → `CollectorError`, never proceed on bad number), `get_token_balance_raw` (sums all token accounts; unparseable accounts are skipped with a warning — a dump could under-count), `send_raw_transaction` (`skipPreflight: false`, `maxRetries: 3`), `signature_status`. Deliberately separate from `wallet_data.HeliusClient` (Rule 4).

### Wiring, caps, and env vars (`ExecutionSettings`, prefix `MEMEINTEL_EXECUTION_`)

- `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED` (default False) — show Buy/Dump buttons and enable /buy, /dump at all (`_trading_guard` in telegram_commands rejects otherwise).
- `MEMEINTEL_EXECUTION_LIVE_ENABLED` (default False) — gate for real signing.
- `MEMEINTEL_EXECUTION_MAX_BUY_SOL` (default 0.15) — per-trade ceiling, re-checked inside `LiveExecutor`.
- `MEMEINTEL_EXECUTION_SLIPPAGE_BPS` (default 500 = 5%) — passed to quotes and as `dynamicSlippage.maxBps` in Jupiter's `/swap/v1/swap` build (dynamic slippage is capped so the signed tx can never tolerate more than configured — money-safety fix).
- `MEMEINTEL_EXECUTION_PRIORITY_FEE_MAX_LAMPORTS` (default 1,000,000 = 0.001 SOL) — priority-fee cap, level `veryHigh`.
- `MEMEINTEL_EXECUTION_CONFIRM_TIMEOUT_SECONDS` (default 45.0).
- `MEMEINTEL_EXECUTION_BUY_PRESETS_SOL` (default "0.05,0.1") — one-tap button sizes; config load fails if any preset exceeds `max_buy_sol`.
- Secrets (env only, never in code/git/logs): `MEMEINTEL_EXECUTION_PRIVATE_KEY` (base58 key of a **dedicated low-balance wallet**, never the operator's main wallet — the real hard cap is its funding), `MEMEINTEL_EXECUTION_HELIUS_API_KEY` (optional dedicated RPC key; if it differs from the scanner's Helius key it gets its own rate limiter, otherwise the shared one).
- `build_executor` in `__main__.py` picks the executor: `LiveExecutor` only when `live_enabled` AND private key AND Jupiter client AND a Helius key all exist; any construction failure (bad key, missing `solders`) degrades to `DryRunExecutor` with a printed note — never crashes the scanner. On success it prints "LIVE TRADING ARMED — trading wallet <pubkey>. Per-trade cap …".

### Telegram entry points and double-tap dedup (alerts/telegram_commands.py)

- `/buy <address> <sol>` and `/dump <address>` (alias `/sell`) validate the address charset and amount, run `_trading_guard`, then call `executor.execute_buy` / `execute_sell_all` via `_do_buy`/`_do_dump`.
- Buttons send callbacks `buy:<address>:<sol>` and `dump:<address>`. Dedup key is `(message_id, callback_data)`; `_claim_button` records it in `_actioned_buttons`, a bounded FIFO `OrderedDict` (`_ACTIONED_BUTTONS_CAP = 500`). Claimed AFTER the guard check but BEFORE execution, so a double-tap on a laggy client fires exactly one trade (bug-hunt 2026-07-12). Updates are processed serially, making the check race-free.
- `_ALLOWED_UPDATES = '["message","callback_query"]'` deliberately excludes `edited_message` — editing a `/buy` must not re-dispatch a second live trade. Startup also skips the pending backlog so a buffered `/buy` is never replayed.
- The callback popup ack never asserts "sent" — the full reply may be a refusal.

Files: `/home/user/Memecoin/meme_intelligence/trading/execution.py`, `/home/user/Memecoin/meme_intelligence/trading/solana_rpc.py`, `/home/user/Memecoin/meme_intelligence/trading/trade_planner.py`; wiring in `/home/user/Memecoin/meme_intelligence/__main__.py` (`build_executor`), settings in `/home/user/Memecoin/meme_intelligence/config/settings.py` (`ExecutionSettings`, lines ~1288-1357), Telegram handlers in `/home/user/Memecoin/meme_intelligence/alerts/telegram_commands.py`.

---


# Part 9 — The Mind Layer (Self-Learning)

### Mind layer overview (`meme_intelligence/learning/`)

`LearningService` (`service.py`) is the assembled "mind layer": analog memory + LightGBM + rug engine, blended by an adaptive ensemble. Decision-support only — it never trades. Public lifecycle: `record_detection` → `capture_snapshot` (auto-registers unseen coins) → `evaluate_coin` (the per-coin verdict, also persists an ungraded prediction) → `resolve_outcome` (labels the coin, fires instant learning) → `retrain_if_due` / `refresh_archetypes` / `get_learning_metrics`. All config in `LearningSettings` / `LightGBMSettings` / `RugThresholds` / `RugSignalWeights` (`config/settings.py`), overridable via `MEMEINTEL_LEARNING_*` / `MEMEINTEL_LIGHTGBM_*` env vars. Off by default: `MEMEINTEL_LEARNING_ENABLED` and `MEMEINTEL_LEARNING_ENABLE_IN_MONITOR` are both `false`.

### Fingerprints (`features.py`)
- `FingerprintExtractor.extract(series)` compresses a variable-length snapshot trajectory into a fixed vector (`FEATURE_DIM`, names in `FEATURE_NAMES`): 13 base metrics (price, liquidity, market_cap, volume_5m/1h, holders, top10_concentration, dev_outflow, liquidity_event, liq_to_mcap, vol_to_liq, buy_sell_ratio, tx_count) × 7 stats (`last, mean, min, max, delta, slope, vol`) + 3 scalars (`age_hours, snapshot_count, price_acceleration`) + 13 per-metric presence masks.
- USD/count metrics go through signed `log1p` before summarization so similarity matches *shape*, not size. Missing metrics fill 0.0 and lower `Fingerprint.coverage`.
- **Gotcha:** `FEATURE_VERSION = 3`. Changing `_BASE_METRICS`, `_LOG_DOMAIN_METRICS`, or stats requires bumping it; on version mismatch `_load_artifacts` discards all trained artifacts and rebuilds from raw SQLite snapshots (nothing lost).
- `StandardScalerBundle` (persisted sklearn scaler) makes distances meaningful; identity passthrough until fitted.

### Analog memory (`analog.py`)
- `AnalogMemory` — append-only FAISS `IndexFlatIP` over L2-normalized scaled fingerprints (inner product = cosine, clamped to [0,1]). `add()` on every resolution = **instant learning**, zero retraining.
- `vote()` weight = `similarity × exp(-age_days / recency_half_life_days)` (default 30d), over `knn_neighbors=25`; abstains (uniform dist) below `min_analog_neighbors=5` or zero total weight.
- Guard: query skips FAISS ids ≥ entries length (torn persist across daemon+cron processes).

### LightGBM classifier (`classifier.py`)
- `OutcomeClassifier` — fixed 4-class multiclass (PUMP/FLAT/DUMP/RUG, `num_class=4` always, so warm-start never breaks on a new class). Warm-start via `init_model` adds `warm_start_rounds=30` trees; full retrain uses `full_retrain_rounds=120`; falls back to full retrain if warm-start fails.
- Sample weights = time decay (`model_half_life_days=30`) × balanced class weights, mean-normalized to average 1 (otherwise LightGBM's absolute hessian floor silently suppressed all splits on old batches).
- Below `min_train_samples=50` it declines to train; `predict_proba` returns `None` when untrained.

### Archetypes (`archetypes.py`)
- `ArchetypeModel.fit` — HDBSCAN (euclidean, `archetype_min_cluster_size=15`) on scaled fingerprints; each cluster named `{dominant_bucket}_archetype_{id}`. Only centroids + sorted training-distance array persist.
- `assign()` returns nearest archetype + `novelty_score` as an empirical percentile [0,1]; flagged "novel" at ≥ `novelty_percentile=90`.

### Rug engine (`rug_engine.py`)
- `RugEngine.assess` sums additive points into a 0-100 score from ten signals: `unsellable` (30), `liquidity_removed` (30), `deployer_blacklisted` (25), `liquidity_unlocked` (20), `mint_authority_active` (20), `dev_wallet_dumping` (20), `freeze_authority_active` (15), `top_holder_concentration` (15), `high_sell_tax` (15), `fake_volume` (10). Thresholds in `RugThresholds` (e.g. LP lock < 50%, top holder > 30%, liquidity −50% from peak or −$1000 event, dev outflow ≥ $1000, sell tax ≥ 20%, volume/holder ≥ $5000).
- Invariant: unknown data never fires a signal. Consumes the existing GoPlus `SecurityProfile`; `unsellable_override` seam for a live sell simulation.

### Ensemble (`ensemble.py`)
- `AdaptiveEnsemble` blends sources `analog`, `lightgbm`, `rug_engine`. `rug_score_to_distribution` maps score→`P(rug)=score/100`, rest uniform. Weight per source = Laplace-smoothed rolling accuracy `(correct+1)/(total+2)` over `accuracy_window=200`, normalized over available sources; unavailable sources are excluded, not zeroed. All-unavailable → uniform + `abstained=True`.
- Also tracks blended-verdict accuracy (`final_accuracy`) — the drift signal. `reset_final_history()` after a drift rebuild, or drift re-fires every cycle.

### Learning loops & drift (`service.py`)
- Instant loop: on first resolution, fingerprint → analog index (skipped if coverage 0), deployer blacklist grows on confirmed rugs (atomic `mark_deployer_counted`), prediction graded once into the ensemble. `_on_rug_upgrade` handles slow rugs (later horizon flips FLAT→RUG): re-inserts a RUG fingerprint, blacklists deployer, re-grades once (`rug_regraded`).
- Periodic loop `retrain_if_due`: triggers on first-train (≥50 resolved), cadence (`retrain_every_n=200`), or drift (`final_accuracy < drift_accuracy_floor=0.40` over ≥ `drift_min_samples=30`). Drift or `scaler_refit_every_n=500` forces a full rebuild: scaler refit + analog index rebuild + full classifier retrain + archetype refit.
- Labeling: return ≥ +50% → PUMP, ≤ −50% → DUMP, else FLAT; `is_rug=True` overrides. `CoinRecord.final_bucket` = RUG if any horizon rugged, else longest-horizon bucket.
- Persistence in `state_dir` (default `learning_state/`): `learning.db` (SQLite, `store.py`), `scaler.joblib`, `index.faiss` + `index_meta.joblib`, `classifier.txt`, `archetypes.joblib`, `ensemble.joblib`, `state.joblib`. **Gotcha:** `ensemble.joblib` is only overwritten by the process that actually graded (`_ensemble_dirty`) — monitor and backtest cron share the dir.

### Metrics and the veto (`metrics.py`)
- `compute_metrics(records)` → directional hit-rate (PUMP/DUMP calls only), rug precision/recall/F1, Brier score, 5-bin confidence calibration, per-archetype accuracy, novelty hit-rate; every metric carries sample size, `None` when unmeasured.
- `veto_gate(metrics, min_accuracy, min_samples)` — the P(rug) veto must EARN authority: rug **precision** ≥ `veto_min_accuracy=0.70` over ≥ `veto_min_samples=10` graded rug calls (TP+FP), else `None`.
- Enforcement lives in `workflow/controller.py::_mind_layer_veto`: requires `veto_enabled=True` (`MEMEINTEL_LEARNING_VETO_ENABLED`, default off), earned authority (cached `veto_metrics_ttl_seconds=1800`), and ensemble `P(rug) ≥ veto_min_p_rug=0.85`; a veto downgrades a HIGH alert, never blocks facts.
- `model_confidence` in verdicts = ensemble confidence × cold-start factor (`resolved/cold_start_samples=100`) × snapshot-depth factor (`len(snaps)/min_snapshots_for_confidence=3`).

### What feeds it and how it's read
- Feeder: `python -m meme_intelligence backtest --refresh` (cron: `15 */6 * * *` via `deploy/install-cron.sh`, flock `/tmp/memeintel-backtest.lock`) measures forward outcomes and calls `resolve_outcome(..., is_rug=(survived is False))` per horizon (`horizons_hours="0.25,1,6,24"`), then `persist()`. The monitor feeds detections/snapshots when `--learn` or `enable_in_monitor` is on.
- Reader: Telegram `/mind` (`alerts/telegram_commands.py::_cmd_mind`) renders memory size, resolved vs graded counts, directional hit rate, rug precision/recall, Brier, per-source ensemble accuracy, classifier readiness, veto state ("ON"/"off" + "authority earned/not earned"), and operator 👍/👎 tallies (advisory only, never a training label). CLI: `python -m meme_intelligence mind` (evaluate / metrics).

---


# Part 10 — Database Schema, Outcomes, Wallet Reputation

### Database: `meme_intelligence/database/storage.py` (`Storage`, SQLite)

- Single sync SQLite connection, `check_same_thread=True` — **use only from the creating thread**; do not wrap in `run_in_executor`. WAL + 30s timeout + `busy_timeout=30000` because the 24/7 monitor and cron jobs share the file. Context-manager (`with Storage(path)`) closes the connection.

**Tables** (all timestamps are aware-UTC isoformat TEXT, so lexical comparison is chronological):
- `tokens` — id, chain, address, symbol, name, first_seen; `UNIQUE(chain,address)`. Master identity table.
- `snapshots` — token_id, created_at, final_score, classification, confidence, coverage, category_scores (JSON), overrides (JSON), source; **migrated columns**: price_usd, liquidity_usd, market_cap, regime, opportunity_rank. Every assessment = a prediction record (Part 24).
- `watchlist` — token_id PK, tier, thesis, added_at, updated_at, last_score, last_classification.
- `journal` — token_id (nullable), created_at, kind (discovery/thesis/decision/outcome/lesson/strategy_change), content.
- `security_facts` — token_id PK, updated_at, facts (JSON). Baseline for contract-change diffs (Part 18 S10).
- `wallet_sightings` — wallet, chain, token_id, side (buy/sell/hold_whale/hold_top10), usd_value, seen_at; migrated: source, percent. **Append-only, no dedup** — dedup upstream or aggregate with DISTINCT.
- `alerts` — token_id, created_at, priority, alert_type, title, reasons (JSON), scores (JSON), score_at_alert, source, outcome (filled later by labeling).
- `outcomes` — snapshot_id, token_id, window_hours, target_at, measured_at, price_usd, price_change_percent, liquidity_usd, survived (1/0/NULL=unknown), source (snapshot/live_fetch); `UNIQUE(snapshot_id, window_hours)`.
- `holdings` — token_id, acquired_at, released_at, active, note. Operator-owned coins (/holding).
- `muted_tokens` — token_id PK, muted_at. Suppresses delivery only; analysis continues.
- `operator_feedback` — token_id, alert_id, verdict CHECK('up'/'down'), created_at. **Advisory only** — never written to `alerts.outcome` or learning labels.

**Migration mechanism**: `_SCHEMA` runs via `executescript` (CREATE IF NOT EXISTS + inline indexes), then `_migrate()` applies `_MIGRATIONS` (per-table ALTER TABLE ADD COLUMN, checked against `PRAGMA table_info`, identifiers guarded by `_safe_identifier` regex). Concurrent "duplicate column" errors are swallowed (two processes starting on a pre-upgrade file). **Indexes on migrated columns live in `_POST_MIGRATION_INDEXES`, executed AFTER `_migrate()`** — putting `idx_sightings_source_pair` (`wallet_sightings(source, wallet, token_id, seen_at)`, covering for the reputation rollup) in `_SCHEMA` crashed startup on old DBs. Other indexes: idx_snapshots_token, idx_sightings_wallet/token, idx_alerts_token/type, idx_outcomes_token, idx_holdings_token, idx_feedback_token.

**Key methods**: `upsert_token`, `find_token(address)` (chainless lookup; exact then case-insensitive for 0x), `table_counts` (/status), `record_snapshot`, `peak_score` (all-time max — feeds decline suppression, fixed the 2026-07-14 "old coins re-pitched" complaint), `score_history`, `get_watchlist` (explicit CASE tier ordering — raw TEXT sort put 'archived' first), `top_opportunities`, `update_watchlist` (atomic UPSERT to avoid monitor/cron race), `archive`, `latest_security_facts`/`record_security_facts`, `record_wallet_sightings` (tuples `(wallet, side, usd_value[, percent])`), `wallet_history`, `wallets_seen_on`, `wallet_sighting_stats` (/wallets progress), `add_journal`/`journal_entries`, `record_alert` (score_at_alert picked via `_SCORE_KEY_PREFERENCE`: master→overall→security→smart_money→community→momentum, explicit None checks so 0.0 counts), `alert_history` (reasons parsed for /why), `set_holding`/`release_holding`/`get_holdings`/`is_holding` (address-match across chains), `mute_token`/`unmute_token`/`is_muted`/`muted_list`, `record_feedback`/`feedback_summary`/`feedback_for_token`, `predictions` (FIRST snapshot per token = MIN(id); `with_price_only=True` default), `snapshots_for_token`, `record_outcome` (INSERT OR IGNORE — re-measuring is a no-op), `outcomes_for_snapshot`, `alerts_with_drift`, `set_alert_outcome`, `alert_performance(min_followups=1)`.

### Backtesting: `meme_intelligence/analytics/backtesting.py`

- `BacktestSettings` defaults (config/settings.py:623): `windows_hours="1,24,168,720"` (1h/24h/7d/30d), `window_tolerance_fraction=0.35`, `success_price_change_percent=50.0`, `failure_price_change_percent=-50.0`, `survival_min_liquidity_usd=1000.0`, `signal_high_score=70.0`, `signal_low_score=50.0`, `alert_useful_drift_points=10.0`, `alert_outcome_min_hours=24.0`, `min_predictions_for_weights=10`.
- `refresh_outcomes(storage, market_service, settings, learning_service)` — async; for each prediction × due window: prefer nearest later snapshot within ±35% of the window (`_nearest_snapshot`), else live fetch (`_live_measurement`; **no tradable pair → price 0.0, liquidity 0.0 — death IS the outcome**). Never measures early; gaps stay unrecorded. Feeds `learning_service.resolve_outcome` best-effort (`is_rug` only when survived is False).
- `evaluate_predictions` → `PredictionVerdict` per prediction with ≥1 measured window. Positive classes (`elite_opportunity`, `strong_candidate`): correct if best window ≥ +50%, incorrect if died or worst ≤ −50%, else undetermined. `avoid` grades inverted. Middle classes → "ungraded".
- `performance_metrics` — accuracy, opportunity detection (winners not rated avoid), false-positive rate, risk detection; buckets by classification/regime/confidence with counts.
- `signal_performance` — per-category high (≥70) vs low (<50) avg best-window change and edge.
- `weight_experiments` — returns None below 10 usable predictions; re-scores under `WEIGHT_VARIANTS` (locked_baseline, security/community/narrative_heavy at 0.28); **report-only** — Part 31 lock; apply via `MEMEINTEL_WEIGHTS_*` env overrides plus `record_strategy_change` (journal kind `strategy_change`).
- `label_alert_outcomes` — fills `alerts.outcome` **permanently** from score drift; skips alerts younger than 24h (maturity gate). Opportunity types `{high_priority_opportunity, strong_candidate, early_opportunity, momentum, smart_money_accumulation}` → "useful" at drift ≥ +10 else "noise"; all other types are risk-side → "correct_warning" at drift ≤ −10 else "noise". Gotcha: `strong_candidate` was once missing from the opportunity set, permanently inverting its labels — keep the set current when adding alert types.
- `render_backtest_report` — the Section 13 text dashboard.

### Wallet reputation: `meme_intelligence/analytics/wallet_reputation.py`

- `DEFAULT_SIGHTING_SOURCE = "goplus_holders"` — the canonical provenance tag; writer (workflow/smart_wallets.py) and reader both import it. Reputation is per-source.
- `compute_wallet_reputations(storage, settings, source=..., min_resolved=3)` → `ReputationReport`. Rejects `min_resolved < 1`. Uses the exact backtest thresholds — no second "winner" definition.
- The join runs entirely in SQL (`Storage._REPUTATION_CTES`): `pair` collapses duplicate sightings to `MIN(seen_at)` per (wallet, token); `agg` computes per-token best/worst change, `died`, `first_measured_at`; `labeled` buckets each pair: **hindsight** (first sighting > first_measured_at — post-pump/restart re-records get zero credit; unparseable seen_at also lands here), **resolved** (hit +50%, hit −50%, or died), else **pending**. `wallet_reputation_rollup` applies `HAVING resolved >= min_resolved` in SQL so thin wallets never reach Python (memory O(reported wallets) — 1GB droplet constraint). `wallet_reputation_totals` supplies honest denominators.
- Each rollup row becomes a `WalletTrackRecord` (win_rate = wins/resolved, rug_avoidance_rate = 1 − deaths/resolved; `early_entry_rate` and `median_position_usd` deliberately None — unmeasured stays unmeasured) scored by the Part 17 `wallet_reputation` formula from `analyzers/wallet_intelligence.py`.
- `render_reputation_report(report, top=20)` — /wallets and CLI text; `_short()` strips non-printables/backticks from GoPlus-sourced wallet strings before display (injection guard).
- Module is read-only: persisting scores and feeding them into live scans are separate future steps.

---


# Part 11 — Complete Configuration Reference (every env var)

### Configuration reference — `meme_intelligence/config/settings.py` + `.env.example`

All settings are frozen dataclasses composed into `Settings`, built by `Settings.from_env()` via `get_settings()` (loads `.env` first; real env vars beat file values; `reset_settings()` clears the cache). Env var pattern: `MEMEINTEL_<GROUP>_<FIELD>`. `_convert` types values from the field default; bad booleans raise `ConfigurationError` (no silent false). All groups validate in `__post_init__` — weight groups must sum to 1.0 exactly; bad values fail at startup, loudly.

### Scoring weights & bands
- `ScoringWeights` (`WEIGHTS_`): foundation/security/community/blockchain/momentum/narrative=0.15 each, timing=0.10. Part 31-locked master score; env override is for backtesting only.
- `SecuritySubWeights` (`SECURITY_WEIGHTS_`): contract 0.25, liquidity 0.20, distribution 0.20, developer 0.20, manipulation 0.15. **Gotcha:** `.env.example` shows stale liquidity=0.25/developer=0.15 — code defaults are authoritative (corrected to match Part 33 §11).
- `CommunitySubWeights` (`COMMUNITY_WEIGHTS_`): engagement/growth/loyalty/creativity/dev_relationship 0.20 each.
- `OnChainSubWeights` (`ONCHAIN_WEIGHTS_`): holder_health 0.20, smart_money 0.20, whale_behavior/developer_activity/volume_quality/token_flow 0.15.
- `FoundationSubWeights` (`FOUNDATION_WEIGHTS_`): meme_strength 0.20, narrative 0.20, brand 0.15, community_quality 0.20, dev_communication 0.15, long_term 0.10.
- `TokenSubWeights` (`TOKEN_WEIGHTS_`): valuation/liquidity 0.20; supply/volume/competition/catalysts 0.15.
- `TradeScoreWeights` (`TRADE_WEIGHTS_`): setup_quality/security 0.20; community/onchain/market_conditions/risk_reward 0.15.
- `RiskSubWeights` (`RISK_WEIGHTS_`): security 0.25, market/token/execution 0.20, community 0.15 (higher = riskier).
- `MomentumSubWeights` (`MOMENTUM_WEIGHTS_`): price/volume/social/onchain 0.25 each.
- `OpportunityWeights` (`OPPORTUNITY_WEIGHTS_`): growth_potential 0.30, momentum 0.25, foundation 0.20, risk 0.15, timing 0.10 — watchlist ranking only, never touches master score.
- `SmartMoneySubWeights` (`SMART_MONEY_WEIGHTS_`), `ViralSubWeights` (`VIRAL_WEIGHTS_`), `NarrativeSubWeights` (`NARRATIVE_WEIGHTS_`): 5 x 0.20 each.
- `ClassificationBands` (`BANDS_`): elite 90, strong_candidate 80, watchlist 70, speculative 60 — must be strictly descending.

### Alerts
- `AlertThresholds` (`ALERTS_`): security 80, community 70, liquidity 70, onchain 75, overall 85, momentum 70; strong_candidate_overall 88 (must be >= overall); strong_candidate_min_liquidity_usd 25000 (depth veto); strong_candidate_min_ai_confidence 40 (AI veto); copycat_veto_enabled true, copycat_liquidity_ratio 10, copycat_min_liquidity_usd 100000; opportunity_min_liquidity_usd/min_market_cap_usd **0.0 = OFF**; opportunity_max_liquidity_usd **50000** and opportunity_max_market_cap_usd **100000** (**ON by default since 2026-07-14**, 0 = OFF); opportunity_max_age_hours **24** (buy-side freshness gate, 0 = OFF); unknown data trips the floor but never a ceiling or the age gate; checklist_sell_tax_max_percent 15, checklist_new_launch_minutes 60 (checklist annotates, doesn't gate — only the rug engine's combined veto suppresses).
- `AlertEngineSettings` (`ALERT_ENGINE_`): cooldown_seconds 900 (same token+type dedupe), score_drop_review_points 15, peak_decline_suppression_points 15 (weak-tier buy alerts muted while ≥15 pts below all-time peak), dead_liquidity_usd 500 (below → one MEDIUM post-mortem + archive), risk_alerts_require_interest true (warnings on never-alerted tokens demoted to LOW).
- `AlertDeliverySettings` (`ALERT_DELIVERY_`): telegram_routes "" / discord_routes "" (`category=dest,...` splitting discoveries/smart_money/security/momentum/reports), external_min_priority "medium", requests_per_minute 20. Sinks activate **only** when secrets exist.

### Pipeline cadence & HTTP
- `ScanIntervals` (`INTERVALS_`): ultra_fast 7, fast 45, research 600, historical 86400 (seconds).
- `HttpSettings` (`HTTP_`): timeout_seconds 10, retry_attempts 4, retry_base_delay 0.5, retry_max_delay 8, cache_ttl_seconds 30, cache_max_entries 2048.
- `ProviderSettings` (`PROVIDERS_`): base URLs + req/min budgets — dexscreener 240, geckoterminal 25, goplus 20, coingecko 10, helius 120 (rpc `mainnet.helius-rpc.com` + api `api.helius.xyz`), birdeye 20, jupiter 50, pumpfun 30; pumpportal_ws_url `wss://pumpportal.fun/api/data`; pumpfun_base_url `https://frontend-api-v3.pump.fun` (unofficial, can break); failure_threshold 3, cooldown_seconds 60 (circuit breaker).
- `WorkflowSettings` (`WORKFLOW_`): networks "solana", top_candidates 5, watchlist_review_limit 10, risk_on_btc_change_percent 2, risk_off_btc_drop_percent 3, monitor_interval_seconds 45, watchlist_recheck_cycles 10, max_tracked_keys 50000, insufficient_data_retry_enabled true / min_coverage 0.5 / retry_minutes 15 / max_age_minutes 120 (data-gap AVOIDs get a second look; confirmed red-flag AVOIDs never retried).
- `DatabaseSettings` (`DATABASE_`): path `data/meme_intelligence.sqlite3`.

### Analysis thresholds
- `DiscoverySettings` (`DISCOVERY_`): min_liquidity_usd 5000, target 50000; min_volume_24h_usd 1000, target 50000; max_age_hours 24; target_txns_24h 200.
- `SecurityThresholds` (`SECURITY_`): max_tax 10 / extreme 25%; min_liquidity_usd 5000 / healthy 50000; min_lp_locked 50 / good 80%; warn/max top_holder 10/20%; warn/max top10 50/70%; min_holder_count 50; warn/max creator 5/10%; max/extreme round_trip_loss 50/90%.
- `CommunityThresholds` (`COMMUNITY_`): excellent_engagement 5%, fake_engagement 0.5%, min_followers_for_fake_check 10000, bot_follower warn/artificial 30/50%, duplicate_message_warn 20%, telegram_active_target 15%, target_growth_7d 30%, target_dev_updates/week 3, target_user_content/day 20.
- `OnChainThresholds` (`ONCHAIN_`): min/target holder_count 50/2000, holder_growth_target_24h 20%, healthy/wash trades_per_trader 3/10, volume_per_holder healthy/suspicious 500/5000 USD, buy_ratio weak/strong 0.35/0.60.
- `TokenThresholds` (`TOKEN_`): early/mature mcap 1M/100M; fdv_dilution warn/severe 1.5/3.0; low/healthy liq-to-mcap 1/5%; volume-to-mcap min/target/excessive 1/20/500%; min/healthy circulating_fraction 0.3/0.9.
- `MomentumThresholds` (`MOMENTUM_`): target_trend_24h 30%, spike_1h 30%, late_extension_24h 100%, flat_trend_band 2% (all-flat = no trend, stops sideways re-alerts), volume_acceleration/fade 1.5/0.5, buy_ratio_shift 0.05, target_social_growth_7d 30%.
- `NarrativeThresholds` (`NARRATIVE_`): positive/negative_sentiment 60/40%.
- `TradingSettings` (`TRADING_`): high/medium conviction min score 80/65, high_conviction_min_security 75, min_confirmation_coverage 0.5, max position % high/medium/speculative 5/2/0.5 (guidance only, never executes).
- `RiskSettings` (`RISK_`): max_open_positions 10, max single/chain/narrative/total exposure 10/50/40/80%, reduced/defensive daily loss 5/10%, weekly 10/20%.
- `WalletIntelSettings` (`WALLET_`): whale_min 1%, risk_whale 5%, top_holders_limit 20, recent_trades_limit 50, target_accumulating_wallets 10, artificial_same_size_fraction 0.30, dominant_buyer_volume_fraction 0.60, min_buy_volume_for_dominance_usd 500, **enable_in_monitor false**.
- `LiquidityProbeSettings` (`LIQUIDITY_PROBE_`): enabled **true**, probe_sol_amount 0.3 (~$50; retune as SOL moves), slippage_bps 500, sell_confirm_fraction 0.05. Needs `MEMEINTEL_JUPITER_API_KEY` (keyless tier deprecated).
- `RugThresholds` (`RUG_THRESHOLDS_`): min_lp_locked 50%, top_holder_max 30%, top10_max 70%, liquidity_drop 50%, liquidity_removal_usd 1000, sell_tax_max 20%, dev_dump_usd 1000, fake_volume_per_holder 5000 / min_volume 1000 USD.
- `RugSignalWeights` (`RUG_SIGNAL_WEIGHTS_`): additive points (not normalized): liquidity_removed/unsellable 30, deployer_blacklisted 25, liquidity_unlocked/mint_authority_active/dev_wallet_dumping 20, freeze_authority_active/top_holder_concentration/high_sell_tax 15, fake_volume 10.

### Opt-in subsystems (OFF by default)
- `PumpFunSettings` (`PUMPFUN_`): **enable_in_monitor false**; launchpads "pump", max_creator_buy_percent 20, max_pending 500, pending_ttl_hours 24, ready_ttl_hours 72, recheck_interval_seconds 120, max_rechecks_per_cycle 8, min_market_cap_growth_ratio 1.5, min_usd_market_cap 10000, min_reply_count 5, max_last_trade_age_minutes 30.
- `SmartWalletSettings` (`SMART_WALLET_`): **enabled false**; max_holders_per_token 10, max_seen_keys 5000, min_resolved_for_reputation 3 (recording only, zero new API calls).
- `AISettings` (`AI_`): model "claude-opus-4-8", max_tokens 4096, effort "high", requests_per_minute 10, timeout_seconds 120, min_confidence 20, **enable_in_monitor false**, verify_opportunities **true**, verify_skip_rug_score 10 (any fired rug signal blocks the paid call). Layer only activates when `anthropic_api_key` is set.
- `BacktestSettings` (`BACKTEST_`): windows_hours "1,24,168,720", window_tolerance_fraction 0.35, success/failure_price_change 50/-50%, survival_min_liquidity_usd 1000, signal high/low 70/50, alert_useful_drift_points 10, alert_outcome_min_hours 24, min_predictions_for_weights 10.
- `LearningSettings` (`LEARNING_`): **enabled false, enable_in_monitor false**; pump/dump 50/-50%, horizons "0.25,1,6,24", knn_neighbors 25, recency/model half-life 30d, min_analog_neighbors 5, archetype_min_cluster_size 15 (≥2 required), novelty_percentile 90, retrain_every_n 200, min_train_samples 50, accuracy_window 200, min_ensemble_confidence 0.0, drift_accuracy_floor 0.40, drift_min_samples 30, scaler_refit_every_n 500, fast_snapshot_seconds 60, fast_window_minutes 60, slow_snapshot_minutes 60, capture_until_hours 24, min_snapshots_for_confidence 3, cold_start_samples 100, state_dir "learning_state"; **veto_enabled false** (keep off until /mind shows "authority: EARNED"), veto_min_p_rug 0.85, veto_min_accuracy 0.70, veto_min_samples 10, veto_metrics_ttl_seconds 1800.
- `LightGBMSettings` (`LIGHTGBM_`): full_retrain_rounds 120, warm_start_rounds 30, learning_rate 0.05, num_leaves 31, min_child_samples 5, balanced_class_weights true.
- `TelegramCommandSettings` (`TELEGRAM_COMMANDS_`): **enabled false**; poll_timeout_seconds 25 (must be 1–50), idle_delay_seconds 2, error_backoff_max_seconds 60. **Only one getUpdates consumer per bot token** — this listener is it.
- `ExecutionSettings` (`EXECUTION_`): **buy_button_enabled false, live_enabled false** (both off = dry-run; empty key also forces dry-run); max_buy_sol 0.15, slippage_bps 500, priority_fee_max_lamports 1000000, confirm_timeout_seconds 45, buy_presets_sol "0.05,0.1" (each must be ≤ max_buy_sol).

### Secrets & logging (flat fields on `Settings`, droplet-overridden)
- `MEMEINTEL_LOG_LEVEL`=INFO, `MEMEINTEL_LOG_DIR`=logs.
- All secrets default "" (empty = feature off): `MEMEINTEL_HELIUS_API_KEY`, `MEMEINTEL_BIRDEYE_API_KEY`, `MEMEINTEL_JUPITER_API_KEY`, `MEMEINTEL_ANTHROPIC_API_KEY`, `MEMEINTEL_COINGECKO_API_KEY` (optional demo key), `MEMEINTEL_TELEGRAM_BOT_TOKEN` + `MEMEINTEL_TELEGRAM_CHAT_ID` (Telegram sink + commands), `MEMEINTEL_DISCORD_WEBHOOK_URL`, `MEMEINTEL_EXECUTION_PRIVATE_KEY` (dedicated low-balance trading wallet — settings field `trading_private_key`), `MEMEINTEL_EXECUTION_HELIUS_API_KEY` (second Helius account for trading RPC; empty = shares main key). The live droplet certainly sets the Telegram token/chat_id and Helius/Jupiter keys; check its `.env` — these never appear in git (Rule 16). Note the env names for the last two use `EXECUTION_` but the fields are `trading_private_key`/`trading_helius_api_key` — they are read explicitly in `from_env`, not via `_load_group`.

---


# Part 12 — Tests, CLI Reference, Maintainer Invariants

### Running the test suite

- Command: `python -m pytest tests/ -q` from the repo root. `pytest.ini` sets `asyncio_mode = auto` (async `def test_*` need no decorator), `testpaths = tests`, and `addopts = -q`.
- Gotcha: because `addopts` already contains `-q`, passing `-q` again gives `-qq`, which suppresses the final `N passed` summary line entirely (pytest 9.x). Use `python -m pytest tests/ -o addopts="" -q` when you need the count.
- 57 test modules in `tests/` (a plain package with `__init__.py`). With all deps installed the suite is ~869 tests; on a deps-limited box (no numpy/solders/anthropic/joblib), skipping the 6 numpy-blocked modules gives **764 passed, 26 failed in ~2.4s** — every failure is a `ModuleNotFoundError`, not a code bug.

### Failures in a deps-limited environment

Optional deps (see `requirements.txt` comments — the scanner runs without them by design):

- **numpy** — 6 modules fail at *collection* (they import `meme_intelligence/learning/service.py`, which does `import numpy` at line 34, or import numpy directly): `test_learning_analog.py`, `test_learning_archetypes.py`, `test_learning_classifier.py`, `test_learning_features.py`, `test_learning_integration.py`, `test_learning_service.py` (79 tests). Collection errors abort the whole run, so exclude them with `--ignore=` flags to run the rest.
- **numpy also breaks 3 tests in `test_controller.py`** (they build a learning service at runtime): `test_ai_spend_vetoed_for_blacklisted_deployer`, `test_rug_screen_vetoes_high_alert_even_without_ai`, `test_rug_screen_now_covers_momentum_and_medium_alerts_too`.
- **solders** — 19 failures in `test_execution.py` (live Solana trade signing).
- **anthropic** — 1 failure: `test_ai_reasoning.py::test_service_builder_requires_key` (`meme_intelligence/ai/reasoning.py:463` imports `AsyncAnthropic`).
- **joblib** — 3 failures in `test_learning_ensemble.py`: `test_save_load_roundtrip`, `test_final_history_survives_save_load`, `test_load_pre_drift_monitor_artifact` (ensemble `save()/load()` at `meme_intelligence/learning/ensemble.py:211`).

`pip install numpy scikit-learn faiss-cpu lightgbm hdbscan joblib solders anthropic` makes everything green (Rule 14 baseline).

### Test conventions

- **No network, ever.** Collectors and services are replaced by hand-rolled fakes defined inside the test modules — 14 files define `class Fake*` (`FakeGoPlus`, `FakeGecko`, `FakeMarket`, `FakeJupiter`, `FakeProvider`, `FakeCommunityClient`, `FakeClock`, `FakeStream`, `FakeRpc`, `FakeStorage`, `FakeMind`, ...). HTTP-level behavior is stubbed with `monkeypatch.setattr` (e.g. `test_alert_delivery.py` patches `sink._get_json`).
- **In-memory database:** tests open `Storage(":memory:", now_func=lambda: NOW)` as a context manager with a frozen clock; `test_storage.py` also exercises a real `tmp_path` file for reopen/migration behavior.
- **Deterministic time:** a module-level `NOW` constant plus `now_func`/`FakeClock` injection instead of `datetime.now()`.
- Async tests are plain `async def` functions (auto mode) — do not add `@pytest.mark.asyncio`.

### CLI entry points — `python -m meme_intelligence <command>`

All 17 subcommands live in `meme_intelligence/__main__.py` (`main()` → `_run()` dispatch table):

- `search <query>` — DexScreener pair search by name/symbol/address (`--limit`, default 5).
- `token <address>` — fetch trading pairs for a contract (`--chain`, `--limit`).
- `discover` — Layer 1 scan of newly launched pools (`--network` repeatable, default solana; `--show-rejected`).
- `security <address> --chain <chain>` — GoPlus security assessment for one token; exit code 2 if destructive risk found.
- `scan` — discovery → security screen → on-chain intel for top candidates (`--top`, default 5).
- `plan <address>` — full research pass + trade plan (Part 8); `--ai` needs `MEMEINTEL_ANTHROPIC_API_KEY`, `--ai-mode`, `--regime {bull,neutral,bear,unknown}`.
- `report <address>` — canonical intelligence report (Part 12), persisted via `Storage.record_snapshot` (source `report_cli`).
- `quick <address>` — Level 1 fast card (Part 16); red flags are never skipped for speed.
- `compare <addr> <addr> ...` — side-by-side table + ranking; per-token chain via `chain:address` prefix.
- `watchlist` — tracked tokens; `--refresh` re-scores, `--top` ranks by opportunity score, `--include-archived`.
- `alerts` — alert history + score-drift performance; `--test` sends a synthetic alert through every configured sink.
- `backtest` — prediction-accuracy report (Part 24); `--refresh` measures due outcome windows against live prices.
- `wallets <address>` — smart-money/whale intel (Part 17); requires `MEMEINTEL_HELIUS_API_KEY` and/or `MEMEINTEL_BIRDEYE_API_KEY`.
- `reputation` — wallet reputation from recorded sightings × outcomes; pure local SQLite read, no keys (`--min-resolved` must be ≥1, `--top` default 20).
- `daily` — full daily research routine (Part 11).
- `monitor` — the 24/7 continuous scanner (Part 13), the production entry point; flags: `--cycles`, `--interval`, `--regime`, `--pumpfun` (PumpPortal WebSocket), `--learn` (mind layer), `--smart-wallets`. Ctrl-C stops gracefully.
- `mind evaluate <address>` / `mind metrics` — self-learning mind layer verdict/metrics; exit code 2 when rug-risk ≥ `_MIND_DESTRUCTIVE_RUG_SCORE` (70). Learning imports are lazy so all other commands stay dependency-light.

### Maintainer invariants

- Exit-code convention: 0 ok, 1 no data/config error, 2 destructive risk (security, plan, report, quick, mind evaluate).
- `monitor` shares one Helius `RateLimiter` between wallet-intel and the trading RPC client — do not give them independent limiters (documented 429 bug).
- A broken live-trading layer (`build_executor`) must degrade to `DryRunExecutor`, never crash the loop (Rule 7).
- Metered layers (wallet, AI, learning, pump.fun) join `monitor` only when their `enable_in_monitor` flag AND keys exist; a set flag with missing keys prints a note rather than failing silently.

---


---

# Part 13 — Known Limitations and Honest Caveats

- **Directional edge is thin.** Hit rate 0.54 on n=555 is probably real but
  small. Do not oversell the bot's picking ability, to the operator or in
  alert copy.
- **Community/social intelligence is mostly unbuilt.** The analyzers exist
  but no affordable data source does (researched twice: nothing viable under
  $20/mo; Twitter API and PumpPortal trade streams are both metered).
- **Wallet intelligence (Helius) is OFF in the monitor** — it exhausted the
  free Helius credits and produced 429 noise. Deliberate operator decision;
  do not turn it back on without a paid plan + credit gating.
- **The smart-wallet reputation scores will stay empty for weeks** until the
  data clock and the outcome cron have overlapping history. This is honest
  behavior, not a bug — /wallets says so explicitly.
- **The watchlist has no staleness door yet** (see Part 14). Until it ships,
  mediocre "undead" coins linger in the recheck rotation indefinitely.
- **Test environment:** the dev container lacks numpy/solders/anthropic/
  joblib; the resulting failures are environmental (exact list in Part 12).
  On the droplet with full deps the suite is green.

# Part 14 — The Agreed Next Step (approved, NOT yet built)

**The watchlist staleness door.** Operator's own theory, confirmed in code:
the watchlist's only exits are death (<$500 liquidity), falling to Avoid, or
pairs vanishing — a mediocre coin lingers forever and can re-alert every time
its numbers wobble. Agreed design, ready to build on request:
- Archive ANY coin after N days on the watchlist (default 3, env-configurable).
- Operator holdings (`/holding`) are exempt — never auto-pruned.
- Archive, never delete: all history stays (learning + reputation unaffected);
  a truly revived coin can re-enter through fresh discovery.
- ON by default (it exists to fix an active, twice-hit complaint).
- Simple age cap only — no clever conditions (Rule 21).

# Part 15 — Operator Safety Rules (permanent)

1. Wallet seed phrases / private keys go ONLY into the droplet `.env` over
   SSH. Never in chat, never in git (Rule 16).
2. The bot NEVER auto-trades. Every trade is an explicit operator button.
   The dedicated trading wallet stays small (~$20), never the main wallet.
3. Alerts must never be noisy: MEDIUM-and-above to the phone, cooldowns on,
   vetoes suppress rather than downgrade. When in doubt, quieter.
4. A boost (paid promotion) is never a buy signal, and no unscreened
   high-frequency signal may share the operator's single alert chat again.
5. When data is missing, say "no data" — never fabricate (Rule 8).
