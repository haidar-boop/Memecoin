"""Tests for the adaptive accuracy-weighted ensemble (Section 6)."""

import pytest

from meme_intelligence.learning.models import OutcomeBucket
from meme_intelligence.learning.ensemble import (
    SOURCE_ANALOG,
    SOURCE_LIGHTGBM,
    SOURCE_RUG,
    AdaptiveEnsemble,
    rug_score_to_distribution,
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


# ---- The rug source must abstain outside its competence (2026-07-31) ----


def test_the_rug_source_abstains_instead_of_predicting_pump():
    """CONFIRMED bug: rug_score_to_distribution spreads the non-rug mass
    UNIFORMLY (the engine has no opinion on pump/flat/dump), so a plain argmax
    recorded 'the rug engine predicted PUMP' for every unflagged coin — a
    3-way tie broken by dict order. Most memecoins resolve DUMP/RUG, so the
    source was graded wrong almost every time and the accuracy-weighted
    ensemble drove its weight to ~0, removing the rug signal from the blend."""
    from meme_intelligence.learning.service import _rug_source_label

    assert _rug_source_label(rug_score_to_distribution(0.0)) is None
    assert _rug_source_label(rug_score_to_distribution(10.0)) is None
    assert _rug_source_label(rug_score_to_distribution(80.0)) == OutcomeBucket.RUG.value


def test_an_abstaining_source_is_not_counted_as_wrong():
    """The mechanism the fix relies on: record_outcome skips None."""
    abstaining = AdaptiveEnsemble(window=10)
    for _ in range(5):
        abstaining.record_outcome({SOURCE_RUG: None}, OutcomeBucket.DUMP.value)

    # The old behaviour: the same 5 coins, but the source labelled "pump"
    # every time (the tie-break) while they all resolved DUMP.
    graded_wrong = AdaptiveEnsemble(window=10)
    for _ in range(5):
        graded_wrong.record_outcome({SOURCE_RUG: OutcomeBucket.PUMP.value},
                                    OutcomeBucket.DUMP.value)

    assert (abstaining.source_accuracy(SOURCE_RUG)
            > graded_wrong.source_accuracy(SOURCE_RUG))
