"""LearningService — the assembled mind layer (Section 10).

Ties every piece together behind one public entry point the scanner/dashboard
calls per coin, plus the supporting lifecycle functions:

* :meth:`evaluate_coin`     — the full verdict for a live coin (Section 10),
* :meth:`record_detection`  — begin a coin's lifecycle,
* :meth:`capture_snapshot`  — append a trajectory observation,
* :meth:`resolve_outcome`   — record a measured forward outcome (labels a coin),
* :meth:`retrain_if_due`    — periodic warm-start retrain of the classifier,
* :meth:`refresh_archetypes`— re-cluster the fingerprint set,
* :meth:`get_learning_metrics` — the self-evaluation numbers (Section 8).

Two learning loops run here (Section 3 / Section 7): the moment a coin resolves
its fingerprint is inserted into the analog index (**instant learning** — the
next similar coin benefits immediately, no retraining) and the ensemble's
per-source accuracy is updated; separately, the LightGBM classifier is
warm-started on a schedule (**periodic learning**). The StandardScaler defines
the shared feature space; refitting it (drift handling) triggers a full rebuild
so every model stays consistent.

Everything is decision-support only — the service never trades (Rule 21). It
consumes a :class:`SecurityProfile` the caller already collected (the existing
GoPlus collector) rather than fetching on-chain data itself, keeping it
decoupled and testable (Rule 4).
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from typing import Callable, Sequence

import numpy as np

from meme_intelligence.config.settings import Settings
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import SecurityProfile, TokenIdentity
from meme_intelligence.learning.analog import AnalogEntry, AnalogMemory
from meme_intelligence.learning.archetypes import ArchetypeModel
from meme_intelligence.learning.classifier import OutcomeClassifier
from meme_intelligence.learning.ensemble import (
    SOURCE_ANALOG,
    SOURCE_LIGHTGBM,
    SOURCE_RUG,
    AdaptiveEnsemble,
    rug_score_to_distribution,
)
from meme_intelligence.learning.features import (
    FEATURE_VERSION,
    FingerprintExtractor,
    StandardScalerBundle,
)
from meme_intelligence.learning.metrics import PredictionRecord, compute_metrics
from meme_intelligence.learning.models import (
    CoinRecord,
    CoinSnapshot,
    CoinVerdict,
    OutcomeBucket,
    OutcomeLabel,
)
from meme_intelligence.learning.rug_engine import RugEngine
from meme_intelligence.learning.store import LearningStore

_MAX_ANALOGS_REPORTED = 5


def _resolution_time(record: CoinRecord) -> datetime:
    times = [lb.resolved_at for lb in record.labels if lb.resolved_at is not None]
    return max(times) if times else record.detected_at


def _argmax_label(distribution: dict[str, float] | None) -> str | None:
    if not distribution:
        return None
    return max(distribution, key=distribution.get)


class LearningService:
    """Orchestrates the analog + model + rug + ensemble mind layer."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: LearningStore | None = None,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._ls = settings.learning
        self._now = now_func
        self._logger = get_logger("learning.service")

        self._state_dir = self._ls.state_dir
        self._store = store if store is not None else LearningStore(
            os.path.join(self._state_dir, "learning.db"), now_func=now_func)

        self._extractor = FingerprintExtractor()
        self._scaler = StandardScalerBundle()
        self._analog = AnalogMemory(now_func=now_func)
        self._classifier = OutcomeClassifier(
            settings.lightgbm,
            half_life_days=self._ls.model_half_life_days,
            min_train_samples=self._ls.min_train_samples,
            now_func=now_func,
        )
        self._archetypes = ArchetypeModel()
        self._ensemble = AdaptiveEnsemble(window=self._ls.accuracy_window)

        # Learning-loop bookkeeping.
        self._last_retrain_count = 0
        self._scaler_fit_count = 0
        # True once THIS process has graded outcomes into the ensemble. The
        # monitor and the backtest cron share the state dir: only the process
        # that actually graded may overwrite ensemble.joblib, otherwise the
        # monitor's stale in-memory copy (loaded at startup, never graded)
        # clobbers the cron's accumulated accuracy history — and because
        # predictions are marked scored durably, those grades could never be
        # regenerated (bug-hunt finding).
        self._ensemble_dirty = False
        # Deferred-persist state (see deferred_persist): depth supports
        # nesting; writes/flush_every drive the mid-block flush cadence.
        self._defer_depth = 0
        self._deferred_writes = 0
        self._defer_flush_every = 0
        # Model-artifact ownership (see _persist_now): True only once THIS
        # process has mutated the scaler/analog/classifier/archetypes.
        self._models_dirty = False
        # mtime of the analog index as last loaded/saved by this process —
        # drives the cross-process reload in _maybe_reload_analog.
        self._analog_mtime_ns: int | None = None
        # False after a feature-version mismatch: the on-disk index is in an
        # OLD feature space and must never be reloaded into this process.
        self._analog_reload_ok = True

        self._load_artifacts()

    # ---- Persistence (Section 9) ----

    def _path(self, name: str) -> str:
        return os.path.join(self._state_dir, name)

    def _load_artifacts(self) -> None:
        """Restore persisted models so learning compounds across restarts.

        Feature-space versioning: model artifacts (scaler, analog index,
        classifier, archetypes) are only loaded when they were saved under the
        current :data:`FEATURE_VERSION`. On mismatch they are discarded — they
        live in a different feature space and would produce garbage distances —
        and the retrain counters reset so the next ``retrain_if_due`` rebuilds
        everything from the stored raw snapshots (nothing is lost; the SQLite
        records are version-independent, Rule 18). The ensemble's accuracy
        history is feature-space-independent and always loads.
        """
        try:
            stored_version = None
            if os.path.exists(self._path("state.joblib")):
                import joblib

                state = joblib.load(self._path("state.joblib"))
                stored_version = state.get("feature_version")
                self._last_retrain_count = state.get("last_retrain_count", 0)
                self._scaler_fit_count = state.get("scaler_fit_count", 0)
            if os.path.exists(self._path("ensemble.joblib")):
                self._ensemble = AdaptiveEnsemble.load(self._path("ensemble.joblib"))

            if stored_version != FEATURE_VERSION:
                if os.path.exists(self._path("scaler.joblib")):
                    self._logger.warning(
                        "fingerprint feature space changed (v%s -> v%d): discarding "
                        "trained models; they will rebuild from stored records",
                        stored_version, FEATURE_VERSION)
                self._last_retrain_count = 0
                self._scaler_fit_count = 0
                self._analog_reload_ok = False
                return

            if os.path.exists(self._path("scaler.joblib")):
                self._scaler = StandardScalerBundle.load(self._path("scaler.joblib"))
            if os.path.exists(self._path("index.faiss")):
                self._analog = AnalogMemory.load(
                    self._path("index.faiss"), self._path("index_meta.joblib"),
                    now_func=self._now)
                self._analog_mtime_ns = self._index_mtime_ns()
            if os.path.exists(self._path("classifier.txt")):
                self._classifier.load_model(self._path("classifier.txt"))
            if os.path.exists(self._path("archetypes.joblib")):
                self._archetypes = ArchetypeModel.load(self._path("archetypes.joblib"))
        except Exception as exc:  # corrupt artifact must not brick the service
            self._logger.error("failed to load learning artifacts (%s); cold start", exc)

    def persist(self) -> None:
        """Persist all mutable artifacts. Called after learning updates.

        Inside a :meth:`deferred_persist` block the write is BATCHED instead
        of immediate (2026-07-19, "store everything faster"): resolving a
        coin calls persist(), and an outcome-refresh run resolves thousands —
        each write rewrites the full analog index (~3.4MB), so per-coin
        persistence turned one cron run into gigabytes of redundant disk
        writes. Deferred, the artifacts are flushed every ``flush_every``
        resolutions and once at block exit. Outside a deferral block the
        behavior is exactly as before (immediate write, Rule 18).
        """
        if self._defer_depth > 0:
            self._deferred_writes += 1
            if (self._defer_flush_every > 0
                    and self._deferred_writes >= self._defer_flush_every):
                self._deferred_writes = 0
                self._persist_now()
            return
        self._persist_now()

    @contextlib.contextmanager
    def deferred_persist(self, *, flush_every: int = 0):
        """Batch persist() calls inside the block; flush on exit.

        ``flush_every`` > 0 additionally flushes mid-block every that many
        deferred writes, bounding how much instant-learning progress a crash
        could lose (measured outcome labels are always durable in SQLite the
        moment they are recorded — this only affects the model artifacts).
        Re-entrant: nested blocks flush once, at the outermost exit.
        """
        self._defer_depth += 1
        if self._defer_depth == 1:
            self._defer_flush_every = max(0, int(flush_every))
            self._deferred_writes = 0
        try:
            yield self
        finally:
            self._defer_depth -= 1
            if self._defer_depth == 0:
                had_writes = self._deferred_writes > 0
                self._deferred_writes = 0
                self._defer_flush_every = 0
                if had_writes:
                    self._persist_now()

    def _persist_now(self) -> None:
        """The real artifact write — see :meth:`persist` for the contract.

        OWNERSHIP GUARD (2026-07-19 — the "memory stuck at exactly 5000"
        bug): the monitor and the backtest cron share this state dir, but
        only the CRON ever mutates the models (it resolves coins; the
        monitor only reads). The monitor's shutdown persist used to write
        its BOOT-TIME in-memory copy of the analog index over everything
        the cron had appended since — so every restart reset the memory
        file to whatever the monitor loaded at its previous boot, forever.
        Model artifacts are now written only by a process that actually
        mutated them (``_models_dirty``), exactly like the pre-existing
        ``_ensemble_dirty`` guard for the ensemble's accuracy history.
        """
        if self._state_dir == ":memory:":
            return
        os.makedirs(self._state_dir, exist_ok=True)
        try:
            if self._models_dirty:
                if self._scaler.is_fitted:
                    self._scaler.save(self._path("scaler.joblib"))
                if self._analog.size > 0:
                    self._analog.save(self._path("index.faiss"),
                                      self._path("index_meta.joblib"))
                    self._analog_mtime_ns = self._index_mtime_ns()
                if self._classifier.is_ready:
                    self._classifier.save(self._path("classifier.txt"))
                if self._archetypes.is_fitted:
                    self._archetypes.save(self._path("archetypes.joblib"))
                import joblib

                joblib.dump({"last_retrain_count": self._last_retrain_count,
                             "scaler_fit_count": self._scaler_fit_count,
                             "feature_version": FEATURE_VERSION},
                            self._path("state.joblib"))
            if self._ensemble_dirty or not os.path.exists(self._path("ensemble.joblib")):
                self._ensemble.save(self._path("ensemble.joblib"))
        except Exception as exc:
            self._logger.error("failed to persist learning artifacts: %s", exc)

    def _index_mtime_ns(self) -> int | None:
        try:
            return os.stat(self._path("index.faiss")).st_mtime_ns
        except OSError:
            return None

    def _maybe_reload_analog(self) -> None:
        """Pick up analog-index growth written by the OTHER process.

        The cron grows the memory file hourly; the monitor used to hold its
        boot-time copy until the next restart, so /mind showed a stale count
        (and fresh analogs were invisible to live verdicts). A cheap mtime
        check reloads the index when the file changed. Never runs in a
        process that owns un-flushed mutations of its own (dirty or inside a
        deferred-persist block) — its in-memory copy is ahead of the file.
        Best-effort: a torn or mid-write file keeps the current copy and
        retries on the next call (Rule 7).
        """
        if (self._state_dir == ":memory:" or not self._analog_reload_ok
                or self._models_dirty or self._defer_depth > 0):
            return
        mtime = self._index_mtime_ns()
        if mtime is None or mtime == self._analog_mtime_ns:
            return
        try:
            reloaded = AnalogMemory.load(self._path("index.faiss"),
                                         self._path("index_meta.joblib"),
                                         now_func=self._now)
        except Exception as exc:  # noqa: BLE001 — mid-write file: retry next call
            self._logger.warning("analog reload failed (keeping current copy): %s", exc)
            return
        self._analog = reloaded
        self._analog_mtime_ns = mtime
        self._logger.info("analog memory reloaded from disk: %d coins",
                          self._analog.size)

    # ---- Lifecycle (Section 1 / Section 10) ----

    def record_detection(
        self,
        token_address: str,
        chain: str,
        *,
        detection_price_usd: float | None = None,
        creator: str | None = None,
        symbol: str | None = None,
        name: str | None = None,
    ) -> int:
        token = TokenIdentity(chain=chain, address=token_address, symbol=symbol, name=name)
        return self._store.record_detection(
            token, detected_at=self._now(),
            detection_price_usd=detection_price_usd, creator=creator)

    def capture_snapshot(self, token_address: str, chain: str, snapshot: dict) -> int | None:
        """Append a trajectory snapshot; auto-registers an unseen coin (Section 1)."""
        token = TokenIdentity(chain=chain, address=token_address)
        coin_id = self._store.coin_id(token)
        if coin_id is None:
            snap0 = CoinSnapshot.from_dict(snapshot)
            coin_id = self._store.record_detection(
                token, detected_at=self._now(), detection_price_usd=snap0.price_usd)
        return self._store.append_snapshot(coin_id, CoinSnapshot.from_dict(snapshot))

    def evaluate_coin(
        self,
        token_address: str,
        chain: str,
        snapshot_series: Sequence[dict],
        *,
        security: SecurityProfile | None = None,
        creator: str | None = None,
    ) -> dict:
        """The full verdict for a live coin (Section 10 — the public contract)."""
        self._maybe_reload_analog()
        snaps = [CoinSnapshot.from_dict(s) for s in snapshot_series]
        fingerprint = self._extractor.extract(snaps)
        scaled = self._scaler.transform(fingerprint.vector)

        # 1. Analog / k-NN forecast (Section 3).
        neighbors = self._analog.query(scaled, self._ls.knn_neighbors)
        vote = self._analog.vote(neighbors, half_life_days=self._ls.recency_half_life_days,
                                 min_neighbors=self._ls.min_analog_neighbors)
        analog_dist = None if vote.abstained else vote.distribution

        # 2. LightGBM classifier (Section 4).
        model_dist = self._classifier.predict_proba(scaled)

        # 3. Rug engine (Section 5) — always has an opinion.
        deployer_rugs = self._store.deployer_rug_count(creator, chain)
        rug = self._rug_engine().assess(security=security, snapshots=snaps,
                                        deployer_rug_count=deployer_rugs)
        rug_dist = rug_score_to_distribution(rug.score)

        # 4. Adaptive ensemble blend (Section 6).
        result = self._ensemble.blend({
            SOURCE_ANALOG: analog_dist,
            SOURCE_LIGHTGBM: model_dist,
            SOURCE_RUG: rug_dist,
        })

        # 5. Archetype + novelty (Section 3).
        assignment = self._archetypes.assign(scaled)
        novelty_flagged = (assignment.novelty_score is not None
                           and assignment.novelty_score * 100.0 >= self._ls.novelty_percentile)

        resolved_count = self._store.resolved_count()
        cold_factor = min(1.0, resolved_count / max(1, self._ls.cold_start_samples))
        # A 1-2 snapshot trajectory barely has a shape yet — its fingerprint is
        # mostly `last` values with zero slopes/volatility. Confidence scales
        # with observed trajectory depth (Section 11, Rule 8).
        snap_factor = min(1.0, len(snaps) / max(1, self._ls.min_snapshots_for_confidence))

        verdict = CoinVerdict(
            token_address=token_address,
            chain=chain,
            final_probabilities=result.distribution,
            rug_risk_score=rug.score,
            rug_signals_fired=list(rug.fired_names),
            nearest_analogs=list(vote.neighbors[:_MAX_ANALOGS_REPORTED]),
            matched_archetype=assignment.name,
            novelty_score=assignment.novelty_score,
            ensemble_weights=result.weights,
            model_confidence=result.confidence * cold_factor * snap_factor,
            sample_size=resolved_count,
        )

        self._store_prediction(token_address, chain, snaps, verdict, result,
                               analog_dist, model_dist, rug_dist, assignment.name,
                               novelty_flagged, creator=creator)
        return verdict.to_dict()

    def _store_prediction(self, token_address, chain, snaps, verdict, result,
                          analog_dist, model_dist, rug_dist, archetype, novelty_flagged,
                          *, creator: str | None = None) -> None:
        """Persist the coin's first verdict for later grading (Section 8)."""
        token = TokenIdentity(chain=chain, address=token_address)
        coin_id = self._store.coin_id(token)
        if coin_id is None:
            detection_price = next((s.price_usd for s in snaps if s.price_usd is not None), None)
            coin_id = self._store.record_detection(
                token, detected_at=self._now(), detection_price_usd=detection_price,
                creator=creator)
        # Persist the evaluated trajectory when the store holds none for this
        # coin, so a later resolution has a real fingerprint to add to the
        # analog index (a caller may use evaluate_coin without capture_snapshot).
        # If capture_snapshot already populated the trajectory, we leave it.
        if not self._store.snapshots_for(coin_id):
            for snap in snaps:
                self._store.append_snapshot(coin_id, snap)
        payload = {
            "distribution": verdict.final_probabilities,
            "predicted_label": _argmax_label(verdict.final_probabilities),
            "source_labels": {
                SOURCE_ANALOG: _argmax_label(analog_dist),
                SOURCE_LIGHTGBM: _argmax_label(model_dist),
                SOURCE_RUG: _argmax_label(rug_dist),
            },
            "archetype": archetype,
            "novelty_flagged": novelty_flagged,
            "scored": False,
        }
        self._store.record_prediction(coin_id, payload)

    def resolve_outcome(
        self,
        token_address: str,
        chain: str,
        horizon_hours: float,
        forward_return_percent: float,
        *,
        is_rug: bool = False,
    ) -> None:
        """Record a measured forward outcome and label the coin (Section 1).

        A confirmed rug overrides the return-based bucket. The first time a
        coin becomes resolved, instant learning fires: its fingerprint enters
        the analog index and the ensemble's accuracy is updated.
        """
        token = TokenIdentity(chain=chain, address=token_address)
        coin_id = self._store.coin_id(token)
        if coin_id is None:
            self._logger.warning("resolve_outcome: unknown coin %s", token_address)
            return
        previous = self._store.coin_final_bucket(coin_id)
        bucket = self._bucket_for_return(forward_return_percent, is_rug)
        self._store.record_label(coin_id, OutcomeLabel(
            horizon_hours=horizon_hours, bucket=bucket,
            forward_return_percent=forward_return_percent, resolved_at=self._now()))
        current = self._store.coin_final_bucket(coin_id)
        if previous is None and current is not None:
            self._on_resolved(coin_id)
        elif (current is OutcomeBucket.RUG and previous is not None
              and previous is not OutcomeBucket.RUG):
            self._on_rug_upgrade(coin_id)

    def _bucket_for_return(self, ret: float, is_rug: bool) -> OutcomeBucket:
        if is_rug:
            return OutcomeBucket.RUG
        if ret >= self._ls.pump_return_percent:
            return OutcomeBucket.PUMP
        if ret <= self._ls.dump_return_percent:
            return OutcomeBucket.DUMP
        return OutcomeBucket.FLAT

    def _on_resolved(self, coin_id: int) -> None:
        """Instant learning + ensemble update the moment a coin resolves."""
        record = self._store.get_record(coin_id)
        if record is None:
            return
        bucket = record.final_bucket
        if not bucket.is_resolved:
            return

        # Instant learning: append the resolved fingerprint to the analog index.
        # A coin with no real observations extracts a zero vector; inserting it
        # would pollute the index with a meaningless "analog" that still counts
        # toward the min-neighbors gate (Rule 8 — no data is not a data point).
        fingerprint = self._extractor.extract(record.snapshots)
        if fingerprint.coverage > 0.0:
            scaled = self._scaler.transform(fingerprint.vector)
            self._analog.add(
                AnalogEntry(address=record.token.address, chain=record.token.chain,
                            bucket=bucket, resolved_at=_resolution_time(record)),
                scaled,
            )
            self._models_dirty = True
        else:
            self._logger.info("analog insert skipped for %s: empty trajectory",
                              record.token.address)

        # Grow the deployer blacklist on confirmed rugs (Section 5a / Section 7).
        # mark_deployer_counted is an atomic once-per-coin claim, so racing
        # resolution passes (overlapping backtest runs) can never double-count
        # one rug event.
        if (bucket is OutcomeBucket.RUG and record.creator
                and self._store.mark_deployer_counted(coin_id)):
            self._store.blacklist_deployer(record.creator, record.token.chain)

        # Update ensemble accuracy from the stored prediction (Section 6).
        prediction = self._store.get_prediction(coin_id)
        if prediction and not prediction.get("scored"):
            self._ensemble.record_outcome(
                prediction.get("source_labels", {}), bucket.value,
                final_label=prediction.get("predicted_label"))
            prediction["scored"] = True
            self._store.update_prediction(coin_id, prediction)
            self._ensemble_dirty = True

        self.persist()

    def _on_rug_upgrade(self, coin_id: int) -> None:
        """A later horizon confirmed RUG on an already-resolved coin.

        Slow rugs are the COMMON shape: the +1h window still looks alive
        (labels FLAT) and only a later window shows the drained pool. Instant
        learning fired once at first resolution, so without this path the
        deployer blacklist never grew for real-world rugs and the analog
        memory kept the stale non-rug label until the next full rebuild —
        blinding exactly the two mechanisms rug detection is supposed to
        sharpen (Sections 5b/7). The corrected RUG fingerprint is inserted
        immediately; the earlier entry for this coin stays until the next
        full rebuild reconstructs the index from records (their votes cancel
        at worst). Non-rug label refinements stay rebuild-only — they are not
        emergencies and per-window inserts would bloat the index.
        """
        record = self._store.get_record(coin_id)
        if record is None:
            return
        if record.creator and self._store.mark_deployer_counted(coin_id):
            count = self._store.blacklist_deployer(record.creator, record.token.chain)
            self._logger.info("rug upgrade for %s: deployer %s blacklisted (%d rug(s))",
                              record.token.address, record.creator, count)
        # Re-grade the prediction against the corrected RUG outcome, once.
        # The first grade (against the early non-rug bucket) punished exactly
        # the sources that correctly called the slow rug — the rug engine was
        # recorded WRONG for a right call, cutting its adaptive weight
        # (bug-hunt finding). The stale grade stays in the rolling window
        # (it ages out); the corrective grade enters now.
        prediction = self._store.get_prediction(coin_id)
        if (prediction and prediction.get("scored")
                and not prediction.get("rug_regraded")):
            self._ensemble.record_outcome(
                prediction.get("source_labels", {}), OutcomeBucket.RUG.value,
                final_label=prediction.get("predicted_label"))
            prediction["rug_regraded"] = True
            self._store.update_prediction(coin_id, prediction)
            self._ensemble_dirty = True
        fingerprint = self._extractor.extract(record.snapshots)
        if fingerprint.coverage > 0.0:
            scaled = self._scaler.transform(fingerprint.vector)
            self._analog.add(
                AnalogEntry(address=record.token.address, chain=record.token.chain,
                            bucket=OutcomeBucket.RUG,
                            resolved_at=_resolution_time(record)),
                scaled,
            )
            self._models_dirty = True
        self.persist()

    # ---- Learning loops (Section 4 / Section 7) ----

    def retrain_if_due(self) -> bool:
        """Warm-start the classifier on a schedule; full-rebuild on drift.

        Returns True if a (re)train happened. Three triggers (Sections 4/7):

        * first train — enough coins have resolved (``min_train_samples``);
        * cadence — every ``retrain_every_n`` newly-resolved coins;
        * **drift** — the blended verdict's rolling accuracy fell below
          ``drift_accuracy_floor`` (measured over at least ``drift_min_samples``
          graded outcomes). Drift forces the *fuller* path: scaler refit +
          full classifier retrain + analog-index/archetype rebuild, and resets
          the drift measurement so it grades the new models, not the old ones.

        A scaler refit (drift, first fit, or every ``scaler_refit_every_n``
        coins) always forces a full rebuild so every model shares one feature
        space (Rule 21).
        """
        resolved = self._store.resolved_count()
        due_by_count = (resolved - self._last_retrain_count) >= self._ls.retrain_every_n
        needs_first = (not self._classifier.is_ready) and resolved >= self._ls.min_train_samples
        drift = self._drift_detected()
        if not (due_by_count or needs_first or drift):
            return False

        needs_scaler = (drift
                        or not self._scaler.is_fitted
                        or (resolved - self._scaler_fit_count) >= self._ls.scaler_refit_every_n)
        if drift:
            self._logger.warning(
                "drift detected: ensemble accuracy %.2f < floor %.2f over %d graded "
                "outcomes — forcing full rebuild",
                self._ensemble.final_accuracy() or 0.0,
                self._ls.drift_accuracy_floor, self._ensemble.final_samples)
        self._rebuild(full=needs_scaler)
        self._models_dirty = True
        self._last_retrain_count = resolved
        if needs_scaler:
            self._scaler_fit_count = resolved
        if drift:
            # Old grades measured the replaced models; keeping them would
            # re-fire the trigger every cycle until the window rolled over.
            self._ensemble.reset_final_history()
            self._ensemble_dirty = True  # a real mutation this process owns
        self.persist()
        return True

    def _drift_detected(self) -> bool:
        """Section 7 drift monitor: blended-verdict accuracy below the floor."""
        accuracy = self._ensemble.final_accuracy()
        if accuracy is None or self._ensemble.final_samples < self._ls.drift_min_samples:
            return False
        return accuracy < self._ls.drift_accuracy_floor

    def _rebuild(self, *, full: bool) -> None:
        records = self._store.resolved_records()
        if not records:
            return
        raw = np.vstack([self._extractor.extract(r.snapshots).vector for r in records])
        buckets = [r.final_bucket for r in records]
        times = [_resolution_time(r) for r in records]

        if full or not self._scaler.is_fitted:
            self._scaler.fit(raw)
            # Rebuild the analog index in the freshly-scaled space so instant-
            # learning inserts and bulk data stay consistent.
            self._analog = AnalogMemory(now_func=self._now)
            self._analog.build_from_records(records, self._extractor, self._scaler)

        scaled = self._scaler.transform_many(raw)
        self._classifier.fit(scaled, buckets, times, warm_start=not full)
        self._archetypes.fit(scaled, buckets,
                             min_cluster_size=self._ls.archetype_min_cluster_size)
        self._logger.info("rebuild complete (%s) on %d resolved coins",
                          "full" if full else "warm-start", len(records))

    def refresh_archetypes(self) -> int:
        """Re-cluster the fingerprint set so new coin types get named (Section 7)."""
        records = self._store.resolved_records()
        if not records or not self._scaler.is_fitted:
            return 0
        raw = np.vstack([self._extractor.extract(r.snapshots).vector for r in records])
        scaled = self._scaler.transform_many(raw)
        buckets = [r.final_bucket for r in records]
        n = self._archetypes.fit(scaled, buckets,
                                 min_cluster_size=self._ls.archetype_min_cluster_size)
        self._models_dirty = True
        self.persist()
        return n

    # ---- Self-evaluation (Section 8) ----

    def get_learning_metrics(self, *, persist: bool = True) -> dict:
        """Compute the self-evaluation metrics over resolved predictions."""
        self._maybe_reload_analog()
        records: list[PredictionRecord] = []
        for record in self._store.resolved_records():
            coin_id = self._store.coin_id(record.token)
            prediction = self._store.get_prediction(coin_id) if coin_id else None
            if not prediction:
                continue
            records.append(PredictionRecord(
                predicted_distribution=prediction.get("distribution", {}),
                predicted_label=prediction.get("predicted_label", ""),
                actual_label=record.final_bucket.value,
                archetype=prediction.get("archetype"),
                novelty_flagged=prediction.get("novelty_flagged", False),
            ))
        metrics = compute_metrics(records)
        metrics["ensemble_accuracy"] = self._ensemble.accuracy_report()
        metrics["analog_memory_size"] = self._analog.size
        metrics["classifier_ready"] = self._classifier.is_ready
        # Total resolved coins regardless of whether a verdict was stored —
        # distinct from resolved_count (graded predictions only), so "449
        # coins learned, 0 graded yet" reads as what it is, not a bug.
        metrics["resolved_coins_total"] = self._store.resolved_count()
        if persist and self._state_dir != ":memory:":
            self._store.record_metrics("all", metrics)
        return metrics

    def _rug_engine(self) -> RugEngine:
        return RugEngine(self._settings.rug_signal_weights, self._settings.rug_thresholds)

    @property
    def store(self) -> LearningStore:
        return self._store
