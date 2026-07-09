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

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            _check_range(f"alert threshold '{name}'", value, 0.0, 100.0)
        if self.strong_candidate_overall < self.overall:
            raise ConfigurationError(
                "strong_candidate_overall must be >= overall "
                f"({self.strong_candidate_overall} < {self.overall})")


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
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"  # free data WS (Part 32.5 S3)
    pumpfun_base_url: str = "https://frontend-api-v3.pump.fun"  # unofficial; can change
    pumpfun_requests_per_minute: float = 30.0         # no documented limit; stay conservative
    failure_threshold: int = 3      # consecutive failures before a provider cools down
    cooldown_seconds: float = 60.0  # how long an unhealthy provider is skipped

    def __post_init__(self) -> None:
        for name in ("dexscreener", "geckoterminal", "goplus", "coingecko",
                     "helius", "birdeye", "pumpfun"):
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

    @property
    def launchpad_list(self) -> list[str]:
        return [pool.strip() for pool in self.launchpads.split(",") if pool.strip()]

    def __post_init__(self) -> None:
        for name in ("max_creator_buy_percent", "max_pending", "pending_ttl_hours",
                     "recheck_interval_seconds", "max_rechecks_per_cycle",
                     "min_market_cap_growth_ratio", "min_usd_market_cap",
                     "min_reply_count", "max_last_trade_age_minutes"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"pumpfun setting '{name}' must be positive")
        if self.max_creator_buy_percent > 100.0:
            raise ConfigurationError("max_creator_buy_percent must be within (0, 100]")
        if not self.launchpad_list:
            raise ConfigurationError("pumpfun launchpads must name at least one pool")


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

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"security threshold '{name}' must be positive, got {value}")
        if self.extreme_tax_percent < self.max_tax_percent:
            raise ConfigurationError("extreme_tax_percent must be >= max_tax_percent")


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
    min_predictions_for_weights: int = 10    # weight experiments need a real sample (S1)

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
        for name in ("survival_min_liquidity_usd", "alert_useful_drift_points",
                     "min_predictions_for_weights"):
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

    def __post_init__(self) -> None:
        if not self.networks.strip():
            raise ConfigurationError("workflow networks must be non-empty")
        for name in ("top_candidates", "watchlist_review_limit",
                     "risk_on_btc_change_percent", "risk_off_btc_drop_percent",
                     "monitor_interval_seconds", "watchlist_recheck_cycles"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"workflow setting '{name}' must be positive")

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

    def __post_init__(self) -> None:
        for name in ("whale_min_percent", "risk_whale_percent", "top_holders_limit",
                     "recent_trades_limit", "target_accumulating_wallets"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"wallet setting '{name}' must be positive")
        for name in ("artificial_same_size_fraction", "dominant_buyer_volume_fraction"):
            if not (0 < getattr(self, name) <= 1):
                raise ConfigurationError(f"wallet setting '{name}' must be within (0, 1]")
        if (not math.isfinite(self.min_buy_volume_for_dominance_usd)
                or self.min_buy_volume_for_dominance_usd <= 0):
            raise ConfigurationError(
                "wallet setting 'min_buy_volume_for_dominance_usd' must be positive, "
                f"got {self.min_buy_volume_for_dominance_usd}")


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

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
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
    wallet: WalletIntelSettings = field(default_factory=WalletIntelSettings)
    smart_money_weights: SmartMoneySubWeights = field(default_factory=SmartMoneySubWeights)
    ai: AISettings = field(default_factory=AISettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    log_level: str = "INFO"
    log_dir: str = "logs"
    # API keys (Rule 16): read from MEMEINTEL_HELIUS_API_KEY / MEMEINTEL_BIRDEYE_API_KEY /
    # MEMEINTEL_ANTHROPIC_API_KEY (or a local .env). Empty string = that layer stays off.
    helius_api_key: str = ""
    birdeye_api_key: str = ""
    anthropic_api_key: str = ""
    # Optional free demo key: raises CoinGecko's rate limit for community data.
    coingecko_api_key: str = ""
    # Alert delivery secrets (Part 29): sinks stay off while these are empty.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""

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
            wallet=_load_group(WalletIntelSettings, "WALLET", env),
            smart_money_weights=_load_group(SmartMoneySubWeights, "SMART_MONEY_WEIGHTS", env),
            ai=_load_group(AISettings, "AI", env),
            backtest=_load_group(BacktestSettings, "BACKTEST", env),
            log_level=env.get(f"{_ENV_PREFIX}_LOG_LEVEL", "INFO"),
            log_dir=env.get(f"{_ENV_PREFIX}_LOG_DIR", "logs"),
            helius_api_key=env.get(f"{_ENV_PREFIX}_HELIUS_API_KEY", ""),
            birdeye_api_key=env.get(f"{_ENV_PREFIX}_BIRDEYE_API_KEY", ""),
            anthropic_api_key=env.get(f"{_ENV_PREFIX}_ANTHROPIC_API_KEY", ""),
            coingecko_api_key=env.get(f"{_ENV_PREFIX}_COINGECKO_API_KEY", ""),
            telegram_bot_token=env.get(f"{_ENV_PREFIX}_TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get(f"{_ENV_PREFIX}_TELEGRAM_CHAT_ID", ""),
            discord_webhook_url=env.get(f"{_ENV_PREFIX}_DISCORD_WEBHOOK_URL", ""),
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
