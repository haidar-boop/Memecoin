"""End-to-end tests for the LearningService orchestrator (Section 10)."""

import os
from datetime import datetime, timezone


from meme_intelligence.config.settings import Settings
from meme_intelligence.core.models import SecurityProfile, TokenIdentity
from meme_intelligence.learning.service import LearningService
from meme_intelligence.learning.store import LearningStore

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)

_ENV = {
    "MEMEINTEL_LEARNING_STATE_DIR": ":memory:",
    "MEMEINTEL_LEARNING_MIN_TRAIN_SAMPLES": "20",
    "MEMEINTEL_LEARNING_RETRAIN_EVERY_N": "5",
    "MEMEINTEL_LEARNING_ARCHETYPE_MIN_CLUSTER_SIZE": "5",
    "MEMEINTEL_LEARNING_MIN_ANALOG_NEIGHBORS": "3",
    "MEMEINTEL_LEARNING_KNN_NEIGHBORS": "15",
    "MEMEINTEL_LEARNING_COLD_START_SAMPLES": "10",
    "MEMEINTEL_LEARNING_ACCURACY_WINDOW": "50",
    "MEMEINTEL_LEARNING_SCALER_REFIT_EVERY_N": "1000",
    "MEMEINTEL_LEARNING_DRIFT_MIN_SAMPLES": "5",
}

_EXPECTED_KEYS = {
    "token_address", "chain", "final_probabilities", "rug_risk_score",
    "rug_signals_fired", "nearest_analogs", "matched_archetype", "novelty_score",
    "ensemble_weights", "model_confidence", "sample_size",
}


def _pump_series():
    return [{"age_seconds": i * 60, "price_usd": 1.0 * (1.2 ** i),
             "liquidity_usd": 5000 + 500 * i, "market_cap_usd": 20000,
             "volume_1h_usd": 4000, "holder_count": 50 + 10 * i,
             "buys": 20, "sells": 5, "top10_holder_percent": 20.0} for i in range(6)]


def _rug_series():
    return [{"age_seconds": i * 60, "price_usd": 1.0,
             "liquidity_usd": 8000 * (0.5 ** i), "market_cap_usd": 30000,
             "volume_1h_usd": 9000, "holder_count": 8, "buys": 3, "sells": 15,
             "top10_holder_percent": 85.0, "dev_outflow_usd": 100 * i,
             "liquidity_event_usd": (-4000 if i > 0 else 0)} for i in range(6)]


def _service():
    settings = Settings.from_env(env=_ENV)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    return LearningService(settings, store=store, now_func=lambda: NOW)


def _seed(service, n_each=15):
    """Seed n_each resolved PUMP coins and n_each resolved RUG coins."""
    for i in range(n_each):
        addr = f"pump{i}"
        service.record_detection(addr, "solana", detection_price_usd=1.0)
        for snap in _pump_series():
            service.capture_snapshot(addr, "solana", snap)
        service.resolve_outcome(addr, "solana", 24.0, 80.0)
    for i in range(n_each):
        addr = f"rug{i}"
        service.record_detection(addr, "solana", detection_price_usd=1.0, creator="devX")
        for snap in _rug_series():
            service.capture_snapshot(addr, "solana", snap)
        service.resolve_outcome(addr, "solana", 24.0, -95.0, is_rug=True)


def test_cold_start_returns_contract_shape():
    service = _service()
    verdict = service.evaluate_coin("new1", "solana", _pump_series())
    assert set(verdict) == _EXPECTED_KEYS
    # Displayed probabilities are rounded to 4 dp, so allow small rounding drift.
    assert abs(sum(verdict["final_probabilities"].values()) - 1.0) < 1e-3
    assert verdict["sample_size"] == 0
    # Cold start -> low confidence (Section 11).
    assert verdict["model_confidence"] < 0.3


def test_lifecycle_resolution_grows_analog_memory():
    service = _service()
    service.record_detection("c1", "solana", detection_price_usd=1.0)
    for snap in _rug_series():
        service.capture_snapshot("c1", "solana", snap)
    assert service.store.resolved_count() == 0
    service.resolve_outcome("c1", "solana", 24.0, -90.0, is_rug=True)
    assert service.store.resolved_count() == 1
    # Instant learning: the resolved coin entered the analog index.
    assert service.get_learning_metrics(persist=False)["analog_memory_size"] == 1


def test_end_to_end_learns_to_separate_rug_from_pump():
    service = _service()
    _seed(service)
    assert service.retrain_if_due() is True

    rug_verdict = service.evaluate_coin("live_rug", "solana", _rug_series())
    assert max(rug_verdict["final_probabilities"], key=rug_verdict["final_probabilities"].get) == "rug"
    assert rug_verdict["rug_risk_score"] > 0
    assert rug_verdict["nearest_analogs"]  # analogs surfaced
    assert rug_verdict["sample_size"] == 30

    pump_verdict = service.evaluate_coin("live_pump", "solana", _pump_series())
    assert max(pump_verdict["final_probabilities"], key=pump_verdict["final_probabilities"].get) == "pump"
    assert pump_verdict["rug_risk_score"] == 0


def test_nearest_analogs_report_resolved_rugs_for_rug_coin():
    service = _service()
    _seed(service)
    service.retrain_if_due()
    verdict = service.evaluate_coin("live_rug2", "solana", _rug_series())
    top = verdict["nearest_analogs"][0]
    assert top["resolved_as"] == "rug"
    assert 0.0 <= top["similarity"] <= 1.0


def test_deployer_blacklist_flags_repeat_creator():
    service = _service()
    # One confirmed rug from creator devX.
    service.record_detection("r1", "solana", detection_price_usd=1.0, creator="devX")
    for snap in _rug_series():
        service.capture_snapshot("r1", "solana", snap)
    service.resolve_outcome("r1", "solana", 24.0, -90.0, is_rug=True)
    # A brand-new coin from the same deployer must fire the reputation signal.
    verdict = service.evaluate_coin("r2", "solana", _pump_series(), creator="devX")
    assert "deployer_blacklisted" in verdict["rug_signals_fired"]


def test_rug_engine_uses_security_profile():
    service = _service()
    sec = SecurityProfile(token=TokenIdentity(chain="solana", address="s1"),
                          source="goplus", is_honeypot=True, is_mintable=True)
    verdict = service.evaluate_coin("s1", "solana", _pump_series(), security=sec)
    assert "unsellable" in verdict["rug_signals_fired"]
    assert "mint_authority_active" in verdict["rug_signals_fired"]


def test_metrics_structure_after_predictions_resolve():
    service = _service()
    _seed(service)
    service.retrain_if_due()
    # Evaluate then resolve a coin so a graded prediction exists.
    service.evaluate_coin("m1", "solana", _rug_series())
    service.resolve_outcome("m1", "solana", 24.0, -90.0, is_rug=True)
    metrics = service.get_learning_metrics(persist=False)
    assert metrics["resolved_count"] >= 1
    assert "rug" in metrics
    assert "ensemble_accuracy" in metrics
    assert metrics["classifier_ready"] is True


def test_evaluate_only_coin_resolves_into_real_analog_entry():
    """A coin evaluated without capture_snapshot must still yield a real
    fingerprint in the analog index once it resolves (not a zero vector)."""
    service = _service()
    service.evaluate_coin("x1", "solana", _rug_series())  # evaluate-only
    service.resolve_outcome("x1", "solana", 24.0, -90.0, is_rug=True)
    assert service.get_learning_metrics(persist=False)["analog_memory_size"] == 1
    # An identical live coin finds it at near-perfect similarity.
    neighbors = service.evaluate_coin("x2", "solana", _rug_series())["nearest_analogs"]
    assert neighbors and neighbors[0]["similarity"] > 0.5
    assert neighbors[0]["resolved_as"] == "rug"


def test_persistence_survives_restart(tmp_path):
    """Models + DB persist to disk and a fresh service reloads them (Section 9)."""
    env = dict(_ENV)
    env["MEMEINTEL_LEARNING_STATE_DIR"] = str(tmp_path)
    settings = Settings.from_env(env=env)

    # First service: seed, train, persist (store lives on disk under state_dir).
    svc1 = LearningService(settings, now_func=lambda: NOW)
    _seed(svc1)
    assert svc1.retrain_if_due() is True
    svc1.persist()
    baseline = svc1.evaluate_coin("probe", "solana", _rug_series())
    svc1.store.close()

    # Second service on the same directory reloads everything.
    svc2 = LearningService(settings, now_func=lambda: NOW)
    assert svc2.store.resolved_count() == 30
    reloaded = svc2.evaluate_coin("probe2", "solana", _rug_series())
    assert reloaded["sample_size"] == 30
    # Same trained models -> same rug call and analog memory.
    assert (max(reloaded["final_probabilities"], key=reloaded["final_probabilities"].get)
            == max(baseline["final_probabilities"], key=baseline["final_probabilities"].get))
    assert reloaded["nearest_analogs"][0]["resolved_as"] == "rug"


def test_drift_triggers_full_rebuild_and_resets_measurement():
    """Section 7: blended-verdict accuracy below the floor forces a rebuild."""
    service = _service()
    _seed(service)
    assert service.retrain_if_due() is True  # first train; counters catch up
    assert service.retrain_if_due() is False  # nothing due

    # Simulate a stale meta: six graded finals, all wrong (floor is 0.40,
    # drift_min_samples is 5 in _ENV).
    for _ in range(6):
        service._ensemble.record_outcome({}, "pump", final_label="rug")
    assert service._ensemble.final_accuracy() == 0.0

    assert service.retrain_if_due() is True   # drift fired
    assert service._ensemble.final_samples == 0  # measurement reset
    assert service.retrain_if_due() is False  # does not re-fire next cycle


def test_drift_needs_min_samples():
    """A couple of bad calls must not trigger a rebuild (noise guard)."""
    service = _service()
    _seed(service)
    service.retrain_if_due()
    for _ in range(3):  # below drift_min_samples=5
        service._ensemble.record_outcome({}, "pump", final_label="rug")
    assert service.retrain_if_due() is False


def test_resolution_grades_blended_verdict():
    """resolve_outcome feeds the drift monitor via the stored final label."""
    service = _service()
    _seed(service)
    service.retrain_if_due()
    service.evaluate_coin("g1", "solana", _rug_series())
    service.resolve_outcome("g1", "solana", 24.0, -95.0, is_rug=True)
    assert service._ensemble.final_samples == 1
    assert service._ensemble.final_accuracy() == 1.0  # predicted rug, was rug


def test_feature_version_mismatch_discards_models_and_rebuilds(tmp_path):
    """Artifacts saved under an older fingerprint definition are discarded on
    load (they live in a different feature space) and rebuilt from the stored
    raw snapshots — learning data is never lost (Rule 18)."""
    import joblib

    env = dict(_ENV)
    env["MEMEINTEL_LEARNING_STATE_DIR"] = str(tmp_path)
    settings = Settings.from_env(env=env)

    svc1 = LearningService(settings, now_func=lambda: NOW)
    _seed(svc1)
    assert svc1.retrain_if_due() is True
    svc1.persist()
    svc1.store.close()

    # Simulate artifacts from an older feature space.
    state = joblib.load(str(tmp_path / "state.joblib"))
    state["feature_version"] = 1
    joblib.dump(state, str(tmp_path / "state.joblib"))

    svc2 = LearningService(settings, now_func=lambda: NOW)
    # Stale models were discarded...
    metrics = svc2.get_learning_metrics(persist=False)
    assert metrics["classifier_ready"] is False
    assert metrics["analog_memory_size"] == 0
    # ...but the raw records survived, and one retrain restores everything.
    assert svc2.store.resolved_count() == 30
    assert svc2.retrain_if_due() is True
    metrics = svc2.get_learning_metrics(persist=False)
    assert metrics["classifier_ready"] is True
    assert metrics["analog_memory_size"] == 30


def test_short_trajectory_lowers_confidence():
    """A 1-snapshot coin barely has a shape — confidence must scale down
    (min_snapshots_for_confidence, Section 11)."""
    service = _service()
    _seed(service)
    service.retrain_if_due()
    full = service.evaluate_coin("conf_full", "solana", _rug_series())       # 6 snaps
    single = service.evaluate_coin("conf_one", "solana", _rug_series()[:1])  # 1 snap
    # Default min_snapshots_for_confidence=3 -> the 1-snapshot factor is 1/3.
    assert single["model_confidence"] < full["model_confidence"]
    assert single["model_confidence"] <= 1 / 3 + 1e-9


def test_evaluate_only_coin_persists_creator_for_blacklist():
    """A coin known only through evaluate_coin still records its creator, so a
    later rug grows the deployer blacklist (upgrade #2)."""
    service = _service()
    service.evaluate_coin("e1", "solana", _rug_series(), creator="devZ")
    service.resolve_outcome("e1", "solana", 24.0, -95.0, is_rug=True)
    assert service.store.deployer_rug_count("devZ", "solana") == 1


def test_slow_rug_upgrade_grows_blacklist_and_analog_memory():
    """Bug-hunt regression: a coin that resolved FLAT at +1h and was confirmed
    RUG at +24h never blacklisted its deployer or taught the analog memory —
    the common slow-rug shape was invisible to instant learning."""
    service = _service()
    service.record_detection("slowrug", "solana", detection_price_usd=1.0, creator="devSlow")
    for snap in _rug_series():
        service.capture_snapshot("slowrug", "solana", snap)
    # +1h window: still looks alive -> FLAT (coin becomes resolved).
    service.resolve_outcome("slowrug", "solana", 1.0, 5.0)
    assert service.store.deployer_rug_count("devSlow", "solana") == 0
    size_before = service.get_learning_metrics(persist=False)["analog_memory_size"]
    # +24h window: pool drained -> confirmed rug.
    service.resolve_outcome("slowrug", "solana", 24.0, -95.0, is_rug=True)
    assert service.store.deployer_rug_count("devSlow", "solana") == 1
    after = service.get_learning_metrics(persist=False)["analog_memory_size"]
    assert after == size_before + 1  # corrected RUG fingerprint inserted


def test_rug_upgrade_does_not_double_blacklist():
    """First-resolution RUG then a later RUG window must blacklist exactly once."""
    service = _service()
    service.record_detection("fastrug", "solana", detection_price_usd=1.0, creator="devFast")
    for snap in _rug_series():
        service.capture_snapshot("fastrug", "solana", snap)
    service.resolve_outcome("fastrug", "solana", 1.0, -95.0, is_rug=True)
    service.resolve_outcome("fastrug", "solana", 24.0, -99.0, is_rug=True)
    assert service.store.deployer_rug_count("devFast", "solana") == 1


def test_stale_process_persist_does_not_clobber_graded_ensemble(tmp_path):
    """Bug-hunt: the monitor (which never grades) persisted its stale
    in-memory ensemble over the cron's graded accuracy history — and since
    predictions are durably marked scored, the grades could never be
    regenerated. Only a process that actually graded writes ensemble.joblib."""
    env = dict(_ENV)
    env["MEMEINTEL_LEARNING_STATE_DIR"] = str(tmp_path)
    settings = Settings.from_env(env=env)

    # "Cron" process: grades one outcome, persists.
    cron = LearningService(settings, now_func=lambda: NOW)
    cron.evaluate_coin("g1", "solana", _rug_series())
    cron.resolve_outcome("g1", "solana", 24.0, -95.0, is_rug=True)  # persists
    cron.store.close()

    # "Monitor" process: loads the graded state, never grades, persists.
    monitor = LearningService(settings, now_func=lambda: NOW)
    from meme_intelligence.learning.ensemble import AdaptiveEnsemble
    monitor._ensemble = AdaptiveEnsemble(window=50)  # simulate stale/blank copy
    monitor.persist()  # must NOT overwrite the graded file
    monitor.store.close()

    reloaded = AdaptiveEnsemble.load(str(tmp_path / "ensemble.joblib"))
    assert reloaded.final_samples == 1  # cron's grade survived


def test_racing_resolutions_blacklist_deployer_once():
    """Bug-hunt: the check-then-act in resolve_outcome let two overlapping
    backtest runs both fire instant learning and durably record TWO rugs for
    one rug event. The atomic once-per-coin claim makes it exactly one."""
    service = _service()
    service.record_detection("r1", "solana", detection_price_usd=1.0, creator="devR")
    for snap in _rug_series():
        service.capture_snapshot("r1", "solana", snap)
    coin_id = service.store.coin_id(
        __import__("meme_intelligence.core.models", fromlist=["TokenIdentity"])
        .TokenIdentity(chain="solana", address="r1"))
    # Simulate the race: both passes believe they are first.
    service.resolve_outcome("r1", "solana", 1.0, -95.0, is_rug=True)
    service._on_resolved(coin_id)  # second racing invocation
    assert service.store.deployer_rug_count("devR", "solana") == 1


def test_slow_rug_upgrade_regrades_the_prediction():
    """Bug-hunt: a slow rug graded the sources against the early FLAT bucket
    and never re-graded — the rug engine was recorded WRONG for a correct rug
    call. The upgrade now adds a corrective grade (once)."""
    service = _service()
    service.evaluate_coin("s1", "solana", _rug_series())
    service.resolve_outcome("s1", "solana", 1.0, 5.0)               # FLAT: grade 1
    assert service._ensemble.final_samples == 1
    service.resolve_outcome("s1", "solana", 24.0, -95.0, is_rug=True)  # upgrade
    assert service._ensemble.final_samples == 2                     # regraded once
    service.resolve_outcome("s1", "solana", 168.0, -99.0, is_rug=True)
    assert service._ensemble.final_samples == 2                     # never twice


def test_empty_trajectory_resolution_does_not_pollute_analog_index():
    """Bug-hunt regression: a coin resolved with zero snapshots extracted a
    zero-vector fingerprint that entered the analog index as a meaningless
    'analog' counting toward the min-neighbors gate."""
    service = _service()
    service.record_detection("ghost", "solana", detection_price_usd=1.0)
    service.resolve_outcome("ghost", "solana", 24.0, -90.0, is_rug=True)
    assert service.store.resolved_count() == 1  # labeled and graded...
    assert service.get_learning_metrics(persist=False)["analog_memory_size"] == 0


def test_retrain_not_due_below_threshold():
    service = _service()
    service.record_detection("c1", "solana", detection_price_usd=1.0)
    for snap in _pump_series():
        service.capture_snapshot("c1", "solana", snap)
    service.resolve_outcome("c1", "solana", 24.0, 80.0)
    # Only one resolved coin, far below min_train_samples.
    assert service.retrain_if_due() is False


# ---- Frozen-analog-memory regression suite (monitor vs cron) ----------------
#
# The monitor and the backtest cron share one state dir. Only the cron mutates
# models (it resolves coins); the monitor only reads. Two bugs made the
# monitor's analog memory freeze at its boot-time size for a whole run:
#   1. the monitor's shutdown persist() wrote its stale boot-time index over
#      whatever the cron had appended since — permanently resetting the file;
#   2. the monitor never reloaded the index the cron kept growing.
# The fix: a _models_dirty ownership guard on the model-side writes, plus a
# cheap mtime-driven _maybe_reload_analog at both read entry points.


def _disk_settings(tmp_path):
    env = dict(_ENV)
    env["MEMEINTEL_LEARNING_STATE_DIR"] = str(tmp_path)
    return Settings.from_env(env=env)


def _resolve_batch(service, prefix, n):
    """Resolve n real-trajectory rug coins (each enters the analog index)."""
    for i in range(n):
        addr = f"{prefix}{i}"
        service.record_detection(addr, "solana", detection_price_usd=1.0)
        for snap in _rug_series():
            service.capture_snapshot(addr, "solana", snap)
        service.resolve_outcome(addr, "solana", 24.0, -95.0, is_rug=True)


def _ondisk_analog_size(tmp_path):
    from meme_intelligence.learning.analog import AnalogMemory

    memory = AnalogMemory.load(str(tmp_path / "index.faiss"),
                               str(tmp_path / "index_meta.joblib"),
                               now_func=lambda: NOW)
    return memory.size


def _write_fresh_index(tmp_path, n):
    """Build and save a brand-new analog index of n entries, simulating the
    cron having grown (or replaced) the on-disk file with a newer mtime."""
    import numpy as np

    from meme_intelligence.learning.analog import AnalogEntry, AnalogMemory
    from meme_intelligence.learning.features import FEATURE_DIM
    from meme_intelligence.learning.models import OutcomeBucket

    memory = AnalogMemory(now_func=lambda: NOW)
    for i in range(n):
        vec = np.ones(FEATURE_DIM, dtype=np.float32) * (i + 1)
        memory.add(AnalogEntry(address=f"fresh{i}", chain="solana",
                               bucket=OutcomeBucket.RUG, resolved_at=NOW), vec)
    memory.save(str(tmp_path / "index.faiss"), str(tmp_path / "index_meta.joblib"))
    return n


def test_monitor_persist_does_not_clobber_grown_index(tmp_path):
    """Regression: a monitor-style service (never resolves, _models_dirty stays
    False) whose shutdown persist() runs must NOT overwrite an index another
    process grew. Otherwise its stale boot copy resets the file forever."""
    settings = _disk_settings(tmp_path)

    # Cron process A: resolves coins, grows + persists the index.
    cron = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron, "a", 5)
    cron.persist()
    cron.store.close()
    assert _ondisk_analog_size(tmp_path) == 5

    # Monitor process B: loads the index, never resolves anything.
    monitor = LearningService(settings, now_func=lambda: NOW)
    assert monitor._analog.size == 5            # boot copy
    assert monitor._models_dirty is False
    assert monitor._analog_dirty is False       # never mutated the index

    # Cron keeps growing the on-disk index while the monitor is up.
    _write_fresh_index(tmp_path, 9)
    assert _ondisk_analog_size(tmp_path) == 9

    # Monitor shutdown persist must leave the grown file untouched.
    monitor.persist()
    assert _ondisk_analog_size(tmp_path) == 9   # the cron's 9, NOT the boot 5
    monitor.store.close()


def test_maybe_reload_analog_picks_up_growth_without_restart(tmp_path):
    """Regression: the monitor must reflect index growth the cron wrote,
    without a restart — both /mind (get_learning_metrics) and live verdicts
    (evaluate_coin) reload on a cheap mtime check."""
    settings = _disk_settings(tmp_path)
    cron = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron, "a", 5)
    cron.persist()
    cron.store.close()

    monitor = LearningService(settings, now_func=lambda: NOW)
    assert monitor.get_learning_metrics(persist=False)["analog_memory_size"] == 5

    # The cron grows the file (new mtime + more coins) while the monitor runs.
    _write_fresh_index(tmp_path, 9)

    # /mind reflects the larger memory without a restart...
    assert monitor.get_learning_metrics(persist=False)["analog_memory_size"] == 9
    # ...and so does the live verdict path.
    _write_fresh_index(tmp_path, 12)
    monitor.evaluate_coin("live", "solana", _rug_series())
    assert monitor._analog.size == 12
    monitor.store.close()


def test_mutating_process_still_persists_grown_index(tmp_path):
    """A process that mutates the analog index (instant learning on resolve)
    flushes the grown index to disk — the guard only silences pure readers.
    Each resolve persists internally and clears _analog_dirty, so the on-disk
    file always reflects the latest growth."""
    settings = _disk_settings(tmp_path)
    cron = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron, "a", 5)
    assert cron._analog_dirty is False          # flushed by the per-resolve persist
    assert _ondisk_analog_size(tmp_path) == 5

    # Resolve more — each writer resolve flushes the grown index.
    _resolve_batch(cron, "b", 3)
    cron.persist()
    assert _ondisk_analog_size(tmp_path) == 8
    cron.store.close()


def test_feature_version_mismatch_blocks_analog_reload(tmp_path):
    """After a feature-version mismatch the on-disk index is in an old feature
    space; _maybe_reload_analog must never adopt it, even if the file changes."""
    import joblib

    settings = _disk_settings(tmp_path)
    svc1 = LearningService(settings, now_func=lambda: NOW)
    _seed(svc1)
    assert svc1.retrain_if_due() is True
    svc1.persist()
    svc1.store.close()

    state = joblib.load(str(tmp_path / "state.joblib"))
    state["feature_version"] = 1
    joblib.dump(state, str(tmp_path / "state.joblib"))

    svc2 = LearningService(settings, now_func=lambda: NOW)
    assert svc2._analog_reload_ok is False
    assert svc2.get_learning_metrics(persist=False)["analog_memory_size"] == 0

    # Even a fresh (newer-mtime) index on disk must not be reloaded.
    _write_fresh_index(tmp_path, 7)
    assert svc2.get_learning_metrics(persist=False)["analog_memory_size"] == 0
    svc2.store.close()


def test_torn_index_reload_is_swallowed_and_retries(tmp_path):
    """A torn/mid-write index during reload keeps the current in-memory copy
    (no crash) and retries on the next call (Rule 7)."""
    settings = _disk_settings(tmp_path)
    cron = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron, "a", 5)
    cron.persist()
    cron.store.close()

    monitor = LearningService(settings, now_func=lambda: NOW)
    assert monitor._analog.size == 5

    # Simulate a torn write: garbage in index.faiss, and bump the meta mtime
    # (the reload freshness key) so a reload is actually attempted.
    with open(str(tmp_path / "index.faiss"), "wb") as fh:
        fh.write(b"not a real faiss index")
    meta = str(tmp_path / "index_meta.joblib")
    st = os.stat(meta)
    os.utime(meta, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))

    # Reload is attempted, fails, and is swallowed: the copy stays put.
    assert monitor.get_learning_metrics(persist=False)["analog_memory_size"] == 5

    # A subsequent valid write is picked up on the next call (retry).
    _write_fresh_index(tmp_path, 9)
    assert monitor.get_learning_metrics(persist=False)["analog_memory_size"] == 9
    monitor.store.close()


def test_unchanged_mtime_is_a_cheap_noop_reload(tmp_path):
    """When the index file has not changed, _maybe_reload_analog does no reload
    work — it keeps the exact same in-memory object (the cheap mtime path)."""
    settings = _disk_settings(tmp_path)
    cron = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron, "a", 5)
    cron.persist()
    cron.store.close()

    monitor = LearningService(settings, now_func=lambda: NOW)
    before = monitor._analog
    monitor._maybe_reload_analog()
    monitor.get_learning_metrics(persist=False)
    monitor.evaluate_coin("live", "solana", _rug_series())
    assert monitor._analog is before   # never reloaded
    monitor.store.close()


# The monitor calls retrain_if_due every scan cycle (workflow controller). The
# tests above simulate a "monitor" that only reads; these exercise the real
# controller path, where the monitor DOES retrain — the case that reintroduced
# the frozen-mind / clobber bugs when a single _models_dirty flag latched the
# analog-index write and reload for a mere classifier warm-start.


def test_monitor_warmstart_retrain_does_not_clobber_or_freeze(tmp_path):
    """Regression (frozen mind): a warm-start retrain in the monitor leaves the
    analog index untouched, so it must NOT clobber the cron's grown on-disk
    index and must keep reloading the cron's later growth."""
    settings = _disk_settings(tmp_path)

    # Cron: seed + first (full) train, growing and persisting the index.
    cron = LearningService(settings, now_func=lambda: NOW)
    _seed(cron)                              # 30 resolved coins
    assert cron.retrain_if_due() is True     # first train: full rebuild, fits scaler
    cron.persist()
    cron.store.close()
    assert _ondisk_analog_size(tmp_path) == 30

    # Monitor: boots as a reader (scaler fitted, classifier ready, index=30).
    monitor = LearningService(settings, now_func=lambda: NOW)
    assert monitor._analog.size == 30
    assert monitor._analog_dirty is False

    # Cron keeps resolving: 5 new coins reach the shared DB and on-disk index.
    cron2 = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron2, "grow", 5)
    cron2.store.close()
    assert _ondisk_analog_size(tmp_path) == 35

    # The monitor's periodic retrain fires as a warm-start (scaler already fit,
    # 5 new resolved >= retrain_every_n, no drift, no scaler refit due).
    assert monitor.retrain_if_due() is True
    assert monitor._analog_dirty is False            # warm-start left it clean
    assert _ondisk_analog_size(tmp_path) == 35        # NOT clobbered back to 30

    # Reload still works after the retrain: further cron growth reaches verdicts.
    cron3 = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron3, "more", 5)
    cron3.store.close()
    assert _ondisk_analog_size(tmp_path) == 40
    monitor.evaluate_coin("live", "solana", _rug_series())
    assert monitor._analog.size == 40                 # reloaded, not frozen at 30
    monitor.store.close()


def test_monitor_drift_retrain_does_not_clobber_graded_ensemble(tmp_path):
    """Regression: the monitor's drift path resets its in-memory ensemble but
    must NOT persist that reset over the cron's graded accuracy history — the
    monitor never grades the ensemble, so it does not own it."""
    settings = _disk_settings(tmp_path)

    # Cron: seed, train, then grade six blended verdicts through the real path.
    cron = LearningService(settings, now_func=lambda: NOW)
    _seed(cron)
    cron.retrain_if_due()
    for i in range(6):
        addr = f"graded{i}"
        cron.evaluate_coin(addr, "solana", _rug_series())
        cron.resolve_outcome(addr, "solana", 24.0, -95.0, is_rug=True)
    cron.persist()
    cron.store.close()

    from meme_intelligence.learning.ensemble import AdaptiveEnsemble
    assert AdaptiveEnsemble.load(str(tmp_path / "ensemble.joblib")).final_samples == 6

    # Monitor: boots, then a stale/bad in-memory window drives drift. Injecting
    # via the ensemble object (not resolve_outcome) mirrors the monitor, which
    # never grades — so _ensemble_dirty stays False.
    monitor = LearningService(settings, now_func=lambda: NOW)
    monitor._ensemble.reset_final_history()
    for _ in range(6):
        monitor._ensemble.record_outcome({}, "pump", final_label="rug")  # all wrong
    assert monitor._ensemble_dirty is False

    assert monitor.retrain_if_due() is True           # drift fires the rebuild
    assert monitor._ensemble.final_samples == 0       # in-memory history reset
    monitor.store.close()

    # The cron's graded ensemble on disk survived (not overwritten with 0).
    assert AdaptiveEnsemble.load(str(tmp_path / "ensemble.joblib")).final_samples == 6


def test_reload_rejects_incompatible_feature_version(tmp_path):
    """Split deploy: a peer rebuilds the on-disk index in a NEW feature space
    while this process still runs old code. _maybe_reload_analog must refuse the
    incompatible index rather than query it with the wrong scaler (Rule 8)."""
    import joblib

    from meme_intelligence.learning.features import FEATURE_VERSION

    settings = _disk_settings(tmp_path)
    cron = LearningService(settings, now_func=lambda: NOW)
    _resolve_batch(cron, "a", 5)
    cron.persist()
    cron.store.close()

    monitor = LearningService(settings, now_func=lambda: NOW)
    assert monitor._analog.size == 5

    # Simulate the peer having upgraded FEATURE_VERSION: bump the meta's version
    # (and its mtime, the reload freshness key).
    meta_path = str(tmp_path / "index_meta.joblib")
    payload = joblib.load(meta_path)
    payload["feature_version"] = FEATURE_VERSION + 1
    joblib.dump(payload, meta_path)

    monitor.evaluate_coin("live", "solana", _rug_series())
    assert monitor._analog_reload_ok is False         # refused and latched off
    assert monitor._analog.size == 5                  # kept its own compatible copy
    monitor.store.close()


# ---- Train/serve skew fix (2026-07-28): evaluate on the stored trajectory ----


def test_include_stored_history_merges_trajectory_into_the_fingerprint():
    """The models are trained on full-trajectory fingerprints, so a live
    evaluation handed ONE snapshot fed them a shapeless vector (all slopes
    and volatility zero) the training set never contained. With
    include_stored_history the accumulated trajectory is used instead."""
    service = _service()
    series = _rug_series()
    # The scanner's real pattern: capture each observation as it arrives.
    for snap in series:
        service.capture_snapshot("Traj1", "solana", snap)

    from meme_intelligence.learning.models import CoinSnapshot

    latest = [CoinSnapshot.from_dict(series[-1])]
    merged = service._merge_stored_history("Traj1", "solana", latest)
    assert len(latest) == 1 and len(merged) == len(series)

    # The actual defect: a one-snapshot fingerprint has NO shape — every
    # slope/volatility summary collapses to zero. The merged trajectory
    # carries the falling-liquidity arc the models were trained on.
    shapeless = service._extractor.extract(latest).vector
    trajectory = service._extractor.extract(merged).vector
    assert not (shapeless == trajectory).all()
    names = service._extractor.feature_names
    slope_idx = [i for i, n in enumerate(names) if "slope" in n]
    assert slope_idx, "expected slope features in the fingerprint"
    assert all(shapeless[i] == 0.0 for i in slope_idx)      # no shape at all
    assert any(trajectory[i] != 0.0 for i in slope_idx)     # real trajectory shape
    service.store.close()


def test_include_stored_history_dedupes_the_caller_snapshot():
    """The scanner captures the current snapshot and THEN evaluates, so the
    fresh observation is already stored: it must appear once, not twice."""
    service = _service()
    series = _rug_series()
    for snap in series:
        service.capture_snapshot("Dedup1", "solana", snap)

    merged = service._merge_stored_history(
        "Dedup1", "solana",
        [__import__("meme_intelligence.learning.models", fromlist=["CoinSnapshot"])
         .CoinSnapshot.from_dict(series[-1])])
    ages = [s.age_seconds for s in merged]
    assert ages == sorted(ages)
    assert len(ages) == len(set(ages)) == len(series)
    service.store.close()


def test_include_stored_history_is_bounded_by_setting():
    env = dict(_ENV, MEMEINTEL_LEARNING_MAX_EVALUATION_SNAPSHOTS="3")
    settings = Settings.from_env(env=env)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    service = LearningService(settings, store=store, now_func=lambda: NOW)
    for snap in _rug_series():          # 6 snapshots
        service.capture_snapshot("Cap1", "solana", snap)

    from meme_intelligence.learning.models import CoinSnapshot
    merged = service._merge_stored_history(
        "Cap1", "solana", [CoinSnapshot.from_dict({"age_seconds": 999.0})])
    assert len(merged) <= 3
    assert merged[-1].age_seconds == 999.0   # newest kept
    store.close()


def test_unknown_coin_and_store_failure_fall_back_to_caller_snapshots():
    """Fails OPEN: the ARMED p(rug) veto must never break on a history read."""
    from meme_intelligence.learning.models import CoinSnapshot

    service = _service()
    fresh = [CoinSnapshot.from_dict({"age_seconds": 10.0, "price_usd": 1.0})]
    assert service._merge_stored_history("NeverSeen", "solana", fresh) == fresh

    class BoomStore:
        def coin_id(self, token):
            raise RuntimeError("db gone")

    service._store = BoomStore()
    assert service._merge_stored_history("Any", "solana", fresh) == fresh
