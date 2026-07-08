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
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
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
    """Sub-weights inside the security score (Part 33, Section 10)."""

    contract: float = 0.25
    liquidity: float = 0.25
    distribution: float = 0.20
    developer: float = 0.15
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

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            _check_range(f"alert threshold '{name}'", value, 0.0, 100.0)


@dataclass(frozen=True)
class ScanIntervals:
    """Refresh cadence in seconds for each scanning speed (Part 21, Section 4)."""

    ultra_fast: float = 7.0     # price, liquidity, large transactions, new pairs
    fast: float = 45.0          # holder growth, volume changes, wallet activity
    research: float = 600.0     # community, narrative, security, competition
    historical: float = 86400.0  # performance, prediction accuracy

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if value <= 0:
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
        if self.timeout_seconds <= 0:
            raise ConfigurationError(f"timeout_seconds must be positive, got {self.timeout_seconds}")
        if self.retry_attempts < 1:
            raise ConfigurationError(f"retry_attempts must be >= 1, got {self.retry_attempts}")


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
    failure_threshold: int = 3      # consecutive failures before a provider cools down
    cooldown_seconds: float = 60.0  # how long an unhealthy provider is skipped

    def __post_init__(self) -> None:
        for name in ("dexscreener", "geckoterminal", "goplus", "coingecko"):
            if getattr(self, f"{name}_requests_per_minute") <= 0:
                raise ConfigurationError(f"{name}_requests_per_minute must be positive")
        if self.failure_threshold < 1:
            raise ConfigurationError("failure_threshold must be >= 1")


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
            if value <= 0:
                raise ConfigurationError(f"discovery setting '{name}' must be positive, got {value}")
        if self.target_liquidity_usd < self.min_liquidity_usd:
            raise ConfigurationError("target_liquidity_usd must be >= min_liquidity_usd")
        if self.target_volume_24h_usd < self.min_volume_24h_usd:
            raise ConfigurationError("target_volume_24h_usd must be >= min_volume_24h_usd")


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
            if value <= 0:
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
            if value <= 0:
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
            if value <= 0:
                raise ConfigurationError(f"trading setting '{name}' must be positive, got {value}")
        if self.medium_conviction_min_score >= self.high_conviction_min_score:
            raise ConfigurationError("medium_conviction_min_score must be below high_conviction_min_score")
        if not (0 < self.min_confirmation_coverage <= 1):
            raise ConfigurationError("min_confirmation_coverage must be within (0, 1]")


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

    def __post_init__(self) -> None:
        if not self.networks.strip():
            raise ConfigurationError("workflow networks must be non-empty")
        for name in ("top_candidates", "watchlist_review_limit",
                     "risk_on_btc_change_percent", "risk_off_btc_drop_percent",
                     "monitor_interval_seconds"):
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"workflow setting '{name}' must be positive")

    @property
    def network_list(self) -> list[str]:
        return [n.strip() for n in self.networks.split(",") if n.strip()]


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
            if value <= 0:
                raise ConfigurationError(f"momentum threshold '{name}' must be positive, got {value}")
        if self.volume_fade_ratio >= self.volume_acceleration_ratio:
            raise ConfigurationError("volume_fade_ratio must be below volume_acceleration_ratio")


@dataclass(frozen=True)
class AlertEngineSettings:
    """Alert dispatch behavior (Part 13 Section 4, Part 29 Section 6)."""

    cooldown_seconds: float = 900.0  # same token+type alert suppressed within this window
    score_drop_review_points: float = 15.0  # score drop vs last snapshot triggering review

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if value <= 0:
                raise ConfigurationError(f"alert setting '{name}' must be positive, got {value}")


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
            if value <= 0:
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
            if value <= 0:
                raise ConfigurationError(f"community threshold '{name}' must be positive, got {value}")


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
            if value <= 0:
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
    alert_engine: AlertEngineSettings = field(default_factory=AlertEngineSettings)
    log_level: str = "INFO"
    log_dir: str = "logs"

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
            alert_engine=_load_group(AlertEngineSettings, "ALERT_ENGINE", env),
            log_level=env.get(f"{_ENV_PREFIX}_LOG_LEVEL", "INFO"),
            log_dir=env.get(f"{_ENV_PREFIX}_LOG_DIR", "logs"),
        )


def _convert(raw: str, default: Any, key: str) -> Any:
    """Convert an env string to the type of the field's default value."""
    try:
        if isinstance(default, bool):  # bool is a subclass of int; check first
            return raw.strip().lower() in ("1", "true", "yes", "on")
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


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, loading from the environment on first use."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Clear the cached settings (used by tests and config reloads)."""
    global _settings
    _settings = None
