"""Tests for the LightGBM warm-start outcome classifier (Section 4)."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from meme_intelligence.config.settings import LightGBMSettings, Settings
from meme_intelligence.core.errors import ConfigurationError
from meme_intelligence.learning.classifier import OutcomeClassifier
from meme_intelligence.learning.features import FEATURE_DIM
from meme_intelligence.learning.models import OutcomeBucket

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _blob(center: float, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.normal(loc=center, scale=0.4, size=(n, FEATURE_DIM))).astype(np.float32)


def _classifier(**kw) -> OutcomeClassifier:
    return OutcomeClassifier(
        LightGBMSettings(),
        half_life_days=kw.get("half_life_days", 30.0),
        min_train_samples=kw.get("min_train_samples", 20),
        now_func=lambda: NOW,
    )


def test_cold_start_declines_to_train():
    clf = _classifier(min_train_samples=50)
    X = _blob(0.0, 10, 1)
    buckets = [OutcomeBucket.PUMP] * 10
    times = [NOW] * 10
    assert clf.fit(X, buckets, times) is False
    assert clf.is_ready is False
    assert clf.predict_proba(X[0]) is None


def test_learns_separable_classes():
    pump = _blob(+2.0, 60, 1)
    dump = _blob(-2.0, 60, 2)
    X = np.vstack([pump, dump])
    buckets = [OutcomeBucket.PUMP] * 60 + [OutcomeBucket.DUMP] * 60
    times = [NOW - timedelta(days=1)] * 120
    clf = _classifier()
    assert clf.fit(X, buckets, times) is True
    assert clf.is_ready is True

    proba = clf.predict_proba(_blob(+2.0, 1, 99)[0])
    assert set(proba) == {"pump", "flat", "dump", "rug"}
    assert sum(proba.values()) == pytest.approx(1.0, abs=1e-6)
    assert max(proba, key=proba.get) == "pump"

    proba_down = clf.predict_proba(_blob(-2.0, 1, 98)[0])
    assert max(proba_down, key=proba_down.get) == "dump"


def test_time_decay_favors_recent_labels():
    """Same region labeled DUMP long ago and PUMP recently: recency wins."""
    region_old = _blob(1.0, 40, 3)
    region_new = _blob(1.0, 40, 4)
    X = np.vstack([region_old, region_new])
    buckets = [OutcomeBucket.DUMP] * 40 + [OutcomeBucket.PUMP] * 40
    times = ([NOW - timedelta(days=100)] * 40) + ([NOW] * 40)
    clf = _classifier(half_life_days=1.0)  # 100-day-old samples decay to ~0
    clf.fit(X, buckets, times)
    proba = clf.predict_proba(_blob(1.0, 1, 55)[0])
    assert proba["pump"] > proba["dump"]


def test_warm_start_adds_trees_and_still_predicts():
    X = np.vstack([_blob(+2.0, 40, 1), _blob(-2.0, 40, 2)])
    buckets = [OutcomeBucket.PUMP] * 40 + [OutcomeBucket.DUMP] * 40
    times = [NOW - timedelta(days=1)] * 80
    clf = _classifier()
    clf.fit(X, buckets, times, warm_start=False)
    trees_after_full = clf.tree_count
    assert trees_after_full > 0
    clf.fit(X, buckets, times, warm_start=True)
    assert clf.tree_count > trees_after_full
    proba = clf.predict_proba(_blob(+2.0, 1, 7)[0])
    assert max(proba, key=proba.get) == "pump"


def test_save_load_roundtrip(tmp_path):
    X = np.vstack([_blob(+2.0, 40, 1), _blob(-2.0, 40, 2)])
    buckets = [OutcomeBucket.PUMP] * 40 + [OutcomeBucket.DUMP] * 40
    times = [NOW - timedelta(days=1)] * 80
    clf = _classifier()
    clf.fit(X, buckets, times)
    point = _blob(+2.0, 1, 7)[0]
    before = clf.predict_proba(point)

    path = str(tmp_path / "model.txt")
    clf.save(path)
    reloaded = _classifier()
    reloaded.load_model(path)
    after = reloaded.predict_proba(point)
    assert after is not None
    for label in before:
        assert after[label] == pytest.approx(before[label], abs=1e-6)


def test_single_class_data_trains_without_crashing():
    X = _blob(0.0, 30, 1)
    buckets = [OutcomeBucket.RUG] * 30
    times = [NOW] * 30
    clf = _classifier()
    assert clf.fit(X, buckets, times) is True
    proba = clf.predict_proba(X[0])
    assert max(proba, key=proba.get) == "rug"


def test_length_mismatch_raises():
    clf = _classifier()
    X = _blob(0.0, 30, 1)
    with pytest.raises(ValueError):
        clf.fit(X, [OutcomeBucket.PUMP] * 29, [NOW] * 30)


def test_lightgbm_settings_defaults_and_validation():
    s = Settings.from_env(env={})
    assert s.lightgbm.full_retrain_rounds == 120
    assert s.lightgbm.warm_start_rounds == 30
    with pytest.raises(ConfigurationError):
        LightGBMSettings(learning_rate=0.0)
    with pytest.raises(ConfigurationError):
        LightGBMSettings(num_leaves=0)


def test_lightgbm_settings_env_override():
    env = {
        "MEMEINTEL_LIGHTGBM_WARM_START_ROUNDS": "50",
        "MEMEINTEL_LIGHTGBM_LEARNING_RATE": "0.1",
    }
    s = Settings.from_env(env=env)
    assert s.lightgbm.warm_start_rounds == 50
    assert s.lightgbm.learning_rate == 0.1
