"""Adaptive accuracy-weighted ensemble (Section 6).

Blends three probability sources into the final verdict:

1. the analog / k-NN outcome distribution (Section 3),
2. the LightGBM class probabilities (Section 4), and
3. the rug engine (Section 5), which contributes primarily to ``RUG``.

The blend weights are **not fixed**. Each source's recent accuracy is tracked
over a rolling window of the last M resolved coins, and

    weight(source) = recent_accuracy(source) / Σ recent_accuracy(all sources)

so a sub-model that proves more reliable in the current regime automatically
earns more influence — a second "gets smarter" loop layered on the two
learners (Section 6 / Section 7).

Accuracy is Laplace-smoothed ((correct + 1) / (total + 2)): a brand-new source
starts at 0.5 rather than 0, so it is never permanently locked out before it
has had a chance to be measured, and a source on a short unlucky streak keeps
a small voice instead of dropping to a hard zero.

A source that is unavailable for a given coin (the classifier at cold start, or
an analog engine with too few neighbors) is passed as ``None`` and simply
excluded from that coin's blend; the remaining weights renormalize. When *no*
source is available the ensemble returns an honest uniform distribution with
``abstained=True`` rather than a fabricated call (Rule 8).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.learning.models import OutcomeBucket, uniform_distribution

_logger = get_logger("learning.ensemble")

# Stable source identifiers used as dict keys everywhere.
SOURCE_ANALOG = "analog"
SOURCE_LIGHTGBM = "lightgbm"
SOURCE_RUG = "rug_engine"
SOURCES = (SOURCE_ANALOG, SOURCE_LIGHTGBM, SOURCE_RUG)


@dataclass(frozen=True)
class EnsembleResult:
    """Blended outcome distribution plus the weights that produced it."""

    distribution: dict[str, float]
    weights: dict[str, float]
    confidence: float                 # max blended class probability
    sources_used: tuple[str, ...]
    abstained: bool


def rug_score_to_distribution(rug_score: float) -> dict[str, float]:
    """Turn a 0-100 rug-risk score into an outcome distribution (Section 6).

    The rug engine has an opinion about *rug vs not-rug* only; it says nothing
    about pump/flat/dump. So ``P(rug) = score / 100`` and the remaining mass is
    spread uniformly over the other three labels — the engine influences the
    RUG probability strongly while staying neutral elsewhere (Rule 8).
    """
    r = min(1.0, max(0.0, rug_score / 100.0))
    others = (1.0 - r) / 3.0
    return {
        OutcomeBucket.PUMP.value: others,
        OutcomeBucket.FLAT.value: others,
        OutcomeBucket.DUMP.value: others,
        OutcomeBucket.RUG.value: r,
    }


class AdaptiveEnsemble:
    """Accuracy-weighted blender over the three sub-model sources."""

    def __init__(self, *, window: int) -> None:
        if window <= 0:
            raise ValueError("ensemble window must be positive")
        self._window = window
        # Rolling record of correctness (True/False) per source.
        self._history: dict[str, deque] = {s: deque(maxlen=window) for s in SOURCES}

    def source_accuracy(self, source: str) -> float:
        """Laplace-smoothed recent accuracy for a source (0.5 with no data)."""
        hist = self._history.get(source)
        if not hist:
            return 0.5
        correct = sum(1 for c in hist if c)
        return (correct + 1) / (len(hist) + 2)

    def raw_accuracy(self, source: str) -> float | None:
        """Unsmoothed accuracy for reporting; None when never measured."""
        hist = self._history.get(source)
        if not hist:
            return None
        return sum(1 for c in hist if c) / len(hist)

    def weights(self, sources: tuple[str, ...]) -> dict[str, float]:
        """Normalized blend weights over the given available sources."""
        raw = {s: self.source_accuracy(s) for s in sources}
        total = sum(raw.values())
        if total <= 0.0:  # unreachable with Laplace smoothing, but stay safe
            equal = 1.0 / len(sources)
            return {s: equal for s in sources}
        return {s: w / total for s, w in raw.items()}

    def blend(self, distributions: Mapping[str, dict[str, float] | None]) -> EnsembleResult:
        """Blend the available source distributions into the final verdict."""
        available = {s: d for s, d in distributions.items()
                     if d is not None and s in SOURCES}
        labels = [b.value for b in OutcomeBucket.training_labels()]

        if not available:
            uniform = uniform_distribution()
            return EnsembleResult(distribution=uniform, weights={},
                                  confidence=max(uniform.values()),
                                  sources_used=(), abstained=True)

        sources = tuple(available.keys())
        weights = self.weights(sources)
        final = {label: 0.0 for label in labels}
        for source, dist in available.items():
            w = weights[source]
            for label in labels:
                final[label] += w * float(dist.get(label, 0.0))

        # Renormalize defensively (source dists should already sum to 1).
        total = sum(final.values())
        if total > 0.0:
            final = {label: value / total for label, value in final.items()}
        else:
            final = uniform_distribution()

        return EnsembleResult(
            distribution=final,
            weights=weights,
            confidence=max(final.values()),
            sources_used=sources,
            abstained=False,
        )

    def record_outcome(
        self,
        predicted_labels: Mapping[str, str | None],
        actual_label: str,
    ) -> None:
        """Update rolling accuracy after a coin resolves (Section 6/7).

        ``predicted_labels`` maps each source to the label it argmax-predicted
        for this coin at evaluation time (``None`` if the source abstained /
        was unavailable — such sources are skipped, not counted as wrong).
        """
        for source in SOURCES:
            predicted = predicted_labels.get(source)
            if predicted is None:
                continue
            self._history[source].append(predicted == actual_label)

    def accuracy_report(self) -> dict[str, dict]:
        """Per-source accuracy + sample size, for the metrics dashboard."""
        report: dict[str, dict] = {}
        for source in SOURCES:
            hist = self._history[source]
            report[source] = {
                "accuracy": self.raw_accuracy(source),
                "samples": len(hist),
                "smoothed_weight_basis": self.source_accuracy(source),
            }
        return report

    # ---- Persistence (Section 9) ----

    def save(self, path: str) -> None:
        import joblib

        joblib.dump(
            {"window": self._window,
             "history": {s: list(h) for s, h in self._history.items()}},
            path,
        )

    @classmethod
    def load(cls, path: str) -> "AdaptiveEnsemble":
        import joblib

        payload = joblib.load(path)
        ensemble = cls(window=payload["window"])
        for source, entries in payload["history"].items():
            if source in ensemble._history:
                ensemble._history[source] = deque(entries, maxlen=payload["window"])
        return ensemble
