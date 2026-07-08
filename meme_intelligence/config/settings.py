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
    dexscreener_requests_per_minute: float = 240.0  # documented limit: 300/min
    failure_threshold: int = 3      # consecutive failures before a provider cools down
    cooldown_seconds: float = 60.0  # how long an unhealthy provider is skipped

    def __post_init__(self) -> None:
        if self.dexscreener_requests_per_minute <= 0:
            raise ConfigurationError("dexscreener_requests_per_minute must be positive")
        if self.failure_threshold < 1:
            raise ConfigurationError("failure_threshold must be >= 1")


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
