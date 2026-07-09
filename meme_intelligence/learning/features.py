"""Trajectory -> fingerprint feature engineering (Section 2).

A coin's early life is a *series* of snapshots of different lengths. To make
coins comparable regardless of how long they have existed, each trajectory is
compressed into a single fixed-length feature vector — the coin's
**fingerprint** — used everywhere downstream (FAISS analog search, LightGBM,
archetype clustering).

For every base metric the extractor emits seven trajectory summaries:

* ``last``  — most recent value (where the coin is *now*)
* ``mean``  — average level over the trajectory
* ``min`` / ``max`` — the range it has traveled
* ``delta`` — net change (last minus first)
* ``slope`` — linear-regression trend, per hour (the *direction* of early life)
* ``vol``   — volatility (population standard deviation)

Plus derived per-snapshot ratios (liquidity/market-cap, volume/liquidity,
buy/sell ratio) and scalar context (age, snapshot count, price acceleration).

Design note (Rule 8 / Rule 19): a base metric entirely absent from the series
contributes ``0.0`` for its summaries and lowers the returned ``coverage``.
We do not fabricate plausible values — instead ``coverage`` tells downstream
confidence how much of the fingerprint was backed by real observations, and
the persisted :class:`StandardScaler` centers real features so a genuine 0
level and an absent metric are handled consistently. Richer imputation is a
deliberate future refinement; a neutral fill keeps distances defined without
inventing signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.learning.models import CoinSnapshot

_logger = get_logger("learning.features")

# The seven summary statistics computed per base metric, in stable order.
_SUMMARY_STATS = ("last", "mean", "min", "max", "delta", "slope", "vol")

# Base metrics summarized across the trajectory. Each accessor pulls the raw
# per-snapshot value (or None). Derived ratios are appended below.
_BaseAccessor = Callable[[CoinSnapshot], float | None]


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _buy_sell_ratio(snap: CoinSnapshot) -> float | None:
    if snap.buys is None or snap.sells is None:
        return None
    total = snap.buys + snap.sells
    if total == 0:
        return None
    return snap.buys / total


def _tx_count(snap: CoinSnapshot) -> float | None:
    if snap.buys is None and snap.sells is None:
        return None
    return float((snap.buys or 0) + (snap.sells or 0))


# (metric_name, accessor) — order defines the fingerprint layout and is stable.
_BASE_METRICS: tuple[tuple[str, _BaseAccessor], ...] = (
    ("price", lambda s: s.price_usd),
    ("liquidity", lambda s: s.liquidity_usd),
    ("market_cap", lambda s: s.market_cap_usd),
    ("volume_5m", lambda s: s.volume_5m_usd),
    ("volume_1h", lambda s: s.volume_1h_usd),
    ("holders", lambda s: None if s.holder_count is None else float(s.holder_count)),
    ("top10_concentration", lambda s: s.top10_holder_percent),
    ("dev_outflow", lambda s: s.dev_outflow_usd),
    ("liquidity_event", lambda s: s.liquidity_event_usd),
    ("liq_to_mcap", lambda s: _ratio(s.liquidity_usd, s.market_cap_usd)),
    ("vol_to_liq", lambda s: _ratio(s.volume_1h_usd, s.liquidity_usd)),
    ("buy_sell_ratio", _buy_sell_ratio),
    ("tx_count", _tx_count),
)

# Scalar context features appended after the per-metric summaries.
_SCALAR_FEATURES = ("age_hours", "snapshot_count", "price_acceleration")


def _feature_names() -> tuple[str, ...]:
    names: list[str] = []
    for metric, _ in _BASE_METRICS:
        for stat in _SUMMARY_STATS:
            names.append(f"{metric}_{stat}")
    names.extend(_SCALAR_FEATURES)
    return tuple(names)


FEATURE_NAMES: tuple[str, ...] = _feature_names()
FEATURE_DIM: int = len(FEATURE_NAMES)


@dataclass(frozen=True)
class Fingerprint:
    """A coin's fixed-length fingerprint and how much of it was real.

    ``vector`` is length :data:`FEATURE_DIM`, aligned with
    :data:`FEATURE_NAMES`. ``coverage`` is the fraction of base metrics that
    had at least one real observation — feeds cold-start / low-confidence
    handling (Section 11).
    """

    vector: np.ndarray
    coverage: float


def _slope_per_hour(ages_seconds: np.ndarray, values: np.ndarray) -> float:
    """Least-squares slope of value vs time, expressed per hour.

    Returns 0.0 when there are fewer than two points or the timestamps do
    not vary (a vertical fit is undefined — no trend is observable, not an
    error, Rule 6).
    """
    if values.size < 2:
        return 0.0
    t = ages_seconds.astype(float)
    t_var = t.var()
    if t_var == 0.0:
        return 0.0
    slope_per_second = float(np.cov(t, values, bias=True)[0, 1] / t_var)
    return slope_per_second * 3600.0


def _summarize(ages: np.ndarray, values: np.ndarray) -> list[float]:
    """The seven summary stats for one base metric over present points."""
    if values.size == 0:
        return [0.0] * len(_SUMMARY_STATS)
    return [
        float(values[-1]),                       # last
        float(values.mean()),                    # mean
        float(values.min()),                     # min
        float(values.max()),                     # max
        float(values[-1] - values[0]),           # delta
        _slope_per_hour(ages, values),           # slope (per hour)
        float(values.std()),                     # vol (population std)
    ]


def _price_acceleration(ages: np.ndarray, prices: np.ndarray) -> float:
    """Change in price velocity across the trajectory (Section 2).

    Approximated as the slope of consecutive per-snapshot returns: positive
    means the pump is *accelerating*, negative means it is stalling. Needs
    at least three price points to have two velocities to compare.
    """
    if prices.size < 3:
        return 0.0
    prev = prices[:-1]
    # Guard against zero prices producing inf returns.
    safe_prev = np.where(prev == 0.0, np.nan, prev)
    returns = (prices[1:] - prev) / safe_prev
    mid_ages = ages[1:]
    mask = np.isfinite(returns)
    if mask.sum() < 2:
        return 0.0
    return _slope_per_hour(mid_ages[mask], returns[mask])


class FingerprintExtractor:
    """Compresses a snapshot series into a fixed-length fingerprint vector.

    Stateless and deterministic: the same series always yields the same
    vector, so a coin re-embedded later lands in the same place in analog
    space (a requirement for stable neighbor search).
    """

    feature_names: tuple[str, ...] = FEATURE_NAMES
    dim: int = FEATURE_DIM

    def extract(self, series: Sequence[CoinSnapshot | dict]) -> Fingerprint:
        """Build the fingerprint for a trajectory-so-far.

        Accepts either :class:`CoinSnapshot` objects or the loosely-typed
        dicts of the public API. An empty series yields a zero vector with
        zero coverage (nothing observed yet) rather than raising (Rule 6).
        """
        snaps = [s if isinstance(s, CoinSnapshot) else CoinSnapshot.from_dict(s)
                 for s in series]
        snaps.sort(key=lambda s: s.age_seconds)

        if not snaps:
            return Fingerprint(vector=np.zeros(self.dim, dtype=np.float32), coverage=0.0)

        ages_all = np.array([s.age_seconds for s in snaps], dtype=float)
        features: list[float] = []
        present_metrics = 0

        for _, accessor in _BASE_METRICS:
            pairs = [(age, accessor(s)) for age, s in zip(ages_all, snaps)]
            present = [(age, val) for age, val in pairs if val is not None]
            if present:
                present_metrics += 1
                ages = np.array([p[0] for p in present], dtype=float)
                values = np.array([p[1] for p in present], dtype=float)
                features.extend(_summarize(ages, values))
            else:
                features.extend([0.0] * len(_SUMMARY_STATS))

        # Scalar context.
        age_hours = float(ages_all[-1]) / 3600.0
        snapshot_count = float(len(snaps))
        price_pairs = [(age, s.price_usd) for age, s in zip(ages_all, snaps)
                       if s.price_usd is not None]
        if len(price_pairs) >= 3:
            p_ages = np.array([p[0] for p in price_pairs], dtype=float)
            p_vals = np.array([p[1] for p in price_pairs], dtype=float)
            accel = _price_acceleration(p_ages, p_vals)
        else:
            accel = 0.0
        features.extend([age_hours, snapshot_count, accel])

        vector = np.array(features, dtype=np.float32)
        # Non-finite guards: a stray inf/nan anywhere would poison FAISS
        # distances and LightGBM alike — neutralize to 0 (Rule 6).
        vector = np.nan_to_num(vector, nan=0.0, posinf=0.0, neginf=0.0)
        coverage = present_metrics / len(_BASE_METRICS)
        return Fingerprint(vector=vector, coverage=coverage)


class StandardScalerBundle:
    """A persisted :class:`sklearn.preprocessing.StandardScaler` for fingerprints.

    Fingerprint features live on wildly different scales (USD liquidity vs a
    0-1 buy ratio); unscaled, Euclidean/L2 distance would be dominated by the
    largest-magnitude column. Fitting a scaler on the historical fingerprint
    set and persisting it (Section 9) makes analog distances meaningful and
    stable across restarts.

    Before it has been fit (cold start) :meth:`transform` returns the raw
    vector unchanged and reports ``is_fitted == False`` so callers can lower
    confidence rather than trust distances in an unscaled space.
    """

    def __init__(self) -> None:
        # Imported lazily so the data model / config import cheaply and a
        # missing scientific stack surfaces only when the mind layer runs.
        from sklearn.preprocessing import StandardScaler

        self._scaler = StandardScaler()
        self._fitted = False
        self._dim = FEATURE_DIM
        self._logger = get_logger("learning.scaler")

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def fit(self, vectors: np.ndarray) -> None:
        """Fit (or re-fit) the scaler on a matrix of fingerprints (N x dim)."""
        matrix = np.asarray(vectors, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != self._dim:
            raise ValueError(
                f"expected an (N, {self._dim}) fingerprint matrix, got {matrix.shape}")
        if matrix.shape[0] == 0:
            self._logger.warning("scaler fit skipped: no fingerprints provided")
            return
        self._scaler.fit(matrix)
        self._fitted = True
        self._logger.info("scaler fitted on %d fingerprints", matrix.shape[0])

    def transform(self, vector: np.ndarray) -> np.ndarray:
        """Scale one fingerprint. Identity passthrough until fitted."""
        vec = np.asarray(vector, dtype=np.float64).reshape(1, -1)
        if not self._fitted:
            return vec.astype(np.float32).ravel()
        scaled = self._scaler.transform(vec)
        return np.nan_to_num(scaled.astype(np.float32).ravel(),
                             nan=0.0, posinf=0.0, neginf=0.0)

    def transform_many(self, vectors: np.ndarray) -> np.ndarray:
        matrix = np.asarray(vectors, dtype=np.float64)
        if not self._fitted:
            return matrix.astype(np.float32)
        scaled = self._scaler.transform(matrix)
        return np.nan_to_num(scaled.astype(np.float32),
                             nan=0.0, posinf=0.0, neginf=0.0)

    def save(self, path: str) -> None:
        import joblib

        joblib.dump({"scaler": self._scaler, "fitted": self._fitted, "dim": self._dim}, path)

    @classmethod
    def load(cls, path: str) -> "StandardScalerBundle":
        import joblib

        payload = joblib.load(path)
        bundle = cls()
        bundle._scaler = payload["scaler"]
        bundle._fitted = payload["fitted"]
        bundle._dim = payload.get("dim", FEATURE_DIM)
        return bundle
