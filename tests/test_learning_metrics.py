"""Tests for the self-evaluation metrics (Section 8)."""

import pytest

from meme_intelligence.learning.metrics import PredictionRecord, compute_metrics


def _rec(pred, actual, dist=None, archetype=None, novel=False):
    if dist is None:
        dist = {"pump": 0.0, "flat": 0.0, "dump": 0.0, "rug": 0.0}
        dist[pred] = 1.0
    return PredictionRecord(predicted_distribution=dist, predicted_label=pred,
                            actual_label=actual, archetype=archetype, novelty_flagged=novel)


def test_empty_records_report_none_with_zero_samples():
    m = compute_metrics([])
    assert m["resolved_count"] == 0
    assert m["overall_accuracy"] is None
    assert m["directional"]["hit_rate"] is None
    assert m["rug"]["precision"] is None
    assert m["brier_score"] is None


def test_directional_hit_rate():
    records = [
        _rec("pump", "pump"),   # correct directional
        _rec("pump", "dump"),   # wrong directional
        _rec("dump", "dump"),   # correct directional
        _rec("flat", "flat"),   # not directional, excluded
    ]
    m = compute_metrics(records)
    assert m["directional"]["samples"] == 3
    assert m["directional"]["hit_rate"] == pytest.approx(2 / 3)


def test_rug_precision_recall_f1():
    records = [
        _rec("rug", "rug"),    # TP
        _rec("rug", "rug"),    # TP
        _rec("rug", "pump"),   # FP
        _rec("dump", "rug"),   # FN
        _rec("flat", "flat"),  # TN
    ]
    m = compute_metrics(records)["rug"]
    assert m["true_positives"] == 2
    assert m["false_positives"] == 1
    assert m["false_negatives"] == 1
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3)
    assert m["actual_rugs"] == 3


def test_brier_score_rewards_calibration():
    confident_right = [_rec("pump", "pump")] * 5          # perfect -> brier 0
    confident_wrong = [_rec("pump", "dump")] * 5          # perfectly wrong -> brier 2
    assert compute_metrics(confident_right)["brier_score"] == pytest.approx(0.0)
    assert compute_metrics(confident_wrong)["brier_score"] == pytest.approx(2.0)


def test_per_archetype_accuracy():
    records = [
        _rec("rug", "rug", archetype="rug_archetype_0"),
        _rec("rug", "pump", archetype="rug_archetype_0"),
        _rec("pump", "pump", archetype="pump_archetype_1"),
        _rec("flat", "flat", archetype=None),  # excluded (no archetype)
    ]
    per = compute_metrics(records)["per_archetype"]
    assert per["rug_archetype_0"]["samples"] == 2
    assert per["rug_archetype_0"]["accuracy"] == pytest.approx(0.5)
    assert per["pump_archetype_1"]["accuracy"] == pytest.approx(1.0)
    assert "unassigned" not in per and None not in per


def test_novelty_hit_rate_measures_flagged_vs_baseline():
    records = [
        _rec("pump", "rug", novel=True),    # flagged, wrong
        _rec("pump", "dump", novel=True),   # flagged, wrong
        _rec("pump", "pump", novel=False),  # not flagged, right
        _rec("dump", "dump", novel=False),  # not flagged, right
    ]
    nov = compute_metrics(records)["novelty"]
    assert nov["flagged_count"] == 2
    assert nov["flagged_misprediction_rate"] == pytest.approx(1.0)
    assert nov["baseline_misprediction_rate"] == pytest.approx(0.5)


def test_overall_accuracy():
    records = [_rec("pump", "pump"), _rec("dump", "pump")]
    assert compute_metrics(records)["overall_accuracy"] == pytest.approx(0.5)


def test_calibration_bins_present():
    records = [_rec("pump", "pump", dist={"pump": 0.9, "flat": 0.1, "dump": 0.0, "rug": 0.0})
               for _ in range(5)]
    cal = compute_metrics(records)["calibration"]
    assert len(cal) >= 1
    top = cal[-1]
    assert top["empirical_accuracy"] == pytest.approx(1.0)
    assert 0.8 <= top["mean_confidence"] <= 1.0
