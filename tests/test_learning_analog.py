"""Tests for the FAISS analog memory + recency-weighted voting (Section 3)."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from meme_intelligence.learning.analog import AnalogEntry, AnalogMemory
from meme_intelligence.learning.features import (
    FEATURE_DIM,
    FingerprintExtractor,
    StandardScalerBundle,
)
from meme_intelligence.learning.models import (
    CoinRecord,
    CoinSnapshot,
    OutcomeBucket,
    OutcomeLabel,
)
from meme_intelligence.core.models import TokenIdentity

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _vec(seed: float) -> np.ndarray:
    rng = np.random.default_rng(int(seed))
    return rng.normal(size=FEATURE_DIM).astype(np.float32)


def _entry(addr: str, bucket: OutcomeBucket, age_days: float) -> AnalogEntry:
    return AnalogEntry(address=addr, chain="solana", bucket=bucket,
                       resolved_at=NOW - timedelta(days=age_days))


def test_empty_index_returns_no_neighbors():
    mem = AnalogMemory(now_func=lambda: NOW)
    assert mem.size == 0
    assert mem.query(_vec(1), k=5) == []


def test_identical_fingerprint_is_top_neighbor():
    mem = AnalogMemory(now_func=lambda: NOW)
    target = _vec(1)
    mem.add(_entry("A", OutcomeBucket.PUMP, 1.0), target)
    mem.add(_entry("B", OutcomeBucket.DUMP, 1.0), _vec(2))
    mem.add(_entry("C", OutcomeBucket.RUG, 1.0), _vec(3))
    neighbors = mem.query(target, k=3)
    assert neighbors[0].address == "A"
    assert neighbors[0].similarity == pytest.approx(1.0, abs=1e-4)


def test_query_returns_at_most_index_size():
    mem = AnalogMemory(now_func=lambda: NOW)
    mem.add(_entry("A", OutcomeBucket.PUMP, 1.0), _vec(1))
    neighbors = mem.query(_vec(1), k=25)
    assert len(neighbors) == 1


def test_vote_abstains_below_min_neighbors():
    mem = AnalogMemory(now_func=lambda: NOW)
    for i in range(3):
        mem.add(_entry(f"A{i}", OutcomeBucket.PUMP, 1.0), _vec(i + 1))
    vote = mem.forecast(_vec(1), k=25, half_life_days=30.0, min_neighbors=5)
    assert vote.abstained is True
    # Uniform fallback: no fabricated confidence.
    assert vote.distribution["pump"] == pytest.approx(0.25)


def test_vote_majority_outcome_wins():
    mem = AnalogMemory(now_func=lambda: NOW)
    # 6 near-identical RUG analogs vs 1 far PUMP.
    base = _vec(1)
    for i in range(6):
        mem.add(_entry(f"rug{i}", OutcomeBucket.RUG, 1.0), base + 0.001 * _vec(100 + i))
    mem.add(_entry("pump", OutcomeBucket.PUMP, 1.0), -base)
    vote = mem.forecast(base, k=10, half_life_days=30.0, min_neighbors=5)
    assert vote.abstained is False
    assert vote.distribution["rug"] > 0.8
    assert max(vote.distribution, key=vote.distribution.get) == "rug"


def test_recency_decay_downweights_stale_analogs():
    """A fresh DUMP analog should outvote an equally-similar but ancient PUMP."""
    mem = AnalogMemory(now_func=lambda: NOW)
    base = _vec(7)
    # Five neighbors to clear the min_neighbors gate; two decisive ones differ
    # only in recency.
    mem.add(_entry("fresh_dump", OutcomeBucket.DUMP, 1.0), base)
    mem.add(_entry("old_pump", OutcomeBucket.PUMP, 400.0), base)
    for i in range(3):
        mem.add(_entry(f"filler{i}", OutcomeBucket.FLAT, 200.0), _vec(50 + i))
    vote = mem.forecast(base, k=10, half_life_days=30.0, min_neighbors=5)
    assert vote.distribution["dump"] > vote.distribution["pump"]


def test_build_from_records_skips_unresolved():
    extractor = FingerprintExtractor()
    scaler = StandardScalerBundle()
    resolved = CoinRecord(
        token=TokenIdentity(chain="solana", address="R1"),
        detected_at=NOW - timedelta(days=2),
        detection_price_usd=0.01,
        snapshots=(CoinSnapshot(age_seconds=0, price_usd=0.01, liquidity_usd=1000),
                   CoinSnapshot(age_seconds=60, price_usd=0.02, liquidity_usd=1100)),
        labels=(OutcomeLabel(horizon_hours=24.0, bucket=OutcomeBucket.PUMP,
                             forward_return_percent=90.0, resolved_at=NOW - timedelta(days=1)),),
    )
    unresolved = CoinRecord(
        token=TokenIdentity(chain="solana", address="U1"),
        detected_at=NOW,
        detection_price_usd=0.01,
        snapshots=(CoinSnapshot(age_seconds=0, price_usd=0.01),),
        labels=(),
    )
    mem = AnalogMemory(now_func=lambda: NOW)
    added = mem.build_from_records([resolved, unresolved], extractor, scaler)
    assert added == 1
    assert mem.size == 1


def test_non_training_label_neighbor_does_not_inflate_denominator():
    """A non-training-label (UNRESOLVED) analog must not under-normalize the
    distribution or fake confidence (Rule 8) — it is simply ignored."""
    mem = AnalogMemory(now_func=lambda: NOW)
    base = _vec(3)
    mem.add(_entry("pump", OutcomeBucket.PUMP, 1.0), base)
    for i in range(5):
        mem.add(AnalogEntry(address=f"u{i}", chain="solana",
                            bucket=OutcomeBucket.UNRESOLVED, resolved_at=NOW), _vec(20 + i))
    vote = mem.forecast(base, k=10, half_life_days=30.0, min_neighbors=3)
    assert vote.abstained is False
    # Distribution renormalizes over the single valid PUMP neighbor.
    assert vote.distribution["pump"] == pytest.approx(1.0)
    assert sum(vote.distribution.values()) == pytest.approx(1.0)


def test_all_non_training_label_neighbors_abstain():
    mem = AnalogMemory(now_func=lambda: NOW)
    for i in range(6):
        mem.add(AnalogEntry(address=f"u{i}", chain="solana",
                            bucket=OutcomeBucket.UNRESOLVED, resolved_at=NOW), _vec(i + 1))
    vote = mem.forecast(_vec(1), k=10, half_life_days=30.0, min_neighbors=5)
    assert vote.abstained is True
    assert vote.distribution["pump"] == pytest.approx(0.25)


def test_save_and_load_roundtrip(tmp_path):
    mem = AnalogMemory(now_func=lambda: NOW)
    target = _vec(1)
    mem.add(_entry("A", OutcomeBucket.PUMP, 3.0), target)
    mem.add(_entry("B", OutcomeBucket.RUG, 5.0), _vec(2))
    idx_path = str(tmp_path / "index.faiss")
    meta_path = str(tmp_path / "meta.joblib")
    mem.save(idx_path, meta_path)
    reloaded = AnalogMemory.load(idx_path, meta_path, now_func=lambda: NOW)
    assert reloaded.size == 2
    neighbors = reloaded.query(target, k=2)
    assert neighbors[0].address == "A"
    assert neighbors[0].resolved_as is OutcomeBucket.PUMP
