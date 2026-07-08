"""Core data models shared across the system (Spec Parts 1, 2, 7, 31).

Missing data is always represented as ``None`` — never a fabricated value
(Rule 8). Score aggregation reports *coverage* so downstream confidence
ratings can reflect how much of the framework was actually backed by data.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from meme_intelligence.config.settings import ClassificationBands, ScoringWeights
from meme_intelligence.core.enums import Classification
from meme_intelligence.core.errors import InsufficientDataError


@dataclass(frozen=True)
class TokenIdentity:
    """Minimal identity of a token: chain + contract address, plus display info."""

    chain: str
    address: str
    name: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class DexPair:
    """A normalized DEX trading pair snapshot (Spec Part 2 — DexScreener fields).

    Every metric is optional: providers frequently omit fields for young
    pairs, and absence must stay observable rather than default to zero.
    """

    chain: str
    pair_address: str
    base_token: TokenIdentity
    dex_id: str | None = None
    quote_symbol: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    volume_24h: float | None = None
    price_change_24h: float | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    pair_created_at: datetime | None = None
    url: str | None = None


@dataclass(frozen=True)
class CategoryScores:
    """Per-category scores on a 0-100 scale; ``None`` means "not yet analyzed".

    Field names match :class:`~meme_intelligence.config.settings.ScoringWeights`
    one-to-one so the two can be zipped mechanically.
    """

    foundation: float | None = None
    security: float | None = None
    community: float | None = None
    blockchain: float | None = None
    momentum: float | None = None
    narrative: float | None = None
    timing: float | None = None

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if value is not None and not (0.0 <= value <= 100.0):
                raise ValueError(f"category score '{name}' must be within 0-100, got {value}")


@dataclass(frozen=True)
class WeightedScoreResult:
    """Outcome of combining category scores with the framework weights.

    ``coverage`` is the fraction of total weight backed by real data (1.0 =
    every category was scored). Low coverage should lower confidence, not
    silently produce an authoritative-looking number.
    """

    total: float
    coverage: float
    missing: tuple[str, ...]


def compute_weighted_score(scores: CategoryScores, weights: ScoringWeights) -> WeightedScoreResult:
    """Combine category scores using the framework weights (Part 31, Section 4).

    Categories without data are excluded and the remaining weights are
    renormalized, so a partially-analyzed token still gets a comparable
    0-100 total — with ``coverage`` exposing how partial it was.

    Raises :class:`InsufficientDataError` when no category has been scored.
    """
    score_map = dataclasses.asdict(scores)
    weight_map = dataclasses.asdict(weights)

    available_weight = 0.0
    weighted_sum = 0.0
    missing: list[str] = []
    for name, weight in weight_map.items():
        value = score_map[name]
        if value is None:
            missing.append(name)
        else:
            available_weight += weight
            weighted_sum += value * weight

    if available_weight == 0.0:
        raise InsufficientDataError("cannot compute a weighted score: no category has been scored")

    return WeightedScoreResult(
        total=weighted_sum / available_weight,
        coverage=available_weight,
        missing=tuple(missing),
    )


def classify(total_score: float, bands: ClassificationBands) -> Classification:
    """Map a final 0-100 score onto its classification band (Part 20, Section 4).

    This is pure banding. Red-flag overrides (confirmed honeypot, removable
    liquidity, ...) are applied by the scoring engine before this is called
    and force :attr:`Classification.AVOID` regardless of score (Part 10, Section 5).
    """
    if not (0.0 <= total_score <= 100.0):
        raise ValueError(f"total score must be within 0-100, got {total_score}")
    if total_score >= bands.elite:
        return Classification.ELITE_OPPORTUNITY
    if total_score >= bands.strong_candidate:
        return Classification.STRONG_CANDIDATE
    if total_score >= bands.watchlist:
        return Classification.WATCHLIST
    if total_score >= bands.speculative:
        return Classification.SPECULATIVE
    return Classification.AVOID
