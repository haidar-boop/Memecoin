"""Central configuration for the meme coin intelligence system.

All tunable values live here and can be overridden with environment
variables (Rule 17 — no hardcoding). The naming convention is::

    MEMEINTEL_<GROUP>_<FIELD>

e.g. ``MEMEINTEL_WEIGHTS_SECURITY=0.20`` or ``MEMEINTEL_INTERVALS_ULTRA_FAST=5``.

Defaults follow the specification:

* Scoring weights   -- Part 31 "Framework Consistency Lock", Section 4
* Security weights  -- Part 33, Section 10
* Score bands       -- Parts 10 / 12 / 20 (90 / 80 / 70 / 60)
* Alert thresholds  -- Part 2, Section 4 (Layer 4 human-review gates)
* Scan intervals    -- Part 21, Section 4 (four scanning speeds)

See ``.env.example`` at the repository root for the full list of variables.
"""

from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from meme_intelligence.core.errors import ConfigurationError

_ENV_PREFIX = "MEMEINTEL"
_WEIGHT_SUM_TOLERANCE = 1e-6


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not (low <= value <= high):
        raise ConfigurationError(f"{name} must be between {low} and {high}, got {value}")


def _check_weight_sum(group: str, values: Mapping[str, float]) -> None:
    total = sum(values.values())
    # A NaN weight makes `abs(nan - 1.0) > tolerance` False (NaN comparisons
    # are always False), silently passing an invalid config — check
    # finiteness explicitly rather than relying on the comparison to catch it.
    if not math.isfinite(total) or abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        detail = ", ".join(f"{k}={v}" for k, v in values.items())
        raise ConfigurationError(f"{group} weights must sum to 1.0, got {total} ({detail})")


@dataclass(frozen=True)
class ScoringWeights:
    """Top-level category weights for the final intelligence score.

    Locked by Part 31 (Framework Consistency Lock, Section 4). Changing these
    via env vars is allowed for backtesting experiments (Part 24, Section 5),
    but the shipped defaults are the canonical framework.
    """

    foundation: float = 0.15
    security: float = 0.15
    community: float = 0.15
    blockchain: float = 0.15
    momentum: float = 0.15
    narrative: float = 0.15
    timing: float = 0.10

    def __post_init__(self) -> None:
        _check_weight_sum("scoring", dataclasses.asdict(self))
        for name, value in dataclasses.asdict(self).items():
            _check_range(f"scoring weight '{name}'", value, 0.0, 1.0)


@dataclass(frozen=True)
class SecuritySubWeights:
    """Sub-weights inside the security score (Part 33, Section 11).

    Match Part 33 Section 11's literal rug-risk weighting exactly (Rule 1 —
    the spec is the source of truth): Contract Safety /25, Liquidity Safety
    /20, Developer Safety /20, Distribution Safety /20, Social Authenticity
    (manipulation) /15. An earlier build shipped liquidity 0.25 / developer
    0.15, which matched neither the spec nor its own handoff note; corrected
    here during the Parts 20-33 verification pass.
    """

    contract: float = 0.25
    liquidity: float = 0.20
    distribution: float = 0.20
    developer: float = 0.20
    manipulation: float = 0.15

    def __post_init__(self) -> None:
        _check_weight_sum("security", dataclasses.asdict(self))


@dataclass(frozen=True)
class CommunitySubWeights:
    """Sub-weights inside the community score (Part 5, Section 11 — 5 x 20)."""

    engagement: float = 0.20
    growth: float = 0.20
    loyalty: float = 0.20
    creativity: float = 0.20
    dev_relationship: float = 0.20

    def __post_init__(self) -> None:
        _check_weight_sum("community", dataclasses.asdict(self))


@dataclass(frozen=True)
class OnChainSubWeights:
    """Sub-weights inside the on-chain score (Part 6, Section 15)."""

    holder_health: float = 0.20
    smart_money: float = 0.20
    whale_behavior: float = 0.15
    developer_activity: float = 0.15
    volume_quality: float = 0.15
    token_flow: float = 0.15

    def __post_init__(self) -> None:
        _check_weight_sum("on-chain", dataclasses.asdict(self))


@dataclass(frozen=True)
class FoundationSubWeights:
    """Sub-weights inside the foundation score (Part 5, Section 12)."""

    meme_strength: float = 0.20
    narrative: float = 0.20
    brand: float = 0.15
    community_quality: float = 0.20
    dev_communication: float = 0.15
    long_term: float = 0.10

    def __post_init__(self) -> None:
        _check_weight_sum("foundation", dataclasses.asdict(self))


@dataclass(frozen=True)
class ClassificationBands:
    """Minimum final score for each classification band (Parts 10/12/20)."""

    elite: float = 90.0
    strong_candidate: float = 80.0
    watchlist: float = 70.0
    speculative: float = 60.0

    def __post_init__(self) -> None:
        bands = dataclasses.asdict(self)
        for name, value in bands.items():
            _check_range(f"classification band '{name}'", value, 0.0, 100.0)
        ordered = [self.elite, self.strong_candidate, self.watchlist, self.speculative]
        if ordered != sorted(ordered, reverse=True) or len(set(ordered)) != len(ordered):
            raise ConfigurationError(f"classification bands must be strictly descending, got {bands}")


@dataclass(frozen=True)
class AlertThresholds:
    """Minimum category scores before a human-review alert fires (Part 2, Section 4)."""

    security: float = 80.0
    community: float = 70.0
    liquidity: float = 70.0
    onchain: float = 75.0
    overall: float = 85.0
    momentum: float = 70.0  # momentum score gate for momentum alerts (Part 15, Section 5)
    # A fresh launch (pump.fun-era) can pass security/on-chain/liquidity
    # with data but has no CoinGecko community data for days — so it never
    # hits the "every gate verified" HIGH tier. When such a token clears
    # THIS raised overall bar with every measurable gate passing, it earns
    # a HIGH "strong candidate" alert (community explicitly unverified),
    # so a genuinely strong launch still reaches the operator (Part 2 S4;
    # Rule 8 — the missing gate is named, never assumed passed).
    strong_candidate_overall: float = 88.0
    # Depth veto: the "liquidity" gate scores lock SAFETY, not pool depth, so
    # a $16k pool could clear every gate and fire a HIGH alert while being
    # trivially manipulable. Below this absolute USD depth (or with unknown
    # liquidity — Rule 8), a strong candidate downgrades to MEDIUM.
    strong_candidate_min_liquidity_usd: float = 25000.0
    # AI veto: when an AI verification ran, a lukewarm judgment (confidence
    # below this) downgrades the alert instead of riding along as a footnote.
    # No AI configured -> no veto (Rule 9 — degrade gracefully).
    strong_candidate_min_ai_confidence: float = 40.0
    # Copycat veto (free screen before any HIGH opportunity alert): a fresh
    # token whose symbol/name duplicates an ESTABLISHED token — one with at
    # least ``copycat_min_liquidity_usd`` of liquidity AND at least
    # ``copycat_liquidity_ratio`` times the candidate's — is likely a
    # knock-off farming that name, and the alert downgrades to MEDIUM with
    # the duplicate named. Two small coins sharing a symbol never fire this
    # (symbols collide constantly); only a large size gap is evidence.
    copycat_veto_enabled: bool = True
    copycat_liquidity_ratio: float = 10.0
    copycat_min_liquidity_usd: float = 100000.0
    # COMFORT floors for BUY-SIDE alerts — these ANNOTATE, they do not
    # suppress. A thin-but-real pool still reaches the phone carrying a ⚠
    # checklist line naming the miss, per the operator's 2026-07-12 rule: "if
    # just one thing misses the checklist, send it through and let me know."
    #
    # (This comment previously claimed these SUPPRESS the alert. That was
    # already untrue when the 2026-07-12 annotate-only rule landed and was
    # never corrected, so an operator setting a floor here got no blocking at
    # all — corrected 2026-07-29. To actually block, use the hard floors
    # below.)
    opportunity_min_liquidity_usd: float = 0.0
    opportunity_min_market_cap_usd: float = 0.0
    # HARD floors for BUY-SIDE alerts (opportunity / momentum / smart-money):
    # below these the coin is not a tradeable opportunity at all and the alert
    # is SUPPRESSED, exactly like a $0/unknown pool. Distinct from the comfort
    # floors above so the "send it and tell me" rule keeps applying to coins
    # that are merely thin, while genuine micro-junk never reaches the phone
    # (operator: "it's sending me some bullshit coins, make it stricter",
    # 2026-07-29).
    #
    # Protective warnings (death / risk / whale-exit / insider) are never
    # floored: a dying coin's holder still needs to know. Both default 0.0 =
    # OFF, so nothing changes until the operator sets them (Rule 18).
    hard_min_liquidity_usd: float = 0.0
    hard_min_market_cap_usd: float = 0.0
    # Minimum EVIDENCE COVERAGE for a buy-side alert (0.0-1.0; 0.0 = OFF).
    #
    # The framework score is renormalized over whatever categories have data,
    # so a coin nobody can measure scores on the strength of the one or two
    # categories that DID resolve. Measured on the real pipeline 2026-07-29:
    # stripping every volume/trade field off a healthy coin RAISED its score
    # from 92.9 (70% coverage) to 93.3 (55% coverage). The less the bot knows
    # about a coin, the better it looks — and brand-new junk is what it knows
    # least about.
    #
    # The asymmetry this fixes: `workflow.insufficient_data_min_coverage`
    # (0.5) already says that below half coverage an AVOID is "a data gap, not
    # a verdict" and must not be trusted. The same evidence was still trusted
    # to conclude ELITE OPPORTUNITY. Refusing to conclude "bad" while happily
    # concluding "great" from the same thin data is the bug (Rule 8).
    #
    # Reference points, since coverage is abstract: all 7 categories = 1.00;
    # missing community only = 0.85; missing community + foundation = 0.70 (a
    # normal fresh launch — those two need CoinGecko/social data that does not
    # exist yet for a new coin); missing narrative as well = 0.55.
    min_coverage: float = 0.0
    # Operator "don't send me coins that already ran" CEILING for BUY-SIDE
    # alerts. ABOVE these, the coin is no longer an early opportunity — the move
    # the operator wants to catch already happened (a multi-million-dollar pool
    # firing an "early opportunity" is exactly the noise this cuts) — so the
    # buy-side alert is SUPPRESSED. Protective warnings still fire (a large coin
    # can still rug). Unknown liquidity/mcap NEVER trips the ceiling (Rule 8 —
    # absent data is not evidence a coin is too big; that is the floor's job).
    # Both default 0.0 = OFF, so existing behavior is unchanged until set (Rule 18).
    opportunity_max_liquidity_usd: float = 0.0
    opportunity_max_market_cap_usd: float = 0.0
    # Operator freshness gate for BUY-SIDE alerts (2026-07-17, post-restore:
    # "Make it so it only sends me coins less than 1 hour old"). A pool OLDER
    # than this many hours is past the entry window the operator trades, so
    # its opportunity/momentum/smart-money alerts are SUPPRESSED. Protective
    # warnings still fire (an old coin the operator holds can still rug), and
    # an UNKNOWN pool age never trips the gate (Rule 8 — absent data is not
    # evidence of age; same convention as the size ceiling above). ON by
    # default at 1 hour per the operator's explicit request; 0 = OFF.
    # (Was briefly widened to 3h on 2026-07-17, reverted with that day's
    # work on 2026-07-18 — the operator kept the 1h window when the wallet
    # credit gate was restored as a dormant, off-by-default feature on
    # 2026-07-20.)
    opportunity_max_age_hours: float = 1.0
    # Safety checklist (operator rule 2026-07-12): a buy-side alert now SENDS
    # even when a soft check falls short — the checklist rides ON the alert so
    # the operator sees what missed and decides. Only the rug engine's COMBINED
    # veto suppresses ("if it's a rug pull don't send it at all"); a single soft
    # flag never does. The liquidity/market-cap floors above became checklist
    # comfort lines (annotate), not gates. ``checklist_sell_tax_max_percent`` is
    # the sell-tax ceiling a coin passes under; above it the line reads ⚠ but
    # the alert still sends. ``checklist_new_launch_minutes`` is how young a pool
    # can be for the top-wallet-concentration line to read "normal for a new
    # launch" rather than a standalone concern (a fresh launch is naturally
    # concentrated — Rule 8, never punish a coin merely for being new).
    checklist_sell_tax_max_percent: float = 15.0
    checklist_new_launch_minutes: float = 60.0
    # Security floor for MOMENTUM buy-side alerts (2026-07-17 review finding):
    # momentum was the one buy-side type with no security bar — the
    # opportunity tiers require security >= 80, but momentum fired for any
    # non-destructive coin, letting a 40-49-scoring coin (all soft flags, no
    # rug signal) ride bot-painted volume to the phone unscreened. Aligned
    # with the wallet credit gate's floor so "below the floor never reaches
    # the phone as a buy signal" is actually true. 0 = off (old behavior).
    # DEFAULT 0 (2026-07-20): the floor is part of the dormant wallet-
    # tracking kit — the operator asked for it to be built but change
    # NOTHING until he enables it, so the README enable steps set this to
    # 50 together with the monitor flag (keeping the two floors aligned).
    momentum_min_security_score: float = 0.0
    # /check card DUMPED banner (operator request 2026-07-20): a coin whose
    # price collapsed this many percent in 24h shows "STATUS: DUMPED" on the
    # /check card even while its pool still holds liquidity — the operator's
    # "dead" is the trader's (price cratered), not only the drained-pool death
    # the alert engine tracks. DISPLAY-ONLY: read exclusively by the Telegram
    # /check card, never by the alert pipeline. 0 = off. Unknown 24h change
    # never triggers it (Rule 8).
    check_dumped_drop_percent: float = 80.0

    def __post_init__(self) -> None:
        for name in ("security", "community", "liquidity", "onchain", "overall",
                     "momentum", "strong_candidate_overall",
                     "strong_candidate_min_ai_confidence"):
            _check_range(f"alert threshold '{name}'", getattr(self, name), 0.0, 100.0)
        if self.strong_candidate_overall < self.overall:
            raise ConfigurationError(
                "strong_candidate_overall must be >= overall "
                f"({self.strong_candidate_overall} < {self.overall})")
        for name in ("strong_candidate_min_liquidity_usd",
                     "copycat_liquidity_ratio", "copycat_min_liquidity_usd"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(
                    f"alert threshold '{name}' must be positive, got {value}")
        for name in ("opportunity_min_liquidity_usd", "opportunity_min_market_cap_usd",
                     "opportunity_max_liquidity_usd", "opportunity_max_market_cap_usd",
                     "hard_min_liquidity_usd", "hard_min_market_cap_usd",
                     "opportunity_max_age_hours", "checklist_new_launch_minutes"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError(
                    f"alert threshold '{name}' must be >= 0, got {value}")
        # A ceiling must sit above the comfort floor when both are set (>0),
        # else the "too big" cut would swallow the "too thin" note.
        for floor, cap in (("opportunity_min_liquidity_usd", "opportunity_max_liquidity_usd"),
                           ("opportunity_min_market_cap_usd", "opportunity_max_market_cap_usd"),
                           ("hard_min_liquidity_usd", "opportunity_max_liquidity_usd"),
                           ("hard_min_market_cap_usd", "opportunity_max_market_cap_usd")):
            lo, hi = getattr(self, floor), getattr(self, cap)
            if hi > 0.0 and lo > 0.0 and hi < lo:
                raise ConfigurationError(
                    f"alert threshold '{cap}' ({hi}) must be >= '{floor}' ({lo})")
        _check_range("alert threshold 'min_coverage'", self.min_coverage, 0.0, 1.0)
        _check_range("alert threshold 'checklist_sell_tax_max_percent'",
                     self.checklist_sell_tax_max_percent, 0.0, 100.0)
        _check_range("alert threshold 'check_dumped_drop_percent'",
                     self.check_dumped_drop_percent, 0.0, 100.0)
        _check_range("alert threshold 'momentum_min_security_score'",
                     self.momentum_min_security_score, 0.0, 100.0)


@dataclass(frozen=True)
class ScanIntervals:
    """Refresh cadence in seconds for each scanning speed (Part 21, Section 4)."""

    ultra_fast: float = 7.0     # price, liquidity, large transactions, new pairs
    fast: float = 45.0          # holder growth, volume changes, wallet activity
    research: float = 600.0     # community, narrative, security, competition
    historical: float = 86400.0  # performance, prediction accuracy

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"scan interval '{name}' must be positive, got {value}")


@dataclass(frozen=True)
class HttpSettings:
    """Shared HTTP client behavior for all collectors (Rules 6, 7, 11)."""

    timeout_seconds: float = 10.0
    retry_attempts: int = 4
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0
    cache_ttl_seconds: float = 30.0
    cache_max_entries: int = 2048

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigurationError(f"timeout_seconds must be positive, got {self.timeout_seconds}")
        if self.retry_attempts < 1:
            raise ConfigurationError(f"retry_attempts must be >= 1, got {self.retry_attempts}")
        # Previously unvalidated: a bad cache_ttl_seconds/cache_max_entries
        # value reached TTLCache's constructor and raised a raw ValueError
        # far from the setting that caused it; negative retry delays
        # silently disabled backoff instead of erroring (Rule 6).
        for name in ("retry_base_delay", "retry_max_delay", "cache_ttl_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"http setting '{name}' must be positive, got {value}")
        if self.cache_max_entries < 1:
            raise ConfigurationError(
                f"cache_max_entries must be >= 1, got {self.cache_max_entries}")
        if self.retry_max_delay < self.retry_base_delay:
            raise ConfigurationError("retry_max_delay must be >= retry_base_delay")


@dataclass(frozen=True)
class ProviderSettings:
    """Per-provider endpoints, rate limits, and failover behavior (Parts 15, 21, 32.5).

    Rate limits are set safely below documented provider limits — the goal is
    stable operation, not maximum request frequency (Rule 11).
    """

    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_requests_per_minute: float = 240.0    # documented limit: 300/min
    geckoterminal_base_url: str = "https://api.geckoterminal.com"
    geckoterminal_requests_per_minute: float = 25.0   # documented free limit: 30/min
    goplus_base_url: str = "https://api.gopluslabs.io"
    goplus_requests_per_minute: float = 20.0          # conservative free-tier budget
    coingecko_base_url: str = "https://api.coingecko.com"
    coingecko_requests_per_minute: float = 10.0       # documented free limit: ~10-30/min
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    helius_api_url: str = "https://api.helius.xyz"
    helius_requests_per_minute: float = 120.0         # free tier allows ~10 rps; stay far below
    birdeye_base_url: str = "https://public-api.birdeye.so"
    birdeye_requests_per_minute: float = 20.0         # free tier ~1 rps + monthly CU budget
    jupiter_base_url: str = "https://api.jup.ag"
    jupiter_requests_per_minute: float = 50.0         # free tier documented limit: 60/min (1 rps)
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"  # free data WS (Part 32.5 S3)
    pumpfun_base_url: str = "https://frontend-api-v3.pump.fun"  # unofficial; can change
    pumpfun_requests_per_minute: float = 30.0         # no documented limit; stay conservative
    lunarcrush_base_url: str = "https://lunarcrush.com/api4"
    # LunarCrush's real per-plan limits aren't publicly documented; this is a
    # conservative default the operator can raise once he sees his plan's
    # actual quota (Roadmap item 5, 2026-07-20).
    lunarcrush_requests_per_minute: float = 10.0
    failure_threshold: int = 3      # consecutive failures before a provider cools down
    cooldown_seconds: float = 60.0  # how long an unhealthy provider is skipped

    def __post_init__(self) -> None:
        for name in ("dexscreener", "geckoterminal", "goplus", "coingecko",
                     "helius", "birdeye", "jupiter", "pumpfun", "lunarcrush"):
            value = getattr(self, f"{name}_requests_per_minute")
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name}_requests_per_minute must be positive")
        if self.failure_threshold < 1:
            raise ConfigurationError("failure_threshold must be >= 1")
        if not math.isfinite(self.cooldown_seconds) or self.cooldown_seconds <= 0:
            raise ConfigurationError(
                f"cooldown_seconds must be positive, got {self.cooldown_seconds}")


@dataclass(frozen=True)
class DiscoverySettings:
    """Initial discovery filters and score anchors (Part 3, Part 15 Section 6, Part 27).

    ``min_*`` values are hard gates for candidacy; ``target_*`` values are where
    a component earns full marks in the discovery score.
    """

    min_liquidity_usd: float = 5000.0
    target_liquidity_usd: float = 50000.0
    min_volume_24h_usd: float = 1000.0
    target_volume_24h_usd: float = 50000.0
    max_age_hours: float = 24.0     # pools older than this are no longer "new"
    target_txns_24h: int = 200      # transaction count earning full activity marks

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"discovery setting '{name}' must be positive, got {value}")
        if self.target_liquidity_usd < self.min_liquidity_usd:
            raise ConfigurationError("target_liquidity_usd must be >= min_liquidity_usd")
        if self.target_volume_24h_usd < self.min_volume_24h_usd:
            raise ConfigurationError("target_volume_24h_usd must be >= min_volume_24h_usd")


@dataclass(frozen=True)
class PumpFunSettings:
    """Pump.fun early-launch discovery (Part 32.5 Section 3).

    Launch events arrive over the free PumpPortal WebSocket; tracked
    launches are rechecked against the Pump.fun frontend API and promoted
    into the normal analysis pipeline only after meeting the Section 8
    deep-analysis threshold AND being confirmed by an independent market
    data source (Section 2 — discovery is never confirmation).

    "The system must not alert on every new launch. Most launches should
    be filtered out." (Section 3) — the defaults below are deliberately
    strict; loosen only with data showing they drop real opportunities.
    """

    enable_in_monitor: bool = False           # opt-in for the continuous scanner
    launchpads: str = "pump"                  # comma-separated accepted pool ids from the stream
    max_creator_buy_percent: float = 20.0     # basic filter: bigger dev-buy = insider grab
    max_pending: int = 500                    # bounded launch-tracking memory
    pending_ttl_hours: float = 24.0           # drop unpromoted launches after this window
    recheck_interval_seconds: float = 120.0   # per-token traction recheck cadence (Section 5)
    max_rechecks_per_cycle: int = 8           # frontend-API budget per scanner cycle (Rule 11)
    min_market_cap_growth_ratio: float = 1.5  # SOL mcap vs launch mcap = "increasing attention"
    min_usd_market_cap: float = 10000.0       # promotion gate: evidence of real buying
    min_reply_count: int = 5                  # promotion gate: community interest exists
    max_last_trade_age_minutes: float = 30.0  # promotion gate: still actively trading
    # A promoted (READY) candidate whose market confirmation never succeeds is
    # dropped after this window — longer than pending_ttl_hours because market
    # indexing can lag, but bounded so dead bonding-curve tokens can't retry
    # forever, hammer providers, and exhaust max_pending slots (Rules 7/11).
    ready_ttl_hours: float = 72.0

    @property
    def launchpad_list(self) -> list[str]:
        return [pool.strip() for pool in self.launchpads.split(",") if pool.strip()]

    def __post_init__(self) -> None:
        for name in ("max_creator_buy_percent", "max_pending", "pending_ttl_hours",
                     "recheck_interval_seconds", "max_rechecks_per_cycle",
                     "min_market_cap_growth_ratio", "min_usd_market_cap",
                     "min_reply_count", "max_last_trade_age_minutes",
                     "ready_ttl_hours"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"pumpfun setting '{name}' must be positive")
        if self.max_creator_buy_percent > 100.0:
            raise ConfigurationError("max_creator_buy_percent must be within (0, 100]")
        if not self.launchpad_list:
            raise ConfigurationError("pumpfun launchpads must name at least one pool")


@dataclass(frozen=True)
class BoostWatcherSettings:
    """DexScreener boost radar (Project 5): DM the operator the first time any
    token crosses ``threshold`` boosts on DexScreener.

    A boost is PAID promotion, not organic traction or a safety signal — this
    is a "what is being pumped for visibility right now" heads-up, never a buy
    signal (the emitted alert says so). Off by default (Rule 18): enabling it
    is the only thing that changes behavior. Independent of the scan cycle —
    its own poll loop and its own alert, so it never touches discovery,
    analysis, storage, or trading.
    """

    enabled: bool = False              # opt-in; off = no behavior change
    threshold: float = 100.0           # alert when totalAmount crosses this
    poll_interval_seconds: float = 30.0  # matches DexScreener's ~30s edge cache
    chain_filter: str = "solana"       # "" = every chain; "solana" = Solana-only
    max_seen_keys: int = 5000          # bounded already-alerted dedup memory

    def __post_init__(self) -> None:
        for name in ("threshold", "poll_interval_seconds", "max_seen_keys"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(
                    f"boost watcher setting '{name}' must be positive, got {value}")


@dataclass(frozen=True)
class SecurityThresholds:
    """Security-analysis limits (Part 4, Part 18, Part 33).

    Values above the ``warn_*`` level deduct lightly (acceptable uncertainty);
    values above the ``max_*`` level deduct heavily (serious warning).
    """

    max_tax_percent: float = 10.0
    extreme_tax_percent: float = 25.0
    min_liquidity_usd: float = 5000.0
    healthy_liquidity_usd: float = 50000.0
    min_lp_locked_percent: float = 50.0
    good_lp_locked_percent: float = 80.0
    warn_top_holder_percent: float = 10.0
    max_top_holder_percent: float = 20.0
    warn_top10_holder_percent: float = 50.0
    max_top10_holder_percent: float = 70.0
    min_holder_count: int = 50
    warn_creator_percent: float = 5.0
    max_creator_percent: float = 10.0
    max_round_trip_loss_percent: float = 50.0
    extreme_round_trip_loss_percent: float = 90.0

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"security threshold '{name}' must be positive, got {value}")
        if self.extreme_tax_percent < self.max_tax_percent:
            raise ConfigurationError("extreme_tax_percent must be >= max_tax_percent")
        if self.extreme_round_trip_loss_percent < self.max_round_trip_loss_percent:
            raise ConfigurationError(
                "extreme_round_trip_loss_percent must be >= max_round_trip_loss_percent"
            )


@dataclass(frozen=True)
class TokenSubWeights:
    """Sub-weights inside the token structure score (Part 7, Section 13)."""

    valuation: float = 0.20
    liquidity: float = 0.20
    supply: float = 0.15
    volume: float = 0.15
    competition: float = 0.15
    catalysts: float = 0.15

    def __post_init__(self) -> None:
        _check_weight_sum("token", dataclasses.asdict(self))


@dataclass(frozen=True)
class TradeScoreWeights:
    """Sub-weights inside the pre-entry trade score (Part 8, Section 13)."""

    setup_quality: float = 0.20
    security: float = 0.20
    community: float = 0.15
    onchain: float = 0.15
    market_conditions: float = 0.15
    risk_reward: float = 0.15

    def __post_init__(self) -> None:
        _check_weight_sum("trade", dataclasses.asdict(self))


@dataclass(frozen=True)
class TokenThresholds:
    """Token structure anchors (Part 7)."""

    early_stage_mcap_usd: float = 1_000_000.0
    mature_stage_mcap_usd: float = 100_000_000.0
    fdv_dilution_warn_ratio: float = 1.5     # FDV / market cap above this = dilution overhang
    fdv_dilution_severe_ratio: float = 3.0
    low_liquidity_to_mcap_percent: float = 1.0
    healthy_liquidity_to_mcap_percent: float = 5.0
    min_volume_to_mcap_percent: float = 1.0
    target_volume_to_mcap_percent: float = 20.0
    excessive_volume_to_mcap_percent: float = 500.0  # daily churn > 5x mcap = suspicious
    min_circulating_fraction: float = 0.3    # market cap / FDV
    healthy_circulating_fraction: float = 0.9

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"token threshold '{name}' must be positive, got {value}")
        if self.early_stage_mcap_usd >= self.mature_stage_mcap_usd:
            raise ConfigurationError("early_stage_mcap_usd must be below mature_stage_mcap_usd")
        if self.fdv_dilution_warn_ratio >= self.fdv_dilution_severe_ratio:
            raise ConfigurationError("fdv_dilution_warn_ratio must be below fdv_dilution_severe_ratio")
        if not (self.min_volume_to_mcap_percent < self.target_volume_to_mcap_percent
                < self.excessive_volume_to_mcap_percent):
            raise ConfigurationError("volume/mcap thresholds must satisfy min < target < excessive")


@dataclass(frozen=True)
class TradingSettings:
    """Trade-planning discipline settings (Part 8, Part 9 Section 3).

    Position percentages are *guidance ceilings* written into generated
    plans — this system never executes trades (Part 13, Section 8).
    """

    high_conviction_min_score: float = 80.0
    medium_conviction_min_score: float = 65.0
    high_conviction_min_security: float = 75.0
    min_confirmation_coverage: float = 0.5   # below this, conviction caps at SPECULATIVE
    high_conviction_max_position_percent: float = 5.0
    medium_conviction_max_position_percent: float = 2.0
    speculative_max_position_percent: float = 0.5

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"trading setting '{name}' must be positive, got {value}")
        if self.medium_conviction_min_score >= self.high_conviction_min_score:
            raise ConfigurationError("medium_conviction_min_score must be below high_conviction_min_score")
        if not (0 < self.min_confirmation_coverage <= 1):
            raise ConfigurationError("min_confirmation_coverage must be within (0, 1]")


@dataclass(frozen=True)
class AISettings:
    """AI reasoning layer configuration (Part 23; Part 22 Section 11).

    The layer activates only when ``anthropic_api_key`` is set on
    :class:`Settings` (Rule 16 — key from the environment). Rate limiting
    is deliberately conservative (Rule 11), and the layer never runs in
    the continuous scanner unless explicitly enabled (Rule 10 — expensive
    analysis only after filtering).
    """

    model: str = "claude-opus-4-8"
    max_tokens: int = 4096
    effort: str = "high"                 # low | medium | high | xhigh | max
    requests_per_minute: float = 10.0
    timeout_seconds: float = 120.0       # judgments can take a while at high effort
    min_confidence: float = 20.0         # below this the judgment is discarded (Part 23 S6)
    enable_in_monitor: bool = False      # AI judges EVERY analyzed token (expensive)
    # Part 32.5 Section 8 middle mode: one AI judgment only when a token
    # passes ALL review gates (high-priority opportunity), re-scored before
    # the alert dispatches — deep analysis strictly after initial
    # requirements. Gate-passing tokens are rare, so cost stays near zero.
    verify_opportunities: bool = True
    # Credit conservation: a paid verification call is the LAST check, never
    # the first. If the deterministic rug engine scores at/above this before
    # the call, the call is skipped (the alert is downgraded instead). The
    # smallest signal weight is 10, so the default means ANY fired rug signal
    # blocks the spend; raise it to tolerate weak signals.
    verify_skip_rug_score: float = 10.0

    def __post_init__(self) -> None:
        if self.model.strip() == "":
            raise ConfigurationError("ai model must be non-empty")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigurationError(f"ai effort must be a valid level, got {self.effort!r}")
        for name in ("max_tokens", "requests_per_minute", "timeout_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"ai setting '{name}' must be positive")
        _check_range("ai min_confidence", self.min_confidence, 0.0, 100.0)
        _check_range("ai verify_skip_rug_score", self.verify_skip_rug_score, 0.0, 100.0)


@dataclass(frozen=True)
class BacktestSettings:
    """Backtesting & self-improvement thresholds (Part 24).

    A positive prediction (Elite/Strong Candidate) counts as successful when
    the best measured window gains ``success_price_change_percent``; it
    counts as failed at ``failure_price_change_percent`` or token death
    (liquidity under ``survival_min_liquidity_usd``). An Avoid call is
    graded with the same thresholds inverted. Everything in between stays
    honestly "undetermined" (Rule 8 — one week of sideways price proves
    nothing either way).
    """

    windows_hours: str = "1,24,168,720"      # 1h / 24h / 7d / 30d (Part 24 S2)
    window_tolerance_fraction: float = 0.35  # snapshot within +/-35% of the window counts
    success_price_change_percent: float = 50.0
    failure_price_change_percent: float = -50.0
    survival_min_liquidity_usd: float = 1000.0
    signal_high_score: float = 70.0          # "high" bucket for signal analysis (S6)
    signal_low_score: float = 50.0           # below this = "low" bucket
    alert_useful_drift_points: float = 10.0  # score drift that labels an alert useful (S12/29)
    alert_outcome_min_hours: float = 24.0    # alerts younger than this stay unlabeled
    min_predictions_for_weights: int = 10    # weight experiments need a real sample (S1)
    # A forward return is measured against the token's FIRST recorded price.
    # When that baseline (or the later reading) is a bad datum the ratio
    # explodes: live data showed +1.7e11% — a 1.7-BILLION-fold "gain" — and
    # because anything >= pump_return_percent becomes a PUMP label, those
    # fantasies were written into the mind layer's memory as winners
    # (2026-07-29). Past this ceiling a return is UNMEASURABLE, not real
    # (Rule 8 — a nonsense number is not evidence).
    #
    # The ceiling MUST sit above every genuinely reachable run, because the
    # rare monster is the single most valuable thing this system can record
    # — discarding it would delete exactly the evidence the operator is
    # hunting (his correction, 2026-07-29; an earlier 1000x default was far
    # too tight: 1000x happens in this market). Anchoring on real physics
    # from a ~$20k detection (the discovery floor is $15k liquidity):
    #   $1M peak =        50x =         4,900%
    #   $1B peak =    50,000x =     4,999,900%
    #   $10B (DOGE-tier) = 500,000x = 49,999,900%
    # The observed corruption starts at 26,000,000x (2.7e9%). 1,000,000x
    # therefore sits ~20x above a DOGE-tier miracle and ~27x below the
    # smallest impossible value — wide margins on both sides.
    # 0 disables the guard.
    max_measurable_return_percent: float = 100_000_000.0

    def __post_init__(self) -> None:
        windows = [w.strip() for w in self.windows_hours.split(",") if w.strip()]
        if not windows:
            raise ConfigurationError("backtest windows_hours must be non-empty")
        try:
            parsed = [float(w) for w in windows]
        except ValueError as exc:
            raise ConfigurationError(f"invalid backtest window: {exc}") from exc
        if (any(not math.isfinite(w) or w <= 0 for w in parsed)
                or parsed != sorted(parsed)):
            raise ConfigurationError("backtest windows must be positive and ascending")
        if not (0 < self.window_tolerance_fraction < 1):
            raise ConfigurationError("window_tolerance_fraction must be within (0, 1)")
        if self.success_price_change_percent <= 0:
            raise ConfigurationError("success_price_change_percent must be positive")
        if self.failure_price_change_percent >= 0:
            raise ConfigurationError("failure_price_change_percent must be negative")
        if self.signal_low_score >= self.signal_high_score:
            raise ConfigurationError("signal_low_score must be below signal_high_score")
        if (not math.isfinite(self.max_measurable_return_percent)
                or self.max_measurable_return_percent < 0):
            raise ConfigurationError(
                "max_measurable_return_percent must be >= 0 (0 disables the guard), got "
                f"{self.max_measurable_return_percent}")
        if (0 < self.max_measurable_return_percent
                <= self.success_price_change_percent):
            raise ConfigurationError(
                "max_measurable_return_percent must exceed "
                "success_price_change_percent, else every success is discarded")
        for name in ("survival_min_liquidity_usd", "alert_useful_drift_points",
                     "alert_outcome_min_hours", "min_predictions_for_weights"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"backtest setting '{name}' must be positive")

    @property
    def window_list(self) -> list[float]:
        return [float(w.strip()) for w in self.windows_hours.split(",") if w.strip()]


@dataclass(frozen=True)
class DatabaseSettings:
    """Local persistence (Part 13 Section 5, Part 21 Section 6).

    SQLite by default — simple and reliable (Rule 21); the storage layer is
    the only module that knows the backend, so PostgreSQL can replace it
    behind the same interface when scale requires.
    """

    path: str = "data/meme_intelligence.sqlite3"

    def __post_init__(self) -> None:
        if not self.path:
            raise ConfigurationError("database path must be non-empty")


@dataclass(frozen=True)
class WorkflowSettings:
    """Daily research routine configuration (Part 11)."""

    networks: str = "solana"          # comma-separated network ids to scan
    top_candidates: int = 5           # discovery candidates to deep-analyze per run
    watchlist_review_limit: int = 10  # existing entries re-checked per run
    risk_on_btc_change_percent: float = 2.0   # BTC 24h gain above this = risk-on
    risk_off_btc_drop_percent: float = 3.0    # BTC 24h drop beyond this = risk-off
    monitor_interval_seconds: float = 45.0    # continuous-scanner cycle cadence (fast layer)
    watchlist_recheck_cycles: int = 10        # re-check tracked tokens every N cycles
                                              # (secondary cadence, Part 15 Section 2)
    max_tracked_keys: int = 50000             # cap on the scanner's in-memory dedupe /
                                              # verified caches (bounds weeks-long memory)
    # Give a young token a second look once more data has likely populated
    # (2026-07-11 fix): a 1-minute-old pool usually has no GoPlus/community
    # data yet, so it scores AVOID purely from missing categories -- not a
    # real red flag -- and was previously excluded from re-analysis forever
    # (Rule 8: a data gap is not a verdict). A CONFIRMED red-flag AVOID
    # (destructive security, fake community, extreme risk -- `overrides`
    # non-empty) is real evidence and is never retried by this mechanism.
    insufficient_data_retry_enabled: bool = True
    insufficient_data_min_coverage: float = 0.5     # below this = "too early to judge"
    insufficient_data_retry_minutes: float = 15.0   # wait this long before another look
    insufficient_data_max_age_minutes: float = 120.0  # give up once the pool itself is this old

    def __post_init__(self) -> None:
        if not self.networks.strip():
            raise ConfigurationError("workflow networks must be non-empty")
        for name in ("top_candidates", "watchlist_review_limit",
                     "risk_on_btc_change_percent", "risk_off_btc_drop_percent",
                     "monitor_interval_seconds", "watchlist_recheck_cycles",
                     "max_tracked_keys", "insufficient_data_retry_minutes",
                     "insufficient_data_max_age_minutes"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"workflow setting '{name}' must be positive")
        if not (0.0 < self.insufficient_data_min_coverage <= 1.0):
            raise ConfigurationError(
                "workflow setting 'insufficient_data_min_coverage' must be in (0, 1], "
                f"got {self.insufficient_data_min_coverage}")
        if self.insufficient_data_max_age_minutes < self.insufficient_data_retry_minutes:
            raise ConfigurationError(
                "insufficient_data_max_age_minutes must be >= "
                "insufficient_data_retry_minutes (must allow at least one retry)")

    @property
    def network_list(self) -> list[str]:
        return [n.strip() for n in self.networks.split(",") if n.strip()]


@dataclass(frozen=True)
class SmartMoneySubWeights:
    """Sub-weights inside the smart-money confidence score (Part 17, Section 11)."""

    quality_wallets: float = 0.20
    historical_success: float = 0.20
    entry_timing: float = 0.20
    holding_behavior: float = 0.20
    risk_signals: float = 0.20

    def __post_init__(self) -> None:
        _check_weight_sum("smart-money", dataclasses.asdict(self))


@dataclass(frozen=True)
class WalletIntelSettings:
    """Wallet intelligence anchors (Part 17).

    Wallet analysis costs paid API credits, so it runs on demand (report,
    plan, wallets commands) and stays out of the continuous scanner unless
    explicitly enabled (Rule 10 — expensive analysis only after filtering).
    """

    whale_min_percent: float = 1.0        # holder share that counts as a whale
    risk_whale_percent: float = 5.0       # single-whale share that can crash the price
    top_holders_limit: int = 20
    recent_trades_limit: int = 50
    target_accumulating_wallets: int = 10  # distinct net buyers earning full marks
    artificial_same_size_fraction: float = 0.30  # identical-size trades above = artificial
    dominant_buyer_volume_fraction: float = 0.60  # one wallet above = artificial demand
    min_buy_volume_for_dominance_usd: float = 500.0  # below this, dominance is meaningless dust
    enable_in_monitor: bool = False       # wallet calls in the continuous scanner
    # Credit gate (2026-07-17, rebuilt after the snapshot restore; original
    # build 2026-07-15): a wallet lookup spends real, metered Helius/Birdeye
    # credits. Wired unconditionally, it used to run on EVERY analyzed Solana
    # token in the 24/7 monitor — that exhausted the free Helius tier within
    # ~3 days and 429'd the trading wallet's own balance reads (the incident
    # that got the whole layer turned off 2026-07-11). The pipeline now only
    # spends a lookup on a candidate that could still plausibly earn a
    # buy-side alert: not destructive, security score at/above this floor,
    # tradeable, and inside the alert engine's own size ceiling / freshness
    # window. The floor is only sound because AlertThresholds.momentum_min_
    # security_score holds the SAME line on the alert side (review finding:
    # momentum alerts used to fire with no security bar, so a sub-floor coin
    # could reach the phone precisely while being exempted from wallet
    # screening — keep the two floors aligned). Operator holdings, /check,
    # and plan/report lookups always bypass the gate (deliberate spend).
    credit_gate_min_security_score: float = 50.0
    # Hard spend bounds (2026-07-17 review findings): every quality input the
    # gate reads (GoPlus score, liquidity, mcap, pair age) can be manufactured
    # by an attacker launching clean-by-construction tokens, and without a
    # budget the theoretical drain was ~38k metered calls/day; separately,
    # watchlist rechecks (~7.5 min cadence) re-spent a full-price lookup on
    # the same hot coin every pass. The budget caps gated lookups per UTC day
    # (0 = unlimited); the cooldown skips a repeat lookup on the SAME token
    # inside the window (0 = off). Forced lookups (operator holdings, /check,
    # plan/report) bypass both — operator safety is never starved by a
    # budget — but still stamp the cooldown so a gated lookup right after a
    # forced one is not re-spent.
    credit_gate_max_lookups_per_day: int = 200
    credit_gate_cooldown_minutes: float = 60.0

    def __post_init__(self) -> None:
        for name in ("whale_min_percent", "risk_whale_percent", "top_holders_limit",
                     "recent_trades_limit", "target_accumulating_wallets"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"wallet setting '{name}' must be positive")
        _check_range("wallet credit_gate_min_security_score",
                     self.credit_gate_min_security_score, 0.0, 100.0)
        if self.credit_gate_max_lookups_per_day < 0:
            raise ConfigurationError(
                "wallet credit_gate_max_lookups_per_day must be >= 0 (0 = unlimited), "
                f"got {self.credit_gate_max_lookups_per_day}")
        if (not math.isfinite(self.credit_gate_cooldown_minutes)
                or self.credit_gate_cooldown_minutes < 0):
            raise ConfigurationError(
                "wallet credit_gate_cooldown_minutes must be >= 0 (0 = off), "
                f"got {self.credit_gate_cooldown_minutes}")
        for name in ("artificial_same_size_fraction", "dominant_buyer_volume_fraction"):
            if not (0 < getattr(self, name) <= 1):
                raise ConfigurationError(f"wallet setting '{name}' must be within (0, 1]")
        if (not math.isfinite(self.min_buy_volume_for_dominance_usd)
                or self.min_buy_volume_for_dominance_usd <= 0):
            raise ConfigurationError(
                "wallet setting 'min_buy_volume_for_dominance_usd' must be positive, "
                f"got {self.min_buy_volume_for_dominance_usd}")


@dataclass(frozen=True)
class SocialIntelSettings:
    """X/Twitter-adjacent social intelligence via LunarCrush (Roadmap item 5).

    Same credit-gate shape as :class:`WalletIntelSettings` (Part 17): a
    LunarCrush lookup spends real, metered API credits, so it stays out of
    the continuous scanner unless explicitly enabled, and is protected by
    the same daily-budget + per-token-cooldown bounds once it is. Built
    2026-07-20 at the operator's request, kept OFF until he supplies a paid
    LunarCrush key and runs deploy/enable-x-community-tracking.sh.
    """

    enable_in_monitor: bool = False       # LunarCrush calls in the continuous scanner
    credit_gate_min_security_score: float = 50.0
    credit_gate_max_lookups_per_day: int = 200
    credit_gate_cooldown_minutes: float = 60.0

    def __post_init__(self) -> None:
        _check_range("social credit_gate_min_security_score",
                     self.credit_gate_min_security_score, 0.0, 100.0)
        if self.credit_gate_max_lookups_per_day < 0:
            raise ConfigurationError(
                "social credit_gate_max_lookups_per_day must be >= 0 (0 = unlimited), "
                f"got {self.credit_gate_max_lookups_per_day}")
        if (not math.isfinite(self.credit_gate_cooldown_minutes)
                or self.credit_gate_cooldown_minutes < 0):
            raise ConfigurationError(
                "social credit_gate_cooldown_minutes must be >= 0 (0 = off), "
                f"got {self.credit_gate_cooldown_minutes}")


@dataclass(frozen=True)
class LiquidityProbeSettings:
    """Live round-trip sell-test via Jupiter's swap router (Project 1; Solana only).

    Denominated in SOL rather than USD to avoid a live price-conversion
    dependency in the per-token pipeline; ``probe_sol_amount`` approximates
    the target USD probe size at typical SOL prices and should be tuned in
    .env as SOL's price moves.
    """

    enabled: bool = True
    probe_sol_amount: float = 0.3   # roughly $50 at time of writing; adjust as SOL price moves
    slippage_bps: int = 500         # 5%: tolerate normal slippage without false-positiving on it
    # When a full-size sell finds no route, re-probe with this fraction of the
    # bought amount before declaring the token non-sellable: a tiny sell that
    # still routes proves the pool is merely thin (not a honeypot), so the
    # destructive verdict is reserved for tokens where NOTHING can be sold.
    sell_confirm_fraction: float = 0.05

    def __post_init__(self) -> None:
        # isfinite guard (bug-hunt finding, 2026-07-12): `nan <= 0` and
        # `inf <= 0` are both False, so without it a non-finite probe amount
        # passes config and later blows up int(amount * 1e9) at runtime —
        # inf raises an uncaught OverflowError on every Solana token, and nan
        # raises a ValueError that is caught and SILENTLY disables the
        # honeypot sell-test. Match every sibling float validator.
        if not math.isfinite(self.probe_sol_amount) or self.probe_sol_amount <= 0:
            raise ConfigurationError(
                f"liquidity probe probe_sol_amount must be positive, got {self.probe_sol_amount}"
            )
        if not (0 < self.slippage_bps <= 10000):
            raise ConfigurationError(
                f"liquidity probe slippage_bps must be within (0, 10000], got {self.slippage_bps}"
            )
        if not (0 < self.sell_confirm_fraction < 1):
            raise ConfigurationError(
                "liquidity probe sell_confirm_fraction must be within (0, 1), got "
                f"{self.sell_confirm_fraction}"
            )


@dataclass(frozen=True)
class OnChainSecuritySettings:
    """Reading the two facts that decide a rug straight off the Solana chain.

    Why this exists (DECISIONS_LOG 2026-07-29, late night): GoPlus returns no
    holder distribution and no LP data for a fresh pump.fun mint, so
    ``SecurityProfile.top_holder_percent`` / ``top10_holder_percent`` /
    ``lp_locked_percent`` are ``None`` on essentially every coin the operator is
    alerted about — measured on his live database, 499 of 500. That makes the
    security score a constant ~100 that passes 100% of buy-side alerts, and
    leaves two written-and-weighted ``RugEngine`` signals
    (``top_holder_concentration``, ``liquidity_unlocked``) that have never once
    had an input. This layer fills those fields from chain state so the EXISTING
    analyzer can do its job — no new scoring mechanism (the coverage cap that
    was tried instead is gone; it would have blocked 99% of his alerts).

    Ships OFF (``enabled=False``). The standing rule for anything that can
    change which coins reach the phone: measure it against his real data with
    ``deploy/onchain_facts_probe.py`` BEFORE switching it on.
    """

    enabled: bool = False
    # A holder census costs a handful of RPC calls per coin. Bounded and gated
    # the same way wallet intelligence is (Rule 11) — the operator watches his
    # Helius spend and exhausted a free tier in ~3 days once. 0 = unlimited /
    # off, matching WalletIntelSettings' convention.
    max_lookups_per_day: int = 500
    cooldown_minutes: float = 30.0
    # getTokenLargestAccounts returns at most 20 accounts, so asking for more
    # cannot widen the census; it only bounds how many we resolve to owners.
    top_accounts_limit: int = 20
    # Reading holder concentration means resolving token accounts to owner
    # wallets and then classifying each owner. When True, an owner whose
    # account is owned by a PROGRAM (a PDA: an AMM vault, a bonding curve, a
    # staking pool) is excluded from concentration — it is custody, not a
    # holder. This is the single highest-risk switch in the module: counting
    # those makes every healthy coin look ~90% concentrated and would silence
    # the operator. Off only for diagnostics.
    exclude_program_owned_accounts: bool = True
    # Timeout for one RPC call. Kept below the pipeline's own patience so a
    # slow census degrades to "unknown" rather than stalling a scan cycle.
    timeout_seconds: float = 8.0
    requests_per_minute: float = 120.0
    # Public-RPC fallback when no Helius key is configured (Rule 9 — degrade
    # gracefully rather than going dark). Empty = no fallback, layer stays off.
    fallback_rpc_url: str = "https://api.mainnet-beta.solana.com"
    # Burned and locked are DIFFERENT claims. This layer can verify a burn with
    # arithmetic and cannot verify a third-party lock at all (that needs the
    # locker program's account layout). The value feeds SecurityProfile's
    # `lp_locked_percent`, whose analyzer semantics are "locked OR burned", so a
    # burn figure is an honest FLOOR for it.
    #
    # The edge that matters: a pool whose LP is locked in a locker rather than
    # burned reads 0% here, which deducts 30 points for "liquidity can be
    # pulled" — a coin blocked for being safe in a way we cannot see. That is
    # the "don't silence the operator" failure mode, so it gets a lever. True
    # (default) reports the 0 and catches the genuinely-unlocked rug, which is
    # the dominant case on fresh Solana pools and the whole point of this work.
    # Set False if `deploy/onchain_facts_probe.py` shows healthy coins being
    # caught by it; then a 0% burn reports as unknown instead.
    treat_zero_burn_as_unlocked: bool = True

    def __post_init__(self) -> None:
        if self.max_lookups_per_day < 0:
            raise ConfigurationError(
                "onchain_security max_lookups_per_day must be >= 0 (0 = unlimited), "
                f"got {self.max_lookups_per_day}")
        if not math.isfinite(self.cooldown_minutes) or self.cooldown_minutes < 0:
            raise ConfigurationError(
                "onchain_security cooldown_minutes must be >= 0 (0 = off), "
                f"got {self.cooldown_minutes}")
        if not (0 < self.top_accounts_limit <= 20):
            # The RPC itself caps at 20; a larger number would be a silent lie
            # about how wide the census actually is.
            raise ConfigurationError(
                "onchain_security top_accounts_limit must be within (0, 20] — "
                f"getTokenLargestAccounts returns at most 20, got {self.top_accounts_limit}")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigurationError(
                f"onchain_security timeout_seconds must be positive, got {self.timeout_seconds}")
        if not math.isfinite(self.requests_per_minute) or self.requests_per_minute <= 0:
            raise ConfigurationError(
                "onchain_security requests_per_minute must be positive, "
                f"got {self.requests_per_minute}")


@dataclass(frozen=True)
class MomentumSubWeights:
    """Sub-weights inside the momentum score (Part 14, Section 5 — 4 x 25)."""

    price: float = 0.25
    volume: float = 0.25
    social: float = 0.25
    onchain: float = 0.25

    def __post_init__(self) -> None:
        _check_weight_sum("momentum", dataclasses.asdict(self))


@dataclass(frozen=True)
class OpportunityWeights:
    """Watchlist opportunity-ranking weights (Part 28, Section 5).

    A SECOND, upside-tilted ranking axis, distinct from the Part 31-locked
    master score: it decides which tracked tokens deserve attention/recheck
    priority, and never changes the master score or its Elite/Strong/Avoid
    classification. Literal Section 5 weights:
    Growth Potential 30 / Current Momentum 25 / Foundation Quality 20 /
    Risk Level 15 / Timing 10.
    """

    growth_potential: float = 0.30
    momentum: float = 0.25
    foundation: float = 0.20
    risk: float = 0.15
    timing: float = 0.10

    def __post_init__(self) -> None:
        _check_weight_sum("opportunity", dataclasses.asdict(self))


@dataclass(frozen=True)
class MomentumThresholds:
    """Momentum analysis anchors (Parts 14 and 26)."""

    target_trend_24h_percent: float = 30.0    # 24h gain earning a strong trend signal
    spike_1h_percent: float = 30.0            # 1h move above this = unsupported-spike risk
    late_extension_24h_percent: float = 100.0  # 24h gain above this = late entry zone
    volume_acceleration_ratio: float = 1.5    # (1h volume x24) / 24h volume above = accelerating
    volume_fade_ratio: float = 0.5            # below = volume fading
    buy_ratio_shift: float = 0.05             # 1h vs 24h buy-ratio delta that matters
    target_social_growth_7d_percent: float = 30.0

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"momentum threshold '{name}' must be positive, got {value}")
        if self.volume_fade_ratio >= self.volume_acceleration_ratio:
            raise ConfigurationError("volume_fade_ratio must be below volume_acceleration_ratio")


@dataclass(frozen=True)
class AlertEngineSettings:
    """Alert dispatch behavior (Part 13 Section 4, Part 29 Section 6)."""

    cooldown_seconds: float = 900.0  # same token+type alert suppressed within this window
    score_drop_review_points: float = 15.0  # score drop vs last snapshot triggering review
    # Below this, liquidity has collapsed and the token is treated as dead:
    # one MEDIUM post-mortem replaces the HIGH warning/score-drop pair, and
    # the token is archived instead of re-warned every recheck (Part 29
    # Section 1 — alerts exist to protect decisions, and there is no
    # decision left to protect on a completed rug).
    dead_liquidity_usd: float = 500.0
    # Interest gate (Part 29 Section 1 — alerts exist to protect DECISIONS).
    # The scanner never trades and the operator only learns about tokens
    # through HIGH opportunity alerts, so a risk warning / score drop /
    # emergency on a token that never earned one protects no decision: it is
    # background telemetry about garbage dying, not actionable intelligence.
    # When enabled, protective alerts on such tokens are demoted to LOW
    # priority — still logged and recorded in alert history, but below every
    # external sink's minimum priority, so the phone stays quiet.
    risk_alerts_require_interest: bool = True

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if isinstance(value, bool):
                continue  # switches are not magnitudes — positivity is meaningless
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"alert setting '{name}' must be positive, got {value}")


@dataclass(frozen=True)
class AlertDeliverySettings:
    """External alert delivery (Part 29, Section 8).

    Sinks activate only when their secrets exist on :class:`Settings`
    (telegram_bot_token + telegram_chat_id; discord_webhook_url).
    ``*_routes`` optionally split Section 8's channel categories
    (discoveries / smart_money / security / momentum / reports) across
    destinations: ``"security=-100123,momentum=-100456"``. External sinks
    deliver ``external_min_priority`` and above so phones only buzz for
    decision-relevant alerts (Section 1); the console still shows all.
    """

    telegram_routes: str = ""     # category=chat_id[,category=chat_id...]
    discord_routes: str = ""      # category=webhook_url[,...]
    external_min_priority: str = "medium"  # critical | high | medium | low
    requests_per_minute: float = 20.0      # per external sink (Rule 11)

    def __post_init__(self) -> None:
        if self.external_min_priority not in ("critical", "high", "medium", "low"):
            raise ConfigurationError(
                f"external_min_priority must be a valid priority, got {self.external_min_priority!r}")
        if not math.isfinite(self.requests_per_minute) or self.requests_per_minute <= 0:
            raise ConfigurationError(
                f"requests_per_minute must be positive, got {self.requests_per_minute}")
        if self.requests_per_minute <= 0:
            raise ConfigurationError("alert delivery requests_per_minute must be positive")


@dataclass(frozen=True)
class RiskSubWeights:
    """Sub-weights inside the risk score (Part 9, Section 6). Higher risk score = riskier."""

    security: float = 0.25
    market: float = 0.20
    community: float = 0.15
    token: float = 0.20
    execution: float = 0.20

    def __post_init__(self) -> None:
        _check_weight_sum("risk", dataclasses.asdict(self))


@dataclass(frozen=True)
class RiskSettings:
    """Portfolio-level exposure and drawdown discipline (Part 9, Sections 2/5/8).

    All values are guidance the system reports against — it never manages
    money directly (Part 13, Section 8).
    """

    max_open_positions: int = 10
    max_single_position_percent: float = 10.0
    max_chain_concentration_percent: float = 50.0
    max_narrative_concentration_percent: float = 40.0
    max_total_exposure_percent: float = 80.0  # remainder stays as cash reserve
    reduced_daily_loss_percent: float = 5.0    # losses beyond this => reduce risk
    defensive_daily_loss_percent: float = 10.0  # losses beyond this => defensive mode
    reduced_weekly_loss_percent: float = 10.0
    defensive_weekly_loss_percent: float = 20.0

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"risk setting '{name}' must be positive, got {value}")
        if self.reduced_daily_loss_percent >= self.defensive_daily_loss_percent:
            raise ConfigurationError("reduced_daily_loss_percent must be below defensive_daily_loss_percent")
        if self.reduced_weekly_loss_percent >= self.defensive_weekly_loss_percent:
            raise ConfigurationError("reduced_weekly_loss_percent must be below defensive_weekly_loss_percent")


@dataclass(frozen=True)
class CommunityThresholds:
    """Community-analysis anchors (Part 5, Part 18 Section 8).

    ``target_*`` values earn full marks; the fake-detection values trigger
    warnings or destructive flags (fake community => Avoid, Part 10 Section 5).
    """

    excellent_engagement_rate_percent: float = 5.0
    fake_engagement_rate_percent: float = 0.5     # below this with a big following = fake
    min_followers_for_fake_check: int = 10000
    bot_follower_warn_percent: float = 30.0
    bot_follower_artificial_percent: float = 50.0
    duplicate_message_warn_percent: float = 20.0
    telegram_active_target_percent: float = 15.0
    target_growth_rate_7d_percent: float = 30.0
    target_dev_updates_per_week: float = 3.0
    target_user_content_per_day: float = 20.0

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"community threshold '{name}' must be positive, got {value}")


@dataclass(frozen=True)
class ViralSubWeights:
    """Sub-weights inside the viral potential score (Part 19, Section 3 — 5 x 20)."""

    memorability: float = 0.20
    shareability: float = 0.20
    emotional_impact: float = 0.20
    cultural_timing: float = 0.20
    community_participation: float = 0.20

    def __post_init__(self) -> None:
        _check_weight_sum("viral", dataclasses.asdict(self))


@dataclass(frozen=True)
class NarrativeSubWeights:
    """Sub-weights inside the narrative intelligence score (Part 19, Section 11 — 5 x 20)."""

    meme_strength: float = 0.20
    cultural_timing: float = 0.20
    viral_potential: float = 0.20
    community_creativity: float = 0.20
    long_term_strength: float = 0.20

    def __post_init__(self) -> None:
        _check_weight_sum("narrative", dataclasses.asdict(self))


@dataclass(frozen=True)
class NarrativeThresholds:
    """Narrative-analysis anchors (Part 19, Section 6).

    Sentiment above ``positive_sentiment_percent`` classifies POSITIVE,
    below ``negative_sentiment_percent`` classifies NEGATIVE, in between
    NEUTRAL.
    """

    positive_sentiment_percent: float = 60.0
    negative_sentiment_percent: float = 40.0

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            _check_range(f"narrative threshold '{name}'", value, 0.0, 100.0)
        if self.negative_sentiment_percent >= self.positive_sentiment_percent:
            raise ConfigurationError(
                "negative_sentiment_percent must be below positive_sentiment_percent"
            )


@dataclass(frozen=True)
class OnChainThresholds:
    """On-chain analysis anchors (Part 6 Sections 2-12)."""

    min_holder_count: int = 50
    target_holder_count: int = 2000
    holder_growth_target_percent_24h: float = 20.0
    healthy_trades_per_trader: float = 3.0
    wash_trades_per_trader: float = 10.0
    volume_per_holder_healthy_usd: float = 500.0
    volume_per_holder_suspicious_usd: float = 5000.0
    buy_ratio_weak: float = 0.35    # below: heavy selling pressure
    buy_ratio_strong: float = 0.60  # above: healthy demand

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"on-chain threshold '{name}' must be positive, got {value}")
        if self.wash_trades_per_trader <= self.healthy_trades_per_trader:
            raise ConfigurationError("wash_trades_per_trader must exceed healthy_trades_per_trader")
        if not (0 < self.buy_ratio_weak < self.buy_ratio_strong < 1):
            raise ConfigurationError("buy ratios must satisfy 0 < weak < strong < 1")


@dataclass(frozen=True)
class LearningSettings:
    """Self-learning "mind" layer configuration (analog + model + rug).

    Every threshold, horizon, neighbor count, half-life, and retrain cadence
    the mind layer uses lives here so nothing is hardcoded (Rule 17). All
    time-decay half-lives are expressed in days; horizons in hours.

    ``horizons_hours`` is a comma-separated list rather than a scalar so it
    can still be overridden by a single ``MEMEINTEL_LEARNING_HORIZONS_HOURS``
    env var (the config loader only understands scalar fields); it is parsed
    into a tuple of floats by :meth:`horizon_hours`.
    """

    enabled: bool = False              # opt-in; off by default (Rule 11 — extra work)
    enable_in_monitor: bool = False    # feed the 24/7 scanner into the mind layer

    # Outcome label buckets (return %, relative to detection price) — Section 1
    pump_return_percent: float = 50.0        # >= this at a horizon -> PUMP
    dump_return_percent: float = -50.0       # <= this -> DUMP (else FLAT)
    horizons_hours: str = "0.25,1,6,24"      # +15m / +1h / +6h / +24h

    # Analog / FAISS k-NN forecasting — Section 3
    knn_neighbors: int = 25                  # k
    recency_half_life_days: float = 30.0     # neighbor recency decay half-life
    min_analog_neighbors: int = 5            # below this the analog vote abstains

    # Archetype clustering + novelty — Section 3
    archetype_min_cluster_size: int = 15     # HDBSCAN min_cluster_size
    novelty_percentile: float = 90.0         # novelty at/above this flags "new pattern"

    # LightGBM warm-start classifier — Section 4
    retrain_every_n: int = 200               # warm-start after N newly resolved coins
    model_half_life_days: float = 30.0       # sample time-decay half-life
    min_train_samples: int = 50              # below this the classifier abstains

    # Adaptive ensemble — Section 6
    accuracy_window: int = 200               # M: rolling window for source accuracy
    min_ensemble_confidence: float = 0.0     # floor; kept configurable

    # Continuous learning / drift — Section 7
    drift_accuracy_floor: float = 0.40       # ensemble accuracy below -> full retrain
    drift_min_samples: int = 30              # graded finals needed before drift can fire
    scaler_refit_every_n: int = 500          # re-fit StandardScaler cadence

    # Trajectory capture cadence — Section 1 (drives external snapshot callers)
    fast_snapshot_seconds: int = 60          # snapshot cadence in the first window
    fast_window_minutes: int = 60            # duration of the fast cadence
    slow_snapshot_minutes: int = 60          # cadence after the fast window
    capture_until_hours: float = 24.0        # stop capturing after this age

    # Cold start — Section 11
    min_snapshots_for_confidence: int = 3    # fewer snapshots -> low confidence
    cold_start_samples: int = 100            # resolved coins below this = cold start

    # Live evaluation uses the coin's STORED trajectory merged with the fresh
    # snapshot, because the models are TRAINED on full-trajectory fingerprints
    # (slope / volatility / acceleration) — evaluating from one snapshot fed
    # them a shapeless vector the training set never contained (train/serve
    # skew, 2026-07-28). This bounds how many of the most recent stored
    # snapshots a single evaluation may load; it is generous enough never to
    # bind in normal operation and exists only so a coin watched for days
    # cannot grow an unbounded read on a 1 GB droplet.
    max_evaluation_snapshots: int = 200

    # Alert veto (Project 3, ROADMAP #3): the mind layer's P(rug) blocks
    # HIGH opportunities ONLY once its measured rug precision has earned it
    # (Rule 8 — authority is proven, never assumed). Off by default; the
    # standing plan is to read the /mind report card with the operator
    # before flipping veto_enabled.
    veto_enabled: bool = False
    veto_min_p_rug: float = 0.85         # ensemble P(rug) at/above this vetoes
    veto_min_accuracy: float = 0.70      # measured rug PRECISION floor to earn authority
    veto_min_samples: int = 10           # graded rug calls needed before any authority
    veto_metrics_ttl_seconds: float = 1800.0  # how long the earned-authority check is cached

    # Persistence — Section 9 (db + FAISS index + models live together)
    state_dir: str = "learning_state"

    def __post_init__(self) -> None:
        if self.dump_return_percent >= self.pump_return_percent:
            raise ConfigurationError(
                "learning: dump_return_percent must be below pump_return_percent")
        for name in ("knn_neighbors", "min_analog_neighbors",
                     "retrain_every_n", "min_train_samples", "accuracy_window",
                     "drift_min_samples", "scaler_refit_every_n",
                     "fast_snapshot_seconds", "fast_window_minutes",
                     "slow_snapshot_minutes", "min_snapshots_for_confidence",
                     "cold_start_samples"):
            value = getattr(self, name)
            if value <= 0:
                raise ConfigurationError(f"learning setting '{name}' must be positive, got {value}")
        # HDBSCAN requires min_cluster_size >= 2; 1 is not a meaningful cluster
        # size and would crash the archetype pass, so reject it at config time.
        if self.archetype_min_cluster_size < 2:
            raise ConfigurationError(
                "learning archetype_min_cluster_size must be >= 2, got "
                f"{self.archetype_min_cluster_size}")
        for name in ("recency_half_life_days", "model_half_life_days", "capture_until_hours"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"learning setting '{name}' must be positive, got {value}")
        _check_range("learning drift_accuracy_floor", self.drift_accuracy_floor, 0.0, 1.0)
        _check_range("learning novelty_percentile", self.novelty_percentile, 0.0, 100.0)
        _check_range("learning min_ensemble_confidence", self.min_ensemble_confidence, 0.0, 1.0)
        _check_range("learning veto_min_p_rug", self.veto_min_p_rug, 0.0, 1.0)
        _check_range("learning veto_min_accuracy", self.veto_min_accuracy, 0.0, 1.0)
        if self.veto_min_samples <= 0:
            raise ConfigurationError(
                f"learning veto_min_samples must be positive, got {self.veto_min_samples}")
        if self.max_evaluation_snapshots <= 0:
            raise ConfigurationError(
                "learning max_evaluation_snapshots must be positive, got "
                f"{self.max_evaluation_snapshots}")
        if not math.isfinite(self.veto_metrics_ttl_seconds) or self.veto_metrics_ttl_seconds <= 0:
            raise ConfigurationError(
                "learning veto_metrics_ttl_seconds must be positive, got "
                f"{self.veto_metrics_ttl_seconds}")
        if not self.horizon_hours():
            raise ConfigurationError("learning: horizons_hours must list at least one horizon")

    def horizon_hours(self) -> tuple[float, ...]:
        """Parse ``horizons_hours`` into an ordered tuple of positive floats."""
        hours: list[float] = []
        for piece in self.horizons_hours.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value = float(piece)
            except ValueError as exc:
                raise ConfigurationError(
                    f"learning: invalid horizon {piece!r} in horizons_hours") from exc
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"learning: horizon must be positive, got {value}")
            hours.append(value)
        return tuple(sorted(set(hours)))


@dataclass(frozen=True)
class LightGBMSettings:
    """Hyperparameters for the warm-started outcome classifier (Section 4).

    Kept small and configurable (Rule 17). ``full_retrain_rounds`` is the tree
    budget for a from-scratch train; ``warm_start_rounds`` is how many trees
    each warm-start adds on top of the prior model (``init_model``) so recent
    data refines rather than replaces. Defaults are conservative for the small
    datasets the layer starts with — deeper/greedier settings would overfit a
    young dataset (Rule 8/21).
    """

    full_retrain_rounds: int = 120
    warm_start_rounds: int = 30
    learning_rate: float = 0.05
    num_leaves: int = 31
    min_child_samples: int = 5
    # Rugs/pumps are rare next to flats; balanced weighting (sklearn's
    # N / (n_classes_present * class_count) formula) stops the model from
    # buying accuracy by always predicting the majority class.
    balanced_class_weights: bool = True

    def __post_init__(self) -> None:
        for name in ("full_retrain_rounds", "warm_start_rounds", "num_leaves",
                     "min_child_samples"):
            value = getattr(self, name)
            if value <= 0:
                raise ConfigurationError(f"lightgbm setting '{name}' must be positive, got {value}")
        if not (0.0 < self.learning_rate <= 1.0):
            raise ConfigurationError(
                f"lightgbm learning_rate must be in (0, 1], got {self.learning_rate}")


@dataclass(frozen=True)
class TelegramCommandSettings:
    """Two-way Telegram control (Project 2, ROADMAP item 2).

    When ``enabled`` (and the Telegram bot token + chat id secrets exist),
    the monitor long-polls the Bot API's ``getUpdates`` endpoint and answers
    operator commands (/status, /why, /check, ...). Off by default — the
    operator opts in explicitly (ROADMAP #2). Only ONE consumer may call
    ``getUpdates`` per bot token; this listener is that consumer (the alert
    sink only ever calls ``sendMessage``).
    """

    enabled: bool = False
    poll_timeout_seconds: float = 25.0     # server-side long-poll wait (1..50)
    idle_delay_seconds: float = 2.0        # pause between successful polls
    error_backoff_max_seconds: float = 60.0  # cap for the poll-error backoff
    # Optional comma-separated Telegram USER ids allowed to issue commands.
    # Authorization is otherwise chat-scoped, and `.env.example` tells the
    # operator to "add it to your group/channel" — in a group EVERY member
    # inherits the whole command surface, including /buy and /dump on the real
    # trading wallet (bug-hunt finding, 2026-07-29). Empty = unchanged
    # behaviour (chat-scoped), which is safe for a private one-to-one chat;
    # set it to your own user id when the bot lives in a group.
    allowed_user_ids: str = ""

    def user_id_list(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.allowed_user_ids.split(",") if p.strip())

    def __post_init__(self) -> None:
        for user_id in self.user_id_list():
            if not (user_id.lstrip("-").isdigit()):
                raise ConfigurationError(
                    "telegram_commands allowed_user_ids must be numeric Telegram "
                    f"user ids, got {user_id!r}")
        if not math.isfinite(self.poll_timeout_seconds) or not (
                1.0 <= self.poll_timeout_seconds <= 50.0):
            raise ConfigurationError(
                "telegram_commands poll_timeout_seconds must be within [1, 50], "
                f"got {self.poll_timeout_seconds}")
        for name in ("idle_delay_seconds", "error_backoff_max_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(
                    f"telegram_commands setting '{name}' must be positive, got {value}")


@dataclass(frozen=True)
class ExecutionSettings:
    """Operator-initiated manual buy/dump from Telegram (Project 6).

    HARD SAFETY MODEL. The system never AUTO-trades; a buy or dump is only
    ever a button the OPERATOR taps. Even so, real execution means the bot
    signs Solana transactions, which requires the trading wallet's private
    key on the droplet — so:

    * ``live_enabled`` gates real execution and defaults OFF. With it off (or
      no key configured) every buy/dump routes to the dry-run executor,
      which signs nothing.
    * The trading key is a DEDICATED, low-balance wallet, never the
      operator's main wallet, read only from ``MEMEINTEL_EXECUTION_PRIVATE_KEY``
      (never in code, git, or logs — Rule 16). The real hard cap is how
      little the operator funds it with.
    * ``max_buy_sol`` is an OPTIONAL per-trade ceiling; a single buy above it
      is refused. 0 = no ceiling (operator request, 2026-07-20 — the fixed
      cap didn't scale once percentage-of-balance buy buttons existed, so an
      ordinary percentage tap started getting refused as the wallet grew).
      With it at 0, the wallet balance is the ONLY automatic limit left: the
      bot still can never spend SOL it does not hold, and a live re-check
      against the current balance still runs immediately before every trade
      — but nothing stops a single tap from committing the entire balance.

    See DECISIONS_LOG (2026-07-10, Project 6; 2026-07-20 cap removal).
    """

    buy_button_enabled: bool = False  # show Buy/Dump buttons on Telegram alerts
    live_enabled: bool = False        # actually sign+send trades (else dry-run)
    max_buy_sol: float = 0.15         # per-trade SOL ceiling; 0 = no ceiling
    slippage_bps: int = 500           # base slippage for trade quotes (dynamic on top)
    priority_fee_max_lamports: int = 1_000_000   # cap on priority fee per trade (0.001 SOL)
    confirm_timeout_seconds: float = 45.0        # how long to wait for on-chain confirmation
    # Extra attempts (each with a FRESH quote) when the network's preflight
    # simulation definitively rejects a trade before broadcast — nothing was
    # spent, so a re-quote at the current price is safe and is how a
    # fast-moving coin gets caught (slippage kept tripping live, 2026-07-19).
    # 0 = never retry. Ambiguous submission errors are NEVER auto-retried.
    preflight_retries: int = 2
    # One-tap buy buttons on alerts, sized as a PERCENT of the trading wallet's
    # current spendable balance (2026-07-18, operator request — replaces the
    # old fixed-SOL-amount presets: "0.01/0.04 SOL" buttons didn't scale with
    # the wallet's actual balance). Percentage is resolved against a LIVE
    # balance read at the moment the button is tapped (never at alert-render
    # time — the balance moves), after reserving the same fee/rent buffer
    # ``execute_buy`` always keeps back, so a 100% tap can actually succeed.
    # Every computed SOL amount still passes through the existing
    # ``max_buy_sol`` hard cap and live balance re-check — a percentage
    # button is a sizing convenience, not a bypass of the safety model.
    buy_button_percents: str = "20,50,75,100"

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_buy_sol) or self.max_buy_sol < 0:
            raise ConfigurationError(
                f"execution max_buy_sol must be >= 0 (0 = no ceiling), got "
                f"{self.max_buy_sol}")
        if not (0 < self.slippage_bps <= 10000):
            raise ConfigurationError(
                f"execution slippage_bps must be within (0, 10000], got {self.slippage_bps}")
        if self.priority_fee_max_lamports < 0:
            raise ConfigurationError(
                "execution priority_fee_max_lamports must be >= 0, got "
                f"{self.priority_fee_max_lamports}")
        if not math.isfinite(self.confirm_timeout_seconds) or self.confirm_timeout_seconds <= 0:
            raise ConfigurationError(
                "execution confirm_timeout_seconds must be positive, got "
                f"{self.confirm_timeout_seconds}")
        if not (0 <= self.preflight_retries <= 10):
            raise ConfigurationError(
                f"execution preflight_retries must be within [0, 10], got "
                f"{self.preflight_retries}")
        self.buy_percent_list()  # validates as a side effect

    def buy_percent_list(self) -> tuple[float, ...]:
        """Parse ``buy_button_percents`` into percentages in (0, 100], ordered."""
        percents: list[float] = []
        for part in self.buy_button_percents.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = float(part)
            except ValueError as exc:
                raise ConfigurationError(
                    f"execution buy_button_percents has a non-number: {part!r}") from exc
            if not math.isfinite(value) or not (0.0 < value <= 100.0):
                raise ConfigurationError(
                    "execution buy_button_percents entries must be in (0, 100], "
                    f"got {value}")
            percents.append(value)
        return tuple(percents)


@dataclass(frozen=True)
class RugThresholds:
    """Firing thresholds for the hard rug signals (Section 5a).

    Separate from :class:`RugSignalWeights` (which sets how many points a fired
    signal contributes): these decide *whether* each signal fires. All
    configurable (Rule 17). Percentages are 0-100.
    """

    min_lp_locked_percent: float = 50.0        # below this -> "liquidity not locked"
    top_holder_percent_max: float = 30.0       # single holder above -> concentration
    top10_holder_percent_max: float = 70.0     # top 10 above -> concentration
    liquidity_drop_percent: float = 50.0       # fall from peak -> "liquidity removed"
    liquidity_removal_usd: float = 1000.0      # single LP-remove event magnitude
    sell_tax_max_percent: float = 20.0         # sell tax at/above -> "high sell tax"
    dev_dump_usd: float = 1000.0               # creator outflow at/above -> "dev dumping"
    fake_volume_per_holder_usd: float = 5000.0  # volume/holder above -> "fake volume"
    fake_volume_min_volume_usd: float = 1000.0  # only flag fake volume above this volume

    def __post_init__(self) -> None:
        for name in ("min_lp_locked_percent", "top_holder_percent_max",
                     "top10_holder_percent_max", "liquidity_drop_percent",
                     "sell_tax_max_percent"):
            _check_range(f"rug threshold '{name}'", getattr(self, name), 0.0, 100.0)
        for name in ("liquidity_removal_usd", "dev_dump_usd",
                     "fake_volume_per_holder_usd", "fake_volume_min_volume_usd"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"rug threshold '{name}' must be positive, got {value}")


@dataclass(frozen=True)
class RugWatchSettings:
    """Live rug watch on coins the operator ALREADY OWNS (2026-07-29).

    Distinct from the pre-alert rug engine: this guards money already
    committed. The operator lost a position to a rug and asked for the bot to
    exit by itself, so ``auto_sell`` can spend real money with no human in the
    loop — it is therefore OFF by default and requires BOTH ``enabled`` and
    ``auto_sell`` to be armed explicitly (Rule 16-style least privilege applied
    to money rather than secrets).

    The defaults are deliberately conservative. A false exit sells a healthy
    position at a loss, so a trigger must clear ``exit_drop_percent`` on
    ``min_confirmations`` consecutive readings, and no verdict at all is
    produced before ``min_readings`` measured observations exist.
    """

    enabled: bool = False          # run the watch loop at all
    auto_sell: bool = False        # let it SELL without asking (needs enabled too)
    poll_seconds: float = 15.0     # how often each held coin is re-read
    exit_drop_percent: float = 55.0   # fall from peak liquidity that triggers exit
    warn_drop_percent: float = 30.0   # fall that is worth telling the operator about
    min_confirmations: int = 2     # consecutive readings that must agree before selling
    min_readings: int = 2          # measured observations needed before any verdict
    max_positions: int = 20        # bound the per-poll work on a 1 vCPU droplet
    probe_sell_route: bool = True  # ask Jupiter whether an exit still exists

    def __post_init__(self) -> None:
        if self.auto_sell and not self.enabled:
            raise ConfigurationError(
                "rug_watch auto_sell is on but rug_watch enabled is off — arm both "
                "or neither, so a half-configured guard never looks armed")
        _check_range("rug_watch exit_drop_percent", self.exit_drop_percent, 0.0, 100.0)
        _check_range("rug_watch warn_drop_percent", self.warn_drop_percent, 0.0, 100.0)
        if self.warn_drop_percent > self.exit_drop_percent:
            raise ConfigurationError(
                "rug_watch warn_drop_percent must not exceed exit_drop_percent, "
                f"got warn={self.warn_drop_percent} exit={self.exit_drop_percent}")
        if not math.isfinite(self.poll_seconds) or self.poll_seconds <= 0:
            raise ConfigurationError(
                f"rug_watch poll_seconds must be positive, got {self.poll_seconds}")
        for name in ("min_confirmations", "min_readings", "max_positions"):
            value = getattr(self, name)
            if value <= 0:
                raise ConfigurationError(
                    f"rug_watch '{name}' must be positive, got {value}")
        if self.min_confirmations < 2:
            # One reading is a provider tick, not evidence. Selling on it is
            # exactly the failure mode this guard must not have.
            raise ConfigurationError(
                "rug_watch min_confirmations must be at least 2 — a single "
                "reading must never be able to liquidate a position")


@dataclass(frozen=True)
class RugSignalWeights:
    """Point contributions for each hard rug signal (Section 5a).

    Unlike the framework's normalized category weights, these are additive
    *points* summed into a 0-100 rug-risk score (clamped at 100). Higher
    points = a stronger standalone rug indicator. Kept configurable (Rule 17)
    and non-normalized on purpose — the spec sums signals to a score rather
    than averaging them.
    """

    liquidity_unlocked: float = 20.0         # LP not locked / lock expiring soon
    mint_authority_active: float = 20.0      # owner can print supply
    freeze_authority_active: float = 15.0    # owner can freeze holders
    top_holder_concentration: float = 15.0   # single/cluster holds large supply %
    liquidity_removed: float = 30.0          # real-time LP burn/withdraw
    unsellable: float = 30.0                 # honeypot / failed sell simulation
    high_sell_tax: float = 15.0              # sell tax above threshold
    dev_wallet_dumping: float = 20.0         # large creator outbound transfers
    fake_volume: float = 10.0                # volume vs holder count (wash trading)
    deployer_blacklisted: float = 25.0       # creator linked to prior rugs

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"rug signal weight '{name}' must be >= 0, got {value}")


@dataclass(frozen=True)
class Settings:
    """Root settings object. Build with :func:`Settings.from_env`."""

    weights: ScoringWeights = field(default_factory=ScoringWeights)
    security_weights: SecuritySubWeights = field(default_factory=SecuritySubWeights)
    bands: ClassificationBands = field(default_factory=ClassificationBands)
    alerts: AlertThresholds = field(default_factory=AlertThresholds)
    intervals: ScanIntervals = field(default_factory=ScanIntervals)
    http: HttpSettings = field(default_factory=HttpSettings)
    providers: ProviderSettings = field(default_factory=ProviderSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    pumpfun: PumpFunSettings = field(default_factory=PumpFunSettings)
    boost_watcher: BoostWatcherSettings = field(default_factory=BoostWatcherSettings)
    security: SecurityThresholds = field(default_factory=SecurityThresholds)
    community: CommunityThresholds = field(default_factory=CommunityThresholds)
    onchain: OnChainThresholds = field(default_factory=OnChainThresholds)
    community_weights: CommunitySubWeights = field(default_factory=CommunitySubWeights)
    onchain_weights: OnChainSubWeights = field(default_factory=OnChainSubWeights)
    foundation_weights: FoundationSubWeights = field(default_factory=FoundationSubWeights)
    token: TokenThresholds = field(default_factory=TokenThresholds)
    token_weights: TokenSubWeights = field(default_factory=TokenSubWeights)
    trading: TradingSettings = field(default_factory=TradingSettings)
    trade_weights: TradeScoreWeights = field(default_factory=TradeScoreWeights)
    risk: RiskSettings = field(default_factory=RiskSettings)
    risk_weights: RiskSubWeights = field(default_factory=RiskSubWeights)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    workflow: WorkflowSettings = field(default_factory=WorkflowSettings)
    momentum: MomentumThresholds = field(default_factory=MomentumThresholds)
    momentum_weights: MomentumSubWeights = field(default_factory=MomentumSubWeights)
    opportunity_weights: OpportunityWeights = field(default_factory=OpportunityWeights)
    narrative: NarrativeThresholds = field(default_factory=NarrativeThresholds)
    narrative_weights: NarrativeSubWeights = field(default_factory=NarrativeSubWeights)
    viral_weights: ViralSubWeights = field(default_factory=ViralSubWeights)
    alert_engine: AlertEngineSettings = field(default_factory=AlertEngineSettings)
    alert_delivery: AlertDeliverySettings = field(default_factory=AlertDeliverySettings)
    telegram_commands: TelegramCommandSettings = field(default_factory=TelegramCommandSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    wallet: WalletIntelSettings = field(default_factory=WalletIntelSettings)
    social: SocialIntelSettings = field(default_factory=SocialIntelSettings)
    smart_money_weights: SmartMoneySubWeights = field(default_factory=SmartMoneySubWeights)
    liquidity_probe: LiquidityProbeSettings = field(default_factory=LiquidityProbeSettings)
    onchain_security: OnChainSecuritySettings = field(
        default_factory=OnChainSecuritySettings)
    ai: AISettings = field(default_factory=AISettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    learning: LearningSettings = field(default_factory=LearningSettings)
    lightgbm: LightGBMSettings = field(default_factory=LightGBMSettings)
    rug_thresholds: RugThresholds = field(default_factory=RugThresholds)
    rug_watch: RugWatchSettings = field(default_factory=RugWatchSettings)
    rug_signal_weights: RugSignalWeights = field(default_factory=RugSignalWeights)
    log_level: str = "INFO"
    log_dir: str = "logs"
    # API keys (Rule 16): read from MEMEINTEL_HELIUS_API_KEY / MEMEINTEL_BIRDEYE_API_KEY /
    # MEMEINTEL_ANTHROPIC_API_KEY / MEMEINTEL_JUPITER_API_KEY (or a local
    # .env). Empty string = that layer stays off.
    helius_api_key: str = ""
    birdeye_api_key: str = ""
    # LunarCrush (Roadmap item 5, X/Twitter-adjacent social intelligence):
    # empty = the social layer stays off, same "empty key = off" convention
    # as every other API key here.
    lunarcrush_api_key: str = ""
    anthropic_api_key: str = ""
    jupiter_api_key: str = ""
    # Optional free demo key: raises CoinGecko's rate limit for community data.
    coingecko_api_key: str = ""
    # Alert delivery secrets (Part 29): sinks stay off while these are empty.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    # Trading wallet secret (Project 6): base58 private key of the DEDICATED
    # low-balance trading wallet. Empty = no live executor (dry-run only).
    # Read from env only; never logged or committed (Rule 16).
    trading_private_key: str = ""
    # Optional DEDICATED Helius key for live trading (Project 6): a buy/dump
    # only needs a handful of RPC calls, but the scanner's wallet-intelligence
    # traffic can exhaust the shared account's server-side budget and 429 the
    # trade's balance read (observed live 2026-07-11). Set this to a SECOND
    # Helius account's key so trading has its own untouched budget. Empty =
    # trading shares MEMEINTEL_HELIUS_API_KEY (previous behavior, Rule 18).
    trading_helius_api_key: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from environment variables, falling back to spec defaults."""
        env = os.environ if env is None else env
        return cls(
            weights=_load_group(ScoringWeights, "WEIGHTS", env),
            security_weights=_load_group(SecuritySubWeights, "SECURITY_WEIGHTS", env),
            bands=_load_group(ClassificationBands, "BANDS", env),
            alerts=_load_group(AlertThresholds, "ALERTS", env),
            intervals=_load_group(ScanIntervals, "INTERVALS", env),
            http=_load_group(HttpSettings, "HTTP", env),
            providers=_load_group(ProviderSettings, "PROVIDERS", env),
            discovery=_load_group(DiscoverySettings, "DISCOVERY", env),
            pumpfun=_load_group(PumpFunSettings, "PUMPFUN", env),
            boost_watcher=_load_group(BoostWatcherSettings, "BOOST_WATCHER", env),
            security=_load_group(SecurityThresholds, "SECURITY", env),
            community=_load_group(CommunityThresholds, "COMMUNITY", env),
            onchain=_load_group(OnChainThresholds, "ONCHAIN", env),
            community_weights=_load_group(CommunitySubWeights, "COMMUNITY_WEIGHTS", env),
            onchain_weights=_load_group(OnChainSubWeights, "ONCHAIN_WEIGHTS", env),
            foundation_weights=_load_group(FoundationSubWeights, "FOUNDATION_WEIGHTS", env),
            token=_load_group(TokenThresholds, "TOKEN", env),
            token_weights=_load_group(TokenSubWeights, "TOKEN_WEIGHTS", env),
            trading=_load_group(TradingSettings, "TRADING", env),
            trade_weights=_load_group(TradeScoreWeights, "TRADE_WEIGHTS", env),
            risk=_load_group(RiskSettings, "RISK", env),
            risk_weights=_load_group(RiskSubWeights, "RISK_WEIGHTS", env),
            database=_load_group(DatabaseSettings, "DATABASE", env),
            workflow=_load_group(WorkflowSettings, "WORKFLOW", env),
            momentum=_load_group(MomentumThresholds, "MOMENTUM", env),
            momentum_weights=_load_group(MomentumSubWeights, "MOMENTUM_WEIGHTS", env),
            opportunity_weights=_load_group(OpportunityWeights, "OPPORTUNITY_WEIGHTS", env),
            narrative=_load_group(NarrativeThresholds, "NARRATIVE", env),
            narrative_weights=_load_group(NarrativeSubWeights, "NARRATIVE_WEIGHTS", env),
            viral_weights=_load_group(ViralSubWeights, "VIRAL_WEIGHTS", env),
            alert_engine=_load_group(AlertEngineSettings, "ALERT_ENGINE", env),
            alert_delivery=_load_group(AlertDeliverySettings, "ALERT_DELIVERY", env),
            telegram_commands=_load_group(TelegramCommandSettings, "TELEGRAM_COMMANDS", env),
            execution=_load_group(ExecutionSettings, "EXECUTION", env),
            wallet=_load_group(WalletIntelSettings, "WALLET", env),
            social=_load_group(SocialIntelSettings, "SOCIAL", env),
            smart_money_weights=_load_group(SmartMoneySubWeights, "SMART_MONEY_WEIGHTS", env),
            liquidity_probe=_load_group(LiquidityProbeSettings, "LIQUIDITY_PROBE", env),
            onchain_security=_load_group(
                OnChainSecuritySettings, "ONCHAIN_SECURITY", env),
            ai=_load_group(AISettings, "AI", env),
            backtest=_load_group(BacktestSettings, "BACKTEST", env),
            learning=_load_group(LearningSettings, "LEARNING", env),
            lightgbm=_load_group(LightGBMSettings, "LIGHTGBM", env),
            rug_thresholds=_load_group(RugThresholds, "RUG_THRESHOLDS", env),
            rug_watch=_load_group(RugWatchSettings, "RUG_WATCH", env),
            rug_signal_weights=_load_group(RugSignalWeights, "RUG_SIGNAL_WEIGHTS", env),
            log_level=env.get(f"{_ENV_PREFIX}_LOG_LEVEL", "INFO"),
            log_dir=env.get(f"{_ENV_PREFIX}_LOG_DIR", "logs"),
            helius_api_key=env.get(f"{_ENV_PREFIX}_HELIUS_API_KEY", ""),
            birdeye_api_key=env.get(f"{_ENV_PREFIX}_BIRDEYE_API_KEY", ""),
            lunarcrush_api_key=env.get(f"{_ENV_PREFIX}_LUNARCRUSH_API_KEY", ""),
            jupiter_api_key=env.get(f"{_ENV_PREFIX}_JUPITER_API_KEY", ""),
            anthropic_api_key=env.get(f"{_ENV_PREFIX}_ANTHROPIC_API_KEY", ""),
            coingecko_api_key=env.get(f"{_ENV_PREFIX}_COINGECKO_API_KEY", ""),
            telegram_bot_token=env.get(f"{_ENV_PREFIX}_TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get(f"{_ENV_PREFIX}_TELEGRAM_CHAT_ID", ""),
            discord_webhook_url=env.get(f"{_ENV_PREFIX}_DISCORD_WEBHOOK_URL", ""),
            trading_private_key=env.get(f"{_ENV_PREFIX}_EXECUTION_PRIVATE_KEY", ""),
            trading_helius_api_key=env.get(f"{_ENV_PREFIX}_EXECUTION_HELIUS_API_KEY", ""),
        )


def _convert(raw: str, default: Any, key: str) -> Any:
    """Convert an env string to the type of the field's default value."""
    try:
        if isinstance(default, bool):  # bool is a subclass of int; check first
            normalized = raw.strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
            # An unrecognized string silently became False before this fix —
            # a typo like "MEMEINTEL_AI_ENABLE_IN_MONITOR=treu" would quietly
            # disable a feature the user meant to enable (Rule 6/13: fail
            # loudly, don't guess). Numeric fields already raise on garbage;
            # booleans should too.
            raise ValueError(f"expected a boolean (true/false/yes/no/1/0/on/off), got {raw!r}")
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        return raw
    except ValueError as exc:
        raise ConfigurationError(f"invalid value for {key}: {raw!r} ({exc})") from exc


def _load_group(cls: type, group: str, env: Mapping[str, str]) -> Any:
    """Instantiate a settings dataclass, overriding fields from MEMEINTEL_<GROUP>_<FIELD> vars."""
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        key = f"{_ENV_PREFIX}_{group}_{f.name.upper()}"
        if key in env:
            kwargs[f.name] = _convert(env[key], f.default, key)
    return cls(**kwargs)


def load_dotenv(path: str = ".env") -> int:
    """Load KEY=VALUE lines from a local .env file into the process environment.

    Real environment variables always win over file values; lines starting
    with '#' and blank lines are ignored. Returns the number of values
    loaded. Missing file is fine — .env is optional (Rule 16: secrets live
    outside the repository).
    """
    loaded = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded += 1
    except OSError:
        return 0
    return loaded


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, loading .env then the environment on first use."""
    global _settings
    if _settings is None:
        load_dotenv()
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Clear the cached settings (used by tests and config reloads)."""
    global _settings
    _settings = None
