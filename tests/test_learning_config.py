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
