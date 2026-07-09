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


def test_all_old_batch_still_learns_not_flat_prior():
    """Regression: an all-old resolved batch must still train a real model.

    Raw decay weights collapse below LightGBM's per-leaf hessian floor and
    produce a flat feature-independent prior; mean-normalized weights fix it
    (Rule 8 — never fabricate a confident-looking uniform distribution)."""
    pump = _blob(+2.0, 60, 1)
    dump = _blob(-2.0, 60, 2)
    X = np.vstack([pump, dump])
    buckets = [OutcomeBucket.PUMP] * 60 + [OutcomeBucket.DUMP] * 60
    # Every coin resolved ~1 year ago, default half-life 30d -> tiny raw weights.
    times = [NOW - timedelta(days=365)] * 120
    clf = _classifier(half_life_days=30.0)
    assert clf.fit(X, buckets, times) is True
    pump_proba = clf.predict_proba(_blob(+2.0, 1, 91)[0])
    dump_proba = clf.predict_proba(_blob(-2.0, 1, 92)[0])
    # A real (non-flat) model separates the two regions.
    assert pump_proba["pump"] > 0.7
    assert dump_proba["dump"] > 0.7
    assert max(pump_proba, key=pump_proba.get) == "pump"


def test_short_halflife_moderately_old_batch_learns():
    """Short half-life with moderately-old coins must not collapse training."""
    X = np.vstack([_blob(+2.0, 50, 1), _blob(-2.0, 50, 2)])
    buckets = [OutcomeBucket.PUMP] * 50 + [OutcomeBucket.DUMP] * 50
    times = [NOW - timedelta(days=11)] * 100
    clf = _classifier(half_life_days=1.0)
    assert clf.fit(X, buckets, times) is True
    proba = clf.predict_proba(_blob(+2.0, 1, 5)[0])
    assert max(proba, key=proba.get) == "pump"


def test_warm_start_failure_falls_back_and_logs_accurately(monkeypatch, caplog):
    """A failed warm-start must retrain from scratch and log the real path."""
    import lightgbm as lgb

    X = np.vstack([_blob(+2.0, 40, 1), _blob(-2.0, 40, 2)])
    buckets = [OutcomeBucket.PUMP] * 40 + [OutcomeBucket.DUMP] * 40
    times = [NOW - timedelta(days=1)] * 80
    clf = _classifier()
    clf.fit(X, buckets, times, warm_start=False)  # establishes a base model

    real_train = lgb.train
    calls = {"n": 0}

    def flaky_train(*args, **kwargs):
        # Fail only the warm-start attempt (init_model set); allow the fallback.
        if kwargs.get("init_model") is not None:
            calls["n"] += 1
            raise RuntimeError("incompatible init_model")
        return real_train(*args, **kwargs)

    monkeypatch.setattr(lgb, "train", flaky_train)
    with caplog.at_level("INFO"):
        assert clf.fit(X, buckets, times, warm_start=True) is True
    assert calls["n"] == 1
    assert clf.is_ready is True
    assert any("warm-start failed" in r.message for r in caplog.records)
    assert any("warm-start fallback" in r.message for r in caplog.records)


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
