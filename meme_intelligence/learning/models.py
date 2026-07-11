"""Data model for the self-learning mind layer (Sections 1, 5, 10).

Every coin the scanner surfaces enters a lifecycle: detection -> repeated
trajectory snapshots -> outcome resolution at each horizon -> a labeled
training example. The dataclasses here describe that lifecycle and the
structured verdict the public API returns.

Consistent with the rest of the codebase, missing data is always ``None``
and never a fabricated value (Rule 8): a young coin with a two-snapshot
history is represented honestly, not padded with zeros that would read as
real observations.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from meme_intelligence.core.models import TokenIdentity


class OutcomeBucket(str, Enum):
    """Resolved outcome label for a coin at a given horizon (Section 1).

    ``RUG`` overrides the return-based buckets: a confirmed rug event is a
    rug regardless of the price change that accompanied it. ``UNRESOLVED``
    is the pre-resolution state — never used as a training label, only as a
    lifecycle marker.
    """

    PUMP = "pump"
    FLAT = "flat"
    DUMP = "dump"
    RUG = "rug"
    UNRESOLVED = "unresolved"

    @property
    def is_resolved(self) -> bool:
        return self is not OutcomeBucket.UNRESOLVED

    @classmethod
    def training_labels(cls) -> tuple["OutcomeBucket", ...]:
        """The four buckets a classifier/analog vote can produce (Section 4)."""
        return (cls.PUMP, cls.FLAT, cls.DUMP, cls.RUG)


def _coerce_float(value: Any) -> float | None:
    """Best-effort float conversion; ``None``/blank/non-finite -> ``None``.

    Snapshots arrive as loosely-typed dicts from the scanner/dashboard
    (Section 10). A missing or malformed field must degrade to ``None``
    rather than crash the pipeline (Rule 6).
    """
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _coerce_int(value: Any) -> int | None:
    result = _coerce_float(value)
    return None if result is None else int(result)


@dataclass(frozen=True)
class CoinSnapshot:
    """One point-in-time observation of a coin's early life (Section 2).

    The *series* of these — not any single frozen moment — is the signal:
    the fingerprint extractor summarizes their shape (slope, volatility,
    acceleration, ...) into a fixed-length vector. Every metric is optional
    because providers omit fields for very young pairs.

    ``age_seconds`` is the coin's age at the moment of the snapshot (seconds
    since detection / pair creation); it anchors the trajectory in time so
    slopes are per-second and comparable across coins.
    """

    age_seconds: float
    captured_at: datetime | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    holder_count: int | None = None
    buys: int | None = None
    sells: int | None = None
    top10_holder_percent: float | None = None      # % of supply held by top 10
    dev_outflow_usd: float | None = None            # creator wallet outflow so far
    liquidity_event_usd: float | None = None        # net LP add(+)/remove(-) magnitude

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoinSnapshot":
        """Build a snapshot from a loosely-typed dict (the public API shape).

        Unknown keys are ignored and missing values become ``None`` so the
        dashboard can pass whatever it has without breaking the layer
        (Rule 6). ``age_seconds`` falls back to 0.0 only when entirely
        absent — callers should supply it, but a first snapshot at detection
        legitimately has age ~0.
        """
        age = _coerce_float(data.get("age_seconds"))
        captured = data.get("captured_at")
        if isinstance(captured, str):
            try:
                captured = datetime.fromisoformat(captured)
            except ValueError:
                captured = None
        elif not isinstance(captured, datetime):
            captured = None
        return cls(
            age_seconds=age if age is not None else 0.0,
            captured_at=captured,
            price_usd=_coerce_float(data.get("price_usd")),
            liquidity_usd=_coerce_float(data.get("liquidity_usd")),
            market_cap_usd=_coerce_float(data.get("market_cap_usd")),
            volume_5m_usd=_coerce_float(data.get("volume_5m_usd")),
            volume_1h_usd=_coerce_float(data.get("volume_1h_usd")),
            holder_count=_coerce_int(data.get("holder_count")),
            buys=_coerce_int(data.get("buys")),
            sells=_coerce_int(data.get("sells")),
            top10_holder_percent=_coerce_float(data.get("top10_holder_percent")),
            dev_outflow_usd=_coerce_float(data.get("dev_outflow_usd")),
            liquidity_event_usd=_coerce_float(data.get("liquidity_event_usd")),
        )


@dataclass(frozen=True)
class OutcomeLabel:
    """A resolved forward outcome at one horizon (Section 1).

    ``forward_return_percent`` is the price change vs the detection price at
    ``horizon_hours``. ``bucket`` is the assigned label. When a rug fired,
    ``bucket`` is :attr:`OutcomeBucket.RUG` and the return is recorded but
    does not decide the bucket.
    """

    horizon_hours: float
    bucket: OutcomeBucket
    forward_return_percent: float | None
    resolved_at: datetime | None = None


@dataclass(frozen=True)
class RugSignal:
    """One fired hard rug signal and the points it contributed (Section 5a)."""

    name: str
    points: float
    detail: str | None = None


@dataclass(frozen=True)
class RugAssessment:
    """Merged output of the hard-signal rug engine (Section 5a).

    ``score`` is the 0-100 rug-risk score (summed signal points, clamped).
    ``signals`` are the individual signals that fired, so an alert can
    explain *why* (Rule 8 — legible evidence, not an opaque number).
    """

    score: float
    signals: tuple[RugSignal, ...] = ()

    @property
    def fired_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.signals)


@dataclass(frozen=True)
class AnalogNeighbor:
    """A past coin the live coin resembles, and how it resolved (Section 3)."""

    address: str
    chain: str
    similarity: float          # 0-1, higher = more similar
    resolved_as: OutcomeBucket
    age_days: float            # age of the analog's resolution, for recency

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "chain": self.chain,
            "similarity": round(self.similarity, 4),
            "resolved_as": self.resolved_as.value,
            "age_days": round(self.age_days, 2),
        }


@dataclass(frozen=True)
class CoinRecord:
    """A coin's full lifecycle record, persisted for learning (Section 1/9)."""

    token: TokenIdentity
    detected_at: datetime
    detection_price_usd: float | None
    creator: str | None = None
    snapshots: tuple[CoinSnapshot, ...] = ()
    labels: tuple[OutcomeLabel, ...] = ()
    rug_signals: tuple[RugSignal, ...] = ()

    def label_for(self, horizon_hours: float) -> OutcomeLabel | None:
        for label in self.labels:
            if math.isclose(label.horizon_hours, horizon_hours):
                return label
        return None

    @property
    def final_bucket(self) -> OutcomeBucket:
        """The record's training label: RUG if any horizon rugged, else the
        longest-horizon resolved bucket, else UNRESOLVED (Section 1)."""
        resolved = [lb for lb in self.labels if lb.bucket.is_resolved]
        if not resolved:
            return OutcomeBucket.UNRESOLVED
        for label in resolved:
            if label.bucket is OutcomeBucket.RUG:
                return OutcomeBucket.RUG
        return max(resolved, key=lambda lb: lb.horizon_hours).bucket


@dataclass
class CoinVerdict:
    """The structured verdict returned per live coin (Section 10).

    A mutable dataclass assembled by the ensemble as each sub-model reports.
    :meth:`to_dict` renders the exact public contract shape.
    """

    token_address: str
    chain: str
    final_probabilities: dict[str, float] = field(default_factory=dict)
    rug_risk_score: float = 0.0
    rug_signals_fired: list[str] = field(default_factory=list)
    nearest_analogs: list[AnalogNeighbor] = field(default_factory=list)
    matched_archetype: str | None = None
    novelty_score: float | None = None
    ensemble_weights: dict[str, float] = field(default_factory=dict)
    model_confidence: float = 0.0
    sample_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_address": self.token_address,
            "chain": self.chain,
            "final_probabilities": {k: round(v, 4) for k, v in self.final_probabilities.items()},
            "rug_risk_score": round(self.rug_risk_score),
            "rug_signals_fired": list(self.rug_signals_fired),
            "nearest_analogs": [n.to_dict() for n in self.nearest_analogs],
            "matched_archetype": self.matched_archetype,
            "novelty_score": None if self.novelty_score is None else round(self.novelty_score, 4),
            "ensemble_weights": {k: round(v, 4) for k, v in self.ensemble_weights.items()},
            "model_confidence": round(self.model_confidence, 4),
            "sample_size": self.sample_size,
        }


def uniform_distribution() -> dict[str, float]:
    """An honest maximally-uncertain prior over the four training labels.

    Used at cold start before any coin has resolved (Section 11): the layer
    says "I don't know yet" as 0.25 each rather than inventing confidence.
    """
    labels = OutcomeBucket.training_labels()
    weight = 1.0 / len(labels)
    return {label.value: weight for label in labels}
