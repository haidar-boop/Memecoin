"""LightGBM warm-start outcome classifier (Section 4).

The analog engine (Section 3) forecasts by nearest-neighbor resemblance; this
classifier captures the *nonlinear feature interactions* that a k-NN lookup
misses — combinations of trajectory features that jointly predict an outcome.
The two are blended by the adaptive ensemble (Section 6); here we build the
model half.

Three properties from the spec:

* **Multiclass** over the four training labels (PUMP / FLAT / DUMP / RUG),
  with a *fixed* four-class output so a warm-start is always shape-compatible
  even before every class has appeared in the data.
* **Warm-start retrain** via LightGBM ``init_model``: each retrain continues
  from the prior model, adding a small number of trees rather than training
  from scratch — so periodic learning is cheap and cumulative.
* **Exponential time-decay sample weights** (same half-life idea as the analog
  recency decay): recent coins get more weight, so the model tracks a shifting
  meme meta instead of overfitting a dead era.

Cold start (Section 11): below ``min_train_samples`` resolved coins the model
declines to train and :meth:`predict_proba` returns ``None`` — the ensemble
then leans on the analog engine and the rug engine rather than trusting an
under-trained model (Rule 8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Sequence

import numpy as np

from meme_intelligence.config.settings import LightGBMSettings
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.learning.features import FEATURE_DIM
from meme_intelligence.learning.models import OutcomeBucket

_logger = get_logger("learning.classifier")

# Fixed label <-> class-index mapping. Pinning num_class = 4 (even when the
# data holds fewer classes) keeps every trained model shape-compatible, so a
# warm-start never fails because a new class appeared between retrains.
_LABEL_ORDER: tuple[OutcomeBucket, ...] = OutcomeBucket.training_labels()
_LABEL_TO_INT: dict[str, int] = {b.value: i for i, b in enumerate(_LABEL_ORDER)}
_NUM_CLASS = len(_LABEL_ORDER)


class OutcomeClassifier:
    """A warm-started LightGBM multiclass model over coin fingerprints."""

    def __init__(
        self,
        settings: LightGBMSettings,
        *,
        half_life_days: float,
        min_train_samples: int,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._half_life_days = half_life_days
        self._min_train_samples = min_train_samples
        self._now = now_func
        self._booster = None
        self._trained_samples = 0
        self._logger = get_logger("learning.classifier")

    @property
    def is_ready(self) -> bool:
        return self._booster is not None

    @property
    def trained_samples(self) -> int:
        return self._trained_samples

    @property
    def tree_count(self) -> int:
        return 0 if self._booster is None else int(self._booster.num_trees())

    def _params(self) -> dict:
        return {
            "objective": "multiclass",
            "num_class": _NUM_CLASS,
            "learning_rate": self._settings.learning_rate,
            "num_leaves": self._settings.num_leaves,
            "min_data_in_leaf": self._settings.min_child_samples,
            # Small datasets need relaxed binning or LightGBM finds no splits
            # and/or errors on histogram pre-filtering.
            "min_data_in_bin": 1,
            "feature_pre_filter": False,
            "verbosity": -1,
        }

    def _sample_weights(self, resolution_times: Sequence[datetime]) -> np.ndarray:
        """Exponential time-decay weights: recent coins dominate (Section 4)."""
        now = self._now()
        ages_days = np.array(
            [max(0.0, (now - t).total_seconds() / 86400.0) for t in resolution_times],
            dtype=np.float64,
        )
        weights = np.exp(-ages_days / self._half_life_days)
        # Guard against a fully-decayed batch summing to ~0 (all ancient):
        # fall back to uniform so training still has signal (Rule 6).
        if not np.any(weights > 0.0):
            weights = np.ones_like(weights)
        return weights

    def fit(
        self,
        fingerprints: np.ndarray,
        buckets: Sequence[OutcomeBucket],
        resolution_times: Sequence[datetime],
        *,
        warm_start: bool = True,
    ) -> bool:
        """(Re)train the model. Returns True if a model was trained.

        Below ``min_train_samples`` the model is left as-is and False is
        returned (cold start). A ``warm_start`` continues from the current
        model via ``init_model``; if that fails for any reason (e.g. an
        incompatible cached model), it falls back to a full retrain rather
        than losing the ability to predict (Rule 7).
        """
        import lightgbm as lgb

        matrix = np.ascontiguousarray(fingerprints, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[1] != FEATURE_DIM:
            raise ValueError(f"expected an (N, {FEATURE_DIM}) matrix, got {matrix.shape}")
        n = matrix.shape[0]
        if not (len(buckets) == n == len(resolution_times)):
            raise ValueError("fingerprints, buckets, resolution_times length mismatch")
        if n < self._min_train_samples:
            self._logger.info("classifier fit skipped: %d < min_train_samples %d",
                              n, self._min_train_samples)
            return False

        labels = np.array([_LABEL_TO_INT[b.value] for b in buckets], dtype=np.int32)
        weights = self._sample_weights(resolution_times)
        dataset = lgb.Dataset(matrix, label=labels, weight=weights,
                              params={"min_data_in_bin": 1, "feature_pre_filter": False})

        init_model = self._booster if (warm_start and self._booster is not None) else None
        rounds = (self._settings.warm_start_rounds if init_model is not None
                  else self._settings.full_retrain_rounds)
        try:
            self._booster = lgb.train(
                self._params(), dataset, num_boost_round=rounds,
                init_model=init_model, keep_training_booster=True,
            )
        except Exception as exc:  # warm-start incompatibility -> full retrain
            if init_model is None:
                self._logger.error("classifier training failed: %s", exc)
                raise
            self._logger.warning("warm-start failed (%s); retraining from scratch", exc)
            self._booster = lgb.train(
                self._params(), dataset,
                num_boost_round=self._settings.full_retrain_rounds,
                keep_training_booster=True,
            )
        self._trained_samples = n
        self._logger.info("classifier trained on %d samples (%s, %d trees)",
                          n, "warm-start" if init_model is not None else "full",
                          self.tree_count)
        return True

    def predict_proba(self, fingerprint: np.ndarray) -> dict[str, float] | None:
        """Calibrated class probabilities over the four labels, or None.

        Returns ``None`` when the model has not been trained yet (cold start),
        so the ensemble can weight it out instead of consuming a fabricated
        distribution.
        """
        if self._booster is None:
            return None
        vec = np.ascontiguousarray(fingerprint, dtype=np.float32).reshape(1, -1)
        probs = np.asarray(self._booster.predict(vec)[0], dtype=np.float64)
        probs = np.nan_to_num(probs, nan=0.0, posinf=0.0, neginf=0.0)
        total = probs.sum()
        if total <= 0.0:
            return None
        probs = probs / total
        return {bucket.value: float(probs[i]) for i, bucket in enumerate(_LABEL_ORDER)}

    # ---- Persistence (Section 9) ----

    def save(self, path: str) -> None:
        if self._booster is None:
            raise RuntimeError("cannot save an untrained classifier")
        self._booster.save_model(path)

    def load_model(self, path: str) -> None:
        """Load a persisted booster into this classifier (keeps settings)."""
        import lightgbm as lgb

        self._booster = lgb.Booster(model_file=path)
        self._trained_samples = 0  # unknown after reload; not needed for predict
