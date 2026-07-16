"""End-to-end tests for the LearningService orchestrator (Section 10)."""

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


def test_rug_source_label_wiring_abstain_and_call():
    """The stored rug_engine source label must abstain (None) when the engine
    is not calling a rug and be 'rug' when it is — never a fabricated 'pump'
    (2026-07-16 audit fix). Verified end-to-end through evaluate_coin."""
    service = _service()
    threshold = service._ls.rug_engine_abstain_at_or_below_score

    # _pump_series fires no rug signals (score 0 -> abstain); _rug_series fires
    # liquidity-removal + dev-dumping (score > threshold -> graded as a rug).
    clean = service.evaluate_coin("clean1", "solana", _pump_series())
    dirty = service.evaluate_coin("dirty1", "solana", _rug_series())
    assert clean["rug_risk_score"] <= threshold        # abstain branch reached
    assert dirty["rug_risk_score"] > threshold          # rug-call branch reached

    def stored_rug_label(addr: str):
        coin_id = service._store.coin_id(TokenIdentity(chain="solana", address=addr))
        return service._store.get_prediction(coin_id)["source_labels"]["rug_engine"]

    assert stored_rug_label("clean1") is None            # not the old 'pump'
    assert stored_rug_label("dirty1") == "rug"
    # Invariant, regardless of exact scoring: None iff at/below the threshold.
    for addr in ("clean1", "dirty1"):
        verdict_score = (clean if addr == "clean1" else dirty)["rug_risk_score"]
        assert (stored_rug_label(addr) is None) == (verdict_score <= threshold)


def test_abstaining_rug_source_not_graded_wrong_end_to_end():
    """A clean coin that later rugs must not punish the abstaining rug engine:
    its source label was None, so grading skips it (it keeps a neutral weight
    instead of being scored wrong on a call it never made)."""
    from meme_intelligence.learning.ensemble import SOURCE_RUG

    service = _service()
    # Clean at first sighting (rug engine abstains) but it rugs later.
    service.evaluate_coin("late_rug", "solana", _pump_series())
    service.resolve_outcome("late_rug", "solana", 24.0, -95.0, is_rug=True)
    # The rug engine received no grade -> unmeasured -> neutral 0.5 basis.
    assert service._ensemble.raw_accuracy(SOURCE_RUG) is None
    assert service._ensemble.source_accuracy(SOURCE_RUG) == 0.5


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


async def test_retrain_if_due_runs_in_a_worker_thread():
    """Regression: workflow/controller.py runs retrain via
    `await asyncio.to_thread(self._learning.retrain_if_due)` (so a full
    rebuild can't stall an emergency /dump). Before the store was made
    thread-safe this exact call raised sqlite3.ProgrammingError on its
    FIRST statement (resolved_count) — every scheduled retrain failed and
    the classifier never trained. Both the quick not-due path and a real
    first train must now survive the thread hop."""
    import asyncio
    service = _service()
    assert await asyncio.to_thread(service.retrain_if_due) is False  # nothing resolved
    _seed(service, n_each=30)  # past min_train_samples -> first train is due
    assert await asyncio.to_thread(service.retrain_if_due) is True
    assert service._classifier.is_ready
    # The streamed rebuild (2026-07-15 memory fix) must fill the analog
    # index with every resolved coin, exactly like the old bulk path did.
    assert service._analog.size == 60


# ---- Training-set size cap (2026-07-15 OOM crash-loop) ---------------------

def test_rebuild_respects_max_training_records():
    """The 2026-07-15 incident: _rebuild() pulled EVERY resolved coin ever
    seen, unbounded — at 24,884 coins a single full rebuild (retrain +
    HDBSCAN over the whole history) ran the 1-vCPU/1GB droplet at ~100% CPU
    for 2.5+ minutes and was memory-killed before persist() ever ran, so
    every restart re-triggered the same unbounded rebuild forever. The cap
    bounds the analog index (and therefore the training/clustering set) to
    the newest N resolved coins, regardless of how much history exists."""
    # Cap set above min_train_samples (20) so the classifier still trains --
    # this test is about the CAP, not about starving the cold-start floor.
    env = dict(_ENV, MEMEINTEL_LEARNING_MAX_TRAINING_RECORDS="25")
    settings = Settings.from_env(env=env)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    service = LearningService(settings, store=store, now_func=lambda: NOW)
    _seed(service, n_each=15)  # 30 resolved coins, past the cap of 25
    assert store.resolved_count() == 30
    assert service.retrain_if_due() is True
    assert service._analog.size == 25                # capped, not 30
    assert service._classifier.is_ready
    # Book-keeping still tracks the TRUE total, not the capped training
    # slice -- retrain_every_n scheduling must not think fewer coins have
    # resolved than actually have.
    assert service._last_retrain_count == 30


def test_refresh_archetypes_respects_max_training_records():
    env = dict(_ENV, MEMEINTEL_LEARNING_MAX_TRAINING_RECORDS="25")
    settings = Settings.from_env(env=env)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    service = LearningService(settings, store=store, now_func=lambda: NOW)
    _seed(service, n_each=15)
    service.retrain_if_due()
    n = service.refresh_archetypes()
    assert n <= 25


def test_max_training_records_zero_is_unlimited():
    env = dict(_ENV, MEMEINTEL_LEARNING_MAX_TRAINING_RECORDS="0")
    settings = Settings.from_env(env=env)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    service = LearningService(settings, store=store, now_func=lambda: NOW)
    _seed(service, n_each=15)  # 30 resolved
    service.retrain_if_due()
    assert service._analog.size == 30                # nothing dropped


# ---- Windowed + single-query metrics (2026-07-16 audit, upgrade #2) ----

def test_metrics_join_matches_the_old_per_coin_walk():
    """Golden-value guard: the single-join metrics path must reproduce the
    pre-2026-07-16 per-coin walk (resolved_coin_ids + get_prediction +
    coin_final_bucket) exactly — this feeds the veto authority gate, a live
    safety control, so any drift is a bug."""
    from meme_intelligence.learning.metrics import PredictionRecord, compute_metrics

    service = _service()
    _seed(service)  # resolved coins WITHOUT predictions: both paths must skip
    for i in range(6):
        addr = f"g{i}"
        rugged = i % 2 == 0
        service.evaluate_coin(addr, "solana",
                              _rug_series() if rugged else _pump_series())
        service.resolve_outcome(addr, "solana", 24.0,
                                -95.0 if rugged else 80.0, is_rug=rugged)
    metrics = service.get_learning_metrics(persist=False)

    store = service._store
    records = []
    for coin_id in store.resolved_coin_ids():
        prediction = store.get_prediction(coin_id)
        if not prediction:
            continue
        bucket = store.coin_final_bucket(coin_id)
        if bucket is None:
            continue
        records.append(PredictionRecord(
            predicted_distribution=prediction.get("distribution", {}),
            predicted_label=prediction.get("predicted_label", ""),
            actual_label=bucket.value,
            archetype=prediction.get("archetype"),
            novelty_flagged=prediction.get("novelty_flagged", False),
        ))
    expected = compute_metrics(records)
    for key, value in expected.items():
        assert metrics[key] == value, f"metrics[{key!r}] drifted from the old walk"


def test_metrics_recent_window_grades_only_recent_predictions():
    """The 'recent' section grades only predictions MADE inside the window,
    keyed on the prediction's own created_at — a coin from an older era must
    not appear in the current-regime numbers even after later label writes
    bump its updated_at."""
    from datetime import timedelta

    env = dict(_ENV, MEMEINTEL_LEARNING_METRICS_WINDOW_DAYS="7")
    settings = Settings.from_env(env=env)
    clock = {"now": NOW - timedelta(days=30)}
    store = LearningStore(":memory:", now_func=lambda: clock["now"])
    service = LearningService(settings, store=store, now_func=lambda: clock["now"])

    service.evaluate_coin("old1", "solana", _rug_series())      # 30 days ago
    service.resolve_outcome("old1", "solana", 24.0, -90.0, is_rug=True)
    clock["now"] = NOW
    service.evaluate_coin("new1", "solana", _rug_series())      # inside window
    service.resolve_outcome("new1", "solana", 24.0, -90.0, is_rug=True)
    # A LATE label write on the old coin bumps its updated_at but must not
    # drag its old prediction into the window (the era key is created_at).
    clock["now"] = NOW + timedelta(hours=1)
    service.resolve_outcome("old1", "solana", 168.0, -99.0, is_rug=True)

    metrics = service.get_learning_metrics(persist=False)
    assert metrics["resolved_count"] == 2            # lifetime sees both
    recent = metrics["recent"]
    assert recent["window_days"] == 7.0
    assert recent["resolved_count"] == 1             # only the new-era one


def test_metrics_window_zero_disables_recent_section():
    env = dict(_ENV, MEMEINTEL_LEARNING_METRICS_WINDOW_DAYS="0")
    settings = Settings.from_env(env=env)
    store = LearningStore(":memory:", now_func=lambda: NOW)
    service = LearningService(settings, store=store, now_func=lambda: NOW)
    service.evaluate_coin("w0", "solana", _rug_series())
    service.resolve_outcome("w0", "solana", 24.0, -90.0, is_rug=True)
    metrics = service.get_learning_metrics(persist=False)
    assert "recent" not in metrics
