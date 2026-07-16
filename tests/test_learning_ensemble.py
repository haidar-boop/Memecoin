"""Tests for the adaptive accuracy-weighted ensemble (Section 6)."""

import pytest

from meme_intelligence.learning.ensemble import (
    GRADING_VERSION,
    SOURCE_ANALOG,
    SOURCE_LIGHTGBM,
    SOURCE_RUG,
    AdaptiveEnsemble,
    rug_score_to_distribution,
    rug_source_label,
)


def _dist(**kw):
    d = {"pump": 0.0, "flat": 0.0, "dump": 0.0, "rug": 0.0}
    d.update(kw)
    return d


def test_rug_score_to_distribution():
    zero = rug_score_to_distribution(0)
    assert zero["rug"] == 0.0
    assert zero["pump"] == pytest.approx(1 / 3)
    full = rug_score_to_distribution(100)
    assert full["rug"] == 1.0
    mid = rug_score_to_distribution(75)
    assert mid["rug"] == pytest.approx(0.75)
    assert sum(mid.values()) == pytest.approx(1.0)


def test_cold_start_equal_weights():
    ens = AdaptiveEnsemble(window=50)
    result = ens.blend({
        SOURCE_ANALOG: _dist(pump=1.0),
        SOURCE_LIGHTGBM: _dist(dump=1.0),
    })
    assert result.abstained is False
    # No history -> both 0.5 smoothed -> equal weights.
    assert result.weights[SOURCE_ANALOG] == pytest.approx(0.5)
    assert result.weights[SOURCE_LIGHTGBM] == pytest.approx(0.5)
    assert result.distribution["pump"] == pytest.approx(0.5)
    assert result.distribution["dump"] == pytest.approx(0.5)


def test_none_sources_excluded_and_renormalized():
    ens = AdaptiveEnsemble(window=50)
    result = ens.blend({
        SOURCE_ANALOG: _dist(pump=1.0),
        SOURCE_LIGHTGBM: None,       # classifier cold start
        SOURCE_RUG: _dist(rug=1.0),
    })
    assert set(result.sources_used) == {SOURCE_ANALOG, SOURCE_RUG}
    assert sum(result.distribution.values()) == pytest.approx(1.0)


def test_all_sources_unavailable_abstains():
    ens = AdaptiveEnsemble(window=50)
    result = ens.blend({SOURCE_ANALOG: None, SOURCE_LIGHTGBM: None, SOURCE_RUG: None})
    assert result.abstained is True
    assert result.distribution["pump"] == pytest.approx(0.25)
    assert result.sources_used == ()


def test_accuracy_shifts_weight_toward_reliable_source():
    ens = AdaptiveEnsemble(window=100)
    # lightgbm always right, analog always wrong.
    for _ in range(20):
        ens.record_outcome({SOURCE_LIGHTGBM: "pump", SOURCE_ANALOG: "dump"}, "pump")
    w = ens.weights((SOURCE_ANALOG, SOURCE_LIGHTGBM))
    assert w[SOURCE_LIGHTGBM] > w[SOURCE_ANALOG]
    # The blend now leans toward lightgbm's call.
    result = ens.blend({SOURCE_ANALOG: _dist(pump=1.0), SOURCE_LIGHTGBM: _dist(dump=1.0)})
    assert result.distribution["dump"] > result.distribution["pump"]


def test_smoothed_accuracy_defaults_to_half():
    ens = AdaptiveEnsemble(window=10)
    assert ens.source_accuracy(SOURCE_ANALOG) == 0.5
    assert ens.raw_accuracy(SOURCE_ANALOG) is None


def test_abstained_source_not_counted_wrong():
    ens = AdaptiveEnsemble(window=10)
    # analog abstained (None predicted) -> must not be recorded as a miss.
    ens.record_outcome({SOURCE_ANALOG: None, SOURCE_LIGHTGBM: "rug"}, "rug")
    assert ens.raw_accuracy(SOURCE_ANALOG) is None
    assert ens.raw_accuracy(SOURCE_LIGHTGBM) == 1.0


def test_window_bounds_history():
    ens = AdaptiveEnsemble(window=5)
    for _ in range(10):
        ens.record_outcome({SOURCE_LIGHTGBM: "pump"}, "dump")  # all wrong
    report = ens.accuracy_report()
    assert report[SOURCE_LIGHTGBM]["samples"] == 5  # capped at window


def test_confidence_is_max_probability():
    ens = AdaptiveEnsemble(window=10)
    result = ens.blend({SOURCE_RUG: _dist(rug=0.9, pump=0.1)})
    assert result.confidence == pytest.approx(max(result.distribution.values()))


def test_final_history_tracks_blended_verdict():
    ens = AdaptiveEnsemble(window=10)
    assert ens.final_accuracy() is None
    ens.record_outcome({SOURCE_ANALOG: "pump"}, "pump", final_label="pump")  # right
    ens.record_outcome({}, "rug", final_label="dump")                        # wrong
    assert ens.final_samples == 2
    assert ens.final_accuracy() == pytest.approx(0.5)
    assert ens.accuracy_report()["ensemble_final"]["samples"] == 2
    ens.reset_final_history()
    assert ens.final_accuracy() is None
    assert ens.final_samples == 0


def test_no_final_label_leaves_final_history_untouched():
    ens = AdaptiveEnsemble(window=10)
    ens.record_outcome({SOURCE_ANALOG: "pump"}, "pump")
    assert ens.final_samples == 0


def test_final_history_survives_save_load(tmp_path):
    ens = AdaptiveEnsemble(window=10)
    ens.record_outcome({}, "pump", final_label="pump")
    path = str(tmp_path / "ens.joblib")
    ens.save(path)
    reloaded = AdaptiveEnsemble.load(path)
    assert reloaded.final_samples == 1
    assert reloaded.final_accuracy() == pytest.approx(1.0)


def test_load_pre_drift_monitor_artifact(tmp_path):
    """Artifacts saved before the drift monitor existed must still load (Rule 18)."""
    import joblib

    path = str(tmp_path / "old.joblib")
    joblib.dump({"window": 10, "history": {SOURCE_ANALOG: [True, False]}}, path)
    ens = AdaptiveEnsemble.load(path)
    assert ens.final_samples == 0
    assert ens.raw_accuracy(SOURCE_ANALOG) == pytest.approx(0.5)


def test_save_load_roundtrip(tmp_path):
    ens = AdaptiveEnsemble(window=100)
    for _ in range(15):
        ens.record_outcome({SOURCE_LIGHTGBM: "pump", SOURCE_ANALOG: "pump"}, "pump")
    path = str(tmp_path / "ensemble.joblib")
    ens.save(path)
    reloaded = AdaptiveEnsemble.load(path)
    assert reloaded.raw_accuracy(SOURCE_LIGHTGBM) == 1.0
    assert reloaded.weights((SOURCE_ANALOG, SOURCE_LIGHTGBM)) == ens.weights(
        (SOURCE_ANALOG, SOURCE_LIGHTGBM))


# ---- Rug-engine grading fix (2026-07-16 mind-layer audit) ----

def test_rug_source_label_abstains_at_or_below_threshold():
    # At/below the abstain score the engine is NOT calling a rug and has no
    # four-class opinion — it abstains rather than fabricating a 'pump'.
    assert rug_source_label(0, abstain_at_or_below=25) is None
    assert rug_source_label(25, abstain_at_or_below=25) is None
    # Above it, the engine is calling a rug and is graded as such.
    assert rug_source_label(26, abstain_at_or_below=25) == "rug"
    assert rug_source_label(100, abstain_at_or_below=25) == "rug"


def test_rug_source_label_matches_old_argmax_above_threshold():
    # The fix must not change WHICH label the engine emits when it does speak:
    # above a rug score of 25 the old argmax of the distribution was always
    # RUG, and the new helper agrees.
    for score in (26, 40, 75, 100):
        dist = rug_score_to_distribution(score)
        assert max(dist, key=dist.get) == "rug"
        assert rug_source_label(score, abstain_at_or_below=25) == "rug"


def test_abstaining_rug_source_is_not_graded_wrong():
    # A low-score rug engine (abstained -> None) must not accrue wrong grades
    # on a rug-heavy population; only its real rug calls are scored.
    ens = AdaptiveEnsemble(window=100)
    for _ in range(50):
        ens.record_outcome(
            {SOURCE_RUG: rug_source_label(5, abstain_at_or_below=25)}, "rug")
    assert ens.raw_accuracy(SOURCE_RUG) is None          # never graded
    assert ens.source_accuracy(SOURCE_RUG) == pytest.approx(0.5)  # neutral prior
    # A real rug call on a rug outcome grades correct.
    ens.record_outcome(
        {SOURCE_RUG: rug_source_label(80, abstain_at_or_below=25)}, "rug")
    assert ens.raw_accuracy(SOURCE_RUG) == pytest.approx(1.0)


def test_reset_source_history_restores_neutral_weight():
    ens = AdaptiveEnsemble(window=100)
    for _ in range(50):
        ens.record_outcome({SOURCE_RUG: "pump"}, "rug")   # 50 stale wrong grades
    assert ens.source_accuracy(SOURCE_RUG) < 0.05         # weight pinned near zero
    ens.reset_source_history(SOURCE_RUG)
    assert ens.raw_accuracy(SOURCE_RUG) is None
    assert ens.source_accuracy(SOURCE_RUG) == pytest.approx(0.5)
    ens.reset_source_history("nonexistent-source")        # unknown name is a no-op


def test_grading_version_migration_resets_only_rug_history(tmp_path):
    # A v1 artifact (fabricated-pump grading) must drop ONLY the rug_engine
    # history on load so it re-earns weight; analog/lightgbm/final grading is
    # unchanged and must survive.
    import joblib

    path = str(tmp_path / "v1.joblib")
    joblib.dump({
        "window": 100,
        "history": {
            SOURCE_RUG: [False] * 40,
            SOURCE_ANALOG: [True] * 40,
            SOURCE_LIGHTGBM: [True] * 40,
        },
        "final_history": [True] * 40,
    }, path)  # no grading_version key => treated as v1
    ens = AdaptiveEnsemble.load(path)
    assert ens.raw_accuracy(SOURCE_RUG) is None            # rug history dropped
    assert ens.raw_accuracy(SOURCE_ANALOG) == pytest.approx(1.0)   # kept
    assert ens.raw_accuracy(SOURCE_LIGHTGBM) == pytest.approx(1.0)  # kept
    assert ens.final_samples == 40                        # kept


def test_current_version_artifact_keeps_rug_history(tmp_path):
    # Once saved under the current grading version, a reload must NOT wipe the
    # rug history again (idempotent — the migration fires only on upgrade).
    ens = AdaptiveEnsemble(window=100)
    for _ in range(20):
        ens.record_outcome({SOURCE_RUG: "rug"}, "rug")
    path = str(tmp_path / "v2.joblib")
    ens.save(path)
    reloaded = AdaptiveEnsemble.load(path)
    assert reloaded.raw_accuracy(SOURCE_RUG) == pytest.approx(1.0)


def test_saved_artifact_carries_grading_version(tmp_path):
    import joblib

    path = str(tmp_path / "ens.joblib")
    AdaptiveEnsemble(window=10).save(path)
    assert joblib.load(path)["grading_version"] == GRADING_VERSION
