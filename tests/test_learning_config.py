"""Tests for the mind-layer configuration groups (Section 11, Rule 17)."""

import pytest

from meme_intelligence.config.settings import (
    LearningSettings,
    RugSignalWeights,
    Settings,
)
from meme_intelligence.core.errors import ConfigurationError


def test_learning_defaults_are_valid():
    s = Settings.from_env(env={})
    assert s.learning.enabled is False
    assert s.learning.knn_neighbors == 25
    assert s.learning.recency_half_life_days == 30.0
    assert s.learning.retrain_every_n == 200
    assert s.learning.horizon_hours() == (0.25, 1.0, 6.0, 24.0)


def test_rug_signal_weights_defaults():
    w = RugSignalWeights()
    assert w.liquidity_removed == 30.0
    assert w.unsellable == 30.0
    assert w.mint_authority_active == 20.0


def test_learning_env_overrides():
    env = {
        "MEMEINTEL_LEARNING_ENABLED": "true",
        "MEMEINTEL_LEARNING_KNN_NEIGHBORS": "40",
        "MEMEINTEL_LEARNING_HORIZONS_HOURS": "1,4,12",
        "MEMEINTEL_LEARNING_RECENCY_HALF_LIFE_DAYS": "14",
        "MEMEINTEL_RUG_SIGNAL_WEIGHTS_UNSELLABLE": "45",
    }
    s = Settings.from_env(env=env)
    assert s.learning.enabled is True
    assert s.learning.knn_neighbors == 40
    assert s.learning.horizon_hours() == (1.0, 4.0, 12.0)
    assert s.learning.recency_half_life_days == 14.0
    assert s.rug_signal_weights.unsellable == 45.0


def test_dump_threshold_must_be_below_pump():
    with pytest.raises(ConfigurationError):
        LearningSettings(pump_return_percent=50.0, dump_return_percent=60.0)


def test_positive_settings_validated():
    with pytest.raises(ConfigurationError):
        LearningSettings(knn_neighbors=0)
    with pytest.raises(ConfigurationError):
        LearningSettings(recency_half_life_days=-1.0)


def test_drift_floor_range_validated():
    with pytest.raises(ConfigurationError):
        LearningSettings(drift_accuracy_floor=1.5)


def test_rug_engine_abstain_score_default_and_range():
    # Default matches the distribution's rug-argmax boundary (2026-07-16 audit).
    assert LearningSettings().rug_engine_abstain_at_or_below_score == 25.0
    s = Settings.from_env(
        env={"MEMEINTEL_LEARNING_RUG_ENGINE_ABSTAIN_AT_OR_BELOW_SCORE": "40"})
    assert s.learning.rug_engine_abstain_at_or_below_score == 40.0
    with pytest.raises(ConfigurationError,
                       match="rug_engine_abstain_at_or_below_score"):
        LearningSettings(rug_engine_abstain_at_or_below_score=-1.0)
    with pytest.raises(ConfigurationError,
                       match="rug_engine_abstain_at_or_below_score"):
        LearningSettings(rug_engine_abstain_at_or_below_score=101.0)


def test_archetype_min_cluster_size_must_be_at_least_two():
    # HDBSCAN invariant: 1 is positive but not a valid cluster size.
    with pytest.raises(ConfigurationError):
        LearningSettings(archetype_min_cluster_size=1)
    assert LearningSettings(archetype_min_cluster_size=2).archetype_min_cluster_size == 2


def test_horizons_must_list_at_least_one():
    with pytest.raises(ConfigurationError):
        LearningSettings(horizons_hours="  ,  ")


def test_horizons_reject_non_numeric():
    with pytest.raises(ConfigurationError):
        LearningSettings(horizons_hours="1,soon,6")


def test_rug_weight_negative_rejected():
    with pytest.raises(ConfigurationError):
        RugSignalWeights(unsellable=-5.0)


def test_metrics_window_defaults_and_validation():
    # Windowed self-evaluation (2026-07-16 audit): on by default at 7 days.
    s = LearningSettings()
    assert s.metrics_window_days == 7.0
    assert s.metrics_max_records == 100_000
    assert LearningSettings(metrics_window_days=0).metrics_window_days == 0
    with pytest.raises(ConfigurationError, match="metrics_window_days"):
        LearningSettings(metrics_window_days=-1.0)
    env = Settings.from_env(env={"MEMEINTEL_LEARNING_METRICS_WINDOW_DAYS": "3"})
    assert env.learning.metrics_window_days == 3.0


def test_metrics_window_rejects_overflow_scale_values():
    """2026-07-16 review finding: past ~740k days the cutoff arithmetic
    overflows datetime at RUNTIME — crashing every /mind and silently
    disarming the veto (its blanket except abstains). A typo like 1e10 must
    fail loudly at startup instead (Rule 6)."""
    with pytest.raises(ConfigurationError, match="metrics_window_days"):
        LearningSettings(metrics_window_days=1e10)
    with pytest.raises(ConfigurationError, match="metrics_window_days"):
        LearningSettings(metrics_window_days=3651)
    assert LearningSettings(metrics_window_days=3650).metrics_window_days == 3650


def test_metrics_max_records_validation():
    with pytest.raises(ConfigurationError, match="metrics_max_records"):
        LearningSettings(metrics_max_records=-1)
    assert LearningSettings(metrics_max_records=0).metrics_max_records == 0
