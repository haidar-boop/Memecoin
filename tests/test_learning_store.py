"""Tests for the mind-layer SQLite persistence (Section 9)."""

from datetime import datetime, timezone

import pytest

from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.learning.models import (
    CoinSnapshot,
    OutcomeBucket,
    OutcomeLabel,
    RugSignal,
)
from meme_intelligence.learning.store import LearningStore

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="Mint111", symbol="MEME", name="Test")
TOKEN2 = TokenIdentity(chain="solana", address="Mint222", symbol="DOGE", name="Two")


@pytest.fixture
def store():
    with LearningStore(":memory:", now_func=lambda: NOW) as s:
        yield s


def test_record_detection_is_idempotent_and_preserves_anchor(store):
    cid1 = store.record_detection(TOKEN, detection_price_usd=0.01, creator="devWallet")
    # Re-detecting must not overwrite the original detection price anchor.
    cid2 = store.record_detection(TOKEN, detection_price_usd=0.05)
    assert cid1 == cid2
    unresolved = store.unresolved_coins()
    assert len(unresolved) == 1
    _, token, _, price = unresolved[0]
    assert token.address == "Mint111"
    assert price == 0.01


def test_snapshot_series_roundtrip(store):
    cid = store.record_detection(TOKEN, detection_price_usd=0.01)
    store.append_snapshot(cid, CoinSnapshot(age_seconds=0, price_usd=0.01, liquidity_usd=1000))
    store.append_snapshot(cid, CoinSnapshot(age_seconds=60, price_usd=0.02, liquidity_usd=1100))
    series = store.snapshots_for(cid)
    assert len(series) == 2
    assert series[0].age_seconds == 0
    assert series[1].price_usd == 0.02


def test_labels_and_final_bucket_rug_overrides(store):
    cid = store.record_detection(TOKEN, detection_price_usd=0.01)
    store.record_label(cid, OutcomeLabel(horizon_hours=1.0, bucket=OutcomeBucket.PUMP,
                                         forward_return_percent=80.0))
    store.record_label(cid, OutcomeLabel(horizon_hours=24.0, bucket=OutcomeBucket.RUG,
                                         forward_return_percent=-95.0))
    records = store.resolved_records()
    assert len(records) == 1
    assert records[0].final_bucket is OutcomeBucket.RUG
    assert store.resolved_count() == 1


def test_final_bucket_uses_longest_horizon_when_no_rug(store):
    cid = store.record_detection(TOKEN, detection_price_usd=0.01)
    store.record_label(cid, OutcomeLabel(horizon_hours=1.0, bucket=OutcomeBucket.PUMP,
                                         forward_return_percent=80.0))
    store.record_label(cid, OutcomeLabel(horizon_hours=24.0, bucket=OutcomeBucket.DUMP,
                                         forward_return_percent=-60.0))
    records = store.resolved_records()
    assert records[0].final_bucket is OutcomeBucket.DUMP


def test_unresolved_excluded_from_resolved_records(store):
    store.record_detection(TOKEN, detection_price_usd=0.01)  # never labeled
    cid2 = store.record_detection(TOKEN2, detection_price_usd=0.02)
    store.record_label(cid2, OutcomeLabel(horizon_hours=1.0, bucket=OutcomeBucket.FLAT,
                                          forward_return_percent=5.0))
    assert store.resolved_count() == 1
    assert len(store.unresolved_coins()) == 1


def test_rug_signals_roundtrip(store):
    cid = store.record_detection(TOKEN)
    store.record_rug_signals(cid, [
        RugSignal(name="mint_authority_active", points=20.0, detail="mint open"),
        RugSignal(name="unsellable", points=30.0),
    ])
    signals = store.rug_signals_for(cid)
    assert {s.name for s in signals} == {"mint_authority_active", "unsellable"}


def test_deployer_blacklist_grows(store):
    assert store.deployer_rug_count("devWallet", "solana") == 0
    assert store.blacklist_deployer("devWallet", "solana") == 1
    assert store.blacklist_deployer("devWallet", "solana") == 2
    assert store.deployer_rug_count("devWallet", "solana") == 2
    # Unknown / None creator is clean, not an error.
    assert store.deployer_rug_count(None, "solana") == 0


def test_metrics_history(store):
    store.record_metrics("last_200", {"rug_f1": 0.7, "hit_rate": 0.55})
    store.record_metrics("all", {"resolved": 200})
    all_rows = store.metrics_history()
    assert len(all_rows) == 2
    windowed = store.metrics_history(window="last_200")
    assert len(windowed) == 1
    assert windowed[0]["metrics"]["rug_f1"] == 0.7


def test_resolved_records_carry_full_lifecycle(store):
    cid = store.record_detection(TOKEN, detection_price_usd=0.01, creator="dev")
    store.append_snapshot(cid, CoinSnapshot(age_seconds=0, price_usd=0.01))
    store.record_rug_signals(cid, [RugSignal(name="unsellable", points=30.0)])
    store.record_label(cid, OutcomeLabel(horizon_hours=6.0, bucket=OutcomeBucket.RUG,
                                         forward_return_percent=-90.0))
    record = store.resolved_records()[0]
    assert record.creator == "dev"
    assert len(record.snapshots) == 1
    assert len(record.rug_signals) == 1
    assert record.label_for(6.0).bucket is OutcomeBucket.RUG


async def test_store_is_safe_from_a_worker_thread():
    """Regression (2026-07-15 droplet journal): the scanner runs
    retrain_if_due via asyncio.to_thread, so this store — created on the
    event-loop thread — is read AND written from a worker thread. The old
    check_same_thread connection made every scheduled retrain die with
    'SQLite objects created in a thread can only be used in that same
    thread', so the classifier never trained once in production. The store
    is now lock-serialized and thread-safe."""
    import asyncio
    store = LearningStore(":memory:", now_func=lambda: NOW)
    # Cross-thread read (the first statement retrain_if_due executes).
    assert await asyncio.to_thread(store.resolved_count) == 0
    # Cross-thread write, idempotent against a main-thread write.
    cid = store.record_detection(TOKEN)
    assert await asyncio.to_thread(store.record_detection, TOKEN) == cid
    # Composite method whose helpers nest inside the lock (RLock required).
    record = await asyncio.to_thread(store.get_record, cid)
    assert record is not None and record.token.address == TOKEN.address


async def test_iter_resolved_records_releases_the_lock_between_coins():
    """Regression (2026-07-15, second incident): resolved_records used to
    hold the store lock across the ENTIRE per-coin walk — during the first
    real 24k-coin retrain the event-loop thread blocked on its next store
    call and Telegram went silent. The iterator must take the lock per coin
    so other threads interleave mid-walk."""
    import asyncio
    store = LearningStore(":memory:", now_func=lambda: NOW)
    for i in range(3):
        tok = TokenIdentity(chain="solana", address=f"IterTok{i}", symbol=f"T{i}")
        cid = store.record_detection(tok)
        store.append_snapshot(cid, CoinSnapshot(age_seconds=0, price_usd=1.0))
        store.record_label(cid, OutcomeLabel(horizon_hours=1.0, bucket=OutcomeBucket.PUMP,
                                             forward_return_percent=80.0))
    it = store.iter_resolved_records()
    first = next(it)                       # generator now paused mid-walk
    assert first is not None
    # If the walk held the lock, this cross-thread call would deadlock the
    # 2s timeout; releasing per coin lets it complete immediately.
    count = await asyncio.wait_for(asyncio.to_thread(store.resolved_count), timeout=2.0)
    assert count == 3
    rest = list(it)
    assert len(rest) == 2                  # walk resumes and completes
    assert len(store.resolved_records()) == 3   # list form still equivalent


def test_graded_predictions_single_query_join():
    """graded_predictions must return (payload, final_bucket, created_at) for
    resolved coins WITH stored predictions only — unresolved coins and coins
    without predictions are excluded (2026-07-16 single-query metrics path)."""
    store = LearningStore(":memory:", now_func=lambda: NOW)
    resolved = store.record_detection(
        TokenIdentity(chain="solana", address="Graded1"), detected_at=NOW)
    store.record_prediction(resolved, {"predicted_label": "rug",
                                       "distribution": {"rug": 0.9}})
    store.record_label(resolved, OutcomeLabel(horizon_hours=24.0,
                                              bucket=OutcomeBucket.RUG,
                                              forward_return_percent=-95.0,
                                              resolved_at=NOW))
    unresolved = store.record_detection(
        TokenIdentity(chain="solana", address="Pending1"), detected_at=NOW)
    store.record_prediction(unresolved, {"predicted_label": "pump"})
    no_prediction = store.record_detection(
        TokenIdentity(chain="solana", address="Silent1"), detected_at=NOW)
    store.record_label(no_prediction, OutcomeLabel(horizon_hours=24.0,
                                                   bucket=OutcomeBucket.PUMP,
                                                   forward_return_percent=90.0,
                                                   resolved_at=NOW))

    rows = store.graded_predictions()
    assert len(rows) == 1
    payload, bucket_value, created_at = rows[0]
    assert payload["predicted_label"] == "rug"      # payload JSON round-trips
    assert bucket_value == "rug"
    assert created_at == NOW                         # prediction's own clock
