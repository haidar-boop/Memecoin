"""Self-evaluation metrics — proof the system is improving (Section 8).

These are the numbers that let an operator *watch the mind layer get smarter*
over rolling windows, and they feed the adaptive ensemble weights (Section 6).
Everything is computed from resolved prediction records — a coin's verdict at
evaluation time paired with the outcome it actually reached.

Following Rule 8, every metric carries its sample size and a metric with no
supporting data is reported as ``None`` rather than a falsely-precise number.
Interpretation is left to the reader: for a *risk* signal a low forward return
is the signal working, so the module measures rather than editorializes.

Metrics (Section 8):

* directional hit-rate — did PUMP/DUMP calls resolve correctly,
* rug precision / recall / F1 — of everything flagged RUG, how many rugged;
  of all rugs, how many were caught,
* Brier score — are the probabilities honest (lower = better calibrated),
* confidence calibration — predicted confidence vs empirical accuracy per bin,
* resolved-sample count — raw learning progress,
* per-archetype accuracy — where the system is strong vs blind,
* novelty-flag hit-rate — do high-novelty coins really behave unfamiliarly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from meme_intelligence.learning.models import OutcomeBucket

_LABELS = [b.value for b in OutcomeBucket.training_labels()]
_RUG = OutcomeBucket.RUG.value


@dataclass(frozen=True)
class PredictionRecord:
    """One resolved prediction: what the layer said vs what happened.

    ``predicted_distribution`` is the final ensemble distribution over the four
    labels at evaluation time; ``predicted_label`` its argmax; ``actual_label``
    the resolved outcome bucket. ``archetype`` and ``novelty_flagged`` capture
    the archetype assignment made at evaluation time (Section 3).
    """

    predicted_distribution: dict[str, float]
    predicted_label: str
    actual_label: str
    archetype: str | None = None
    novelty_flagged: bool = False


def _safe_div(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _directional_hit_rate(records: Sequence[PredictionRecord]) -> dict:
    directional = [r for r in records if r.predicted_label in (OutcomeBucket.PUMP.value,
                                                               OutcomeBucket.DUMP.value)]
    correct = sum(1 for r in directional if r.predicted_label == r.actual_label)
    return {"hit_rate": _safe_div(correct, len(directional)),
            "samples": len(directional)}


def _rug_prf(records: Sequence[PredictionRecord]) -> dict:
    tp = sum(1 for r in records if r.predicted_label == _RUG and r.actual_label == _RUG)
    fp = sum(1 for r in records if r.predicted_label == _RUG and r.actual_label != _RUG)
    fn = sum(1 for r in records if r.predicted_label != _RUG and r.actual_label == _RUG)
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1,
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "actual_rugs": tp + fn}


def _brier_score(records: Sequence[PredictionRecord]) -> float | None:
    if not records:
        return None
    total = 0.0
    for r in records:
        for label in _LABELS:
            p = float(r.predicted_distribution.get(label, 0.0))
            y = 1.0 if r.actual_label == label else 0.0
            total += (p - y) ** 2
    return total / len(records)


def _calibration(records: Sequence[PredictionRecord], bins: int = 5) -> list[dict]:
    """Predicted confidence vs empirical accuracy, per confidence bin."""
    buckets: list[dict] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        in_bin = [r for r in records
                  if lo <= max(r.predicted_distribution.values(), default=0.0) < hi
                  or (i == bins - 1 and max(r.predicted_distribution.values(), default=0.0) == 1.0)]
        if not in_bin:
            continue
        # default=0.0 matches the bin-membership expression above — a record
        # with an EMPTY distribution lands in the first bin and must not crash
        # the mean (fuzz finding: max() on an empty sequence).
        mean_conf = sum(max(r.predicted_distribution.values(), default=0.0)
                        for r in in_bin) / len(in_bin)
        accuracy = sum(1 for r in in_bin if r.predicted_label == r.actual_label) / len(in_bin)
        buckets.append({"bin": f"{lo:.1f}-{hi:.1f}", "mean_confidence": mean_conf,
                        "empirical_accuracy": accuracy, "samples": len(in_bin)})
    return buckets


def _per_archetype(records: Sequence[PredictionRecord]) -> dict:
    groups: dict[str, list[PredictionRecord]] = {}
    for r in records:
        if r.archetype is None:
            continue
        groups.setdefault(r.archetype, []).append(r)
    return {
        name: {"accuracy": sum(1 for r in rs if r.predicted_label == r.actual_label) / len(rs),
               "samples": len(rs)}
        for name, rs in groups.items()
    }


def _novelty_hit_rate(records: Sequence[PredictionRecord]) -> dict:
    """Do novelty-flagged coins behave more unfamiliarly than the baseline?

    Measured as misprediction rate among flagged coins vs the overall baseline.
    A *higher* flagged misprediction rate means the novelty flag is meaningful:
    high-novelty coins are genuinely harder to call (an emerging pattern the
    system hasn't learned), which is exactly the signal the flag is meant to
    surface (Section 3). Reported as measurements; the reader judges.
    """
    flagged = [r for r in records if r.novelty_flagged]
    flagged_miss = _safe_div(
        sum(1 for r in flagged if r.predicted_label != r.actual_label), len(flagged))
    baseline_miss = _safe_div(
        sum(1 for r in records if r.predicted_label != r.actual_label), len(records))
    return {"flagged_count": len(flagged),
            "flagged_misprediction_rate": flagged_miss,
            "baseline_misprediction_rate": baseline_miss}


def compute_metrics(records: Sequence[PredictionRecord]) -> dict:
    """Full self-evaluation metric set over resolved prediction records."""
    records = list(records)
    overall_correct = sum(1 for r in records if r.predicted_label == r.actual_label)
    return {
        "resolved_count": len(records),
        "overall_accuracy": _safe_div(overall_correct, len(records)),
        "directional": _directional_hit_rate(records),
        "rug": _rug_prf(records),
        "brier_score": _brier_score(records),
        "calibration": _calibration(records),
        "per_archetype": _per_archetype(records),
        "novelty": _novelty_hit_rate(records),
    }
