"""Tests for the mind-layer SQLite persistence (Section 9)."""

import threading
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


def test_resolved_records_bucket_filter(store):
    """The /winners report pulls 'last N pumps' via a SQL-side bucket filter
    (2026-07-28) — a rare bucket must not require walking the whole table."""
    pump_id = store.record_detection(TOKEN, detection_price_usd=0.01)
    store.record_label(pump_id, OutcomeLabel(horizon_hours=6.0, bucket=OutcomeBucket.PUMP,
                                             forward_return_percent=300.0))
    rug_id = store.record_detection(TOKEN2, detection_price_usd=0.02)
    store.record_label(rug_id, OutcomeLabel(horizon_hours=6.0, bucket=OutcomeBucket.RUG,
                                            forward_return_percent=-90.0))

    pumps = store.resolved_records(bucket=OutcomeBucket.PUMP)
    assert [r.token.address for r in pumps] == [TOKEN.address]
    rugs = store.resolved_records(bucket=OutcomeBucket.RUG, limit=10)
    assert [r.token.address for r in rugs] == [TOKEN2.address]
    # Unfiltered call unchanged: both resolved coins.
    assert len(store.resolved_records()) == 2


def test_store_is_usable_from_a_different_thread(store):
    """Bug-hunt regression (2026-07-21): retrain_if_due runs on a worker
    thread via asyncio.to_thread (Rule 10 -- the CPU-bound retrain must not
    block the event loop), but the connection is opened on the main thread.
    Without check_same_thread=False this raised "SQLite objects created in
    a thread can only be used in that same thread" on every single call --
    confirmed firing every monitor cycle in production logs, silently
    disabling all mind-layer retraining. A background thread must be able
    to read and write through the same store with no exception."""
    store.record_detection(TOKEN, detection_price_usd=0.01, creator="dev")

    errors: list[Exception] = []

    def worker() -> None:
        try:
            store.record_detection(TOKEN2, detection_price_usd=0.02, creator="dev2")
            store.resolved_count()
            store.resolved_records()
        except Exception as exc:  # noqa: BLE001 -- capture for the assertion below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "worker thread hung"
    assert errors == [], f"cross-thread access raised: {errors}"
    assert store.coin_id(TOKEN2) is not None
