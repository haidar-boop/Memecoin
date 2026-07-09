"""End-to-end tests for the LearningService orchestrator (Section 10)."""

from datetime import datetime, timezone

import pytest

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


def test_retrain_not_due_below_threshold():
    service = _service()
    service.record_detection("c1", "solana", detection_price_usd=1.0)
    for snap in _pump_series():
        service.capture_snapshot("c1", "solana", snap)
    service.resolve_outcome("c1", "solana", 24.0, 80.0)
    # Only one resolved coin, far below min_train_samples.
    assert service.retrain_if_due() is False
