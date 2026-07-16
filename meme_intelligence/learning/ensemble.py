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
from dataclasses import dataclass
from typing import Mapping

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.learning.models import OutcomeBucket, uniform_distribution

_logger = get_logger("learning.ensemble")

# Stable source identifiers used as dict keys everywhere.
SOURCE_ANALOG = "analog"
SOURCE_LIGHTGBM = "lightgbm"
SOURCE_RUG = "rug_engine"
SOURCES = (SOURCE_ANALOG, SOURCE_LIGHTGBM, SOURCE_RUG)

# How the rug engine's per-source grade is derived. Bumped when that logic
# changes so a persisted history graded under the old rule is dropped on load
# rather than pinning the source's blend weight forever (see load()).
#   v1 — the argmax of the rug distribution (tie-broke to a fabricated 'pump'
#        below score 25, so the rug source was graded on a task it never
#        performed and its weight collapsed).
#   v2 — the rug engine abstains below its configured score and is graded as a
#        RUG call only above it (2026-07-16 mind-layer audit fix).
GRADING_VERSION = 2


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


def rug_source_label(rug_score: float, *, abstain_at_or_below: float) -> str | None:
    """The rug engine's graded opinion as a single training label, or ``None``.

    The engine speaks only to *rug vs not-rug*; it has no pump/flat/dump view.
    Above ``abstain_at_or_below`` it is calling a RUG and is graded as such.
    At or below it, the engine is *not* calling a rug and has no four-class
    opinion to grade, so it abstains (``None``) — the ensemble skips abstaining
    sources rather than scoring them wrong (Rule 8).

    This replaces ``_argmax_label(rug_distribution)``: above a rug score of 25
    the argmax was always RUG anyway (``P(rug) = score/100 > 0.25`` beats each
    ``(1-P)/3`` sibling), but at or below 25 the three non-rug labels tie and
    the argmax tie-broke to a fabricated 'pump', which graded the hard-signal
    rug source as a de-facto pump predictor and collapsed its blend weight
    (2026-07-16 audit finding). Keeping the default threshold at 25 preserves
    the identical "call a rug" behavior while turning the fabricated calls into
    honest abstentions.
    """
    if rug_score > abstain_at_or_below:
        return OutcomeBucket.RUG.value
    return None


class AdaptiveEnsemble:
    """Accuracy-weighted blender over the three sub-model sources."""

    def __init__(self, *, window: int) -> None:
        if window <= 0:
            raise ValueError("ensemble window must be positive")
        self._window = window
        # Rolling record of correctness (True/False) per source.
        self._history: dict[str, deque] = {s: deque(maxlen=window) for s in SOURCES}
        # Rolling correctness of the BLENDED verdict itself — the signal the
        # drift monitor watches (Section 7): sources can be individually fine
        # while the blend goes stale for the current meta.
        self._final_history: deque = deque(maxlen=window)

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
        *,
        final_label: str | None = None,
    ) -> None:
        """Update rolling accuracy after a coin resolves (Section 6/7).

        ``predicted_labels`` maps each source to the label it argmax-predicted
        for this coin at evaluation time (``None`` if the source abstained /
        was unavailable — such sources are skipped, not counted as wrong).
        ``final_label`` is the blended verdict's argmax at evaluation time; it
        feeds the drift monitor's ensemble-level accuracy (Section 7).
        """
        for source in SOURCES:
            predicted = predicted_labels.get(source)
            if predicted is None:
                continue
            self._history[source].append(predicted == actual_label)
        if final_label is not None:
            self._final_history.append(final_label == actual_label)

    def final_accuracy(self) -> float | None:
        """Rolling accuracy of the blended verdict; None when never graded."""
        if not self._final_history:
            return None
        return sum(1 for c in self._final_history if c) / len(self._final_history)

    @property
    def final_samples(self) -> int:
        return len(self._final_history)

    def reset_final_history(self) -> None:
        """Clear the blended-verdict history after a drift-triggered rebuild.

        Old grades measured models that no longer exist; keeping them would
        re-fire the drift trigger every cycle until the window rolled over
        (Section 7 — measure the model you're running, Rule 8).
        """
        self._final_history.clear()

    def reset_source_history(self, source: str) -> None:
        """Drop one source's rolling grades when its grading rule changes.

        With an empty window the Laplace prior gives the source a neutral 0.5
        accuracy (a ~1/3 blend voice) until it is re-measured under the new
        rule, instead of a weight pinned by grades that scored a behavior it no
        longer has. Unknown source names are ignored (Rule 18 — old artifacts
        may predate a source). Used by the grading-version migration in load().
        """
        hist = self._history.get(source)
        if hist is not None:
            hist.clear()

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
        report["ensemble_final"] = {
            "accuracy": self.final_accuracy(),
            "samples": self.final_samples,
        }
        return report

    # ---- Persistence (Section 9) ----

    def save(self, path: str) -> None:
        import joblib

        joblib.dump(
            {"window": self._window,
             "grading_version": GRADING_VERSION,
             "history": {s: list(h) for s, h in self._history.items()},
             "final_history": list(self._final_history)},
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
        # Absent in pre-drift-monitor artifacts (Rule 18 — old files still load).
        ensemble._final_history = deque(payload.get("final_history", ()),
                                        maxlen=payload["window"])
        # Grading-version migration: a history graded under an older rule for a
        # source whose grading has since changed is meaningless and would pin
        # that source's blend weight until the whole window rolled over — which,
        # for a source that now abstains most of the time, is effectively never.
        # Drop just that source's grades so it re-earns weight under the new
        # rule (analog/lightgbm/final grading is unchanged, so those stay).
        stored_version = int(payload.get("grading_version", 1))
        if stored_version < GRADING_VERSION:
            ensemble.reset_source_history(SOURCE_RUG)
            _logger.info(
                "ensemble grading upgraded v%d->v%d: rug_engine accuracy history "
                "reset so it re-earns blend weight under abstain semantics",
                stored_version, GRADING_VERSION)
        return ensemble
