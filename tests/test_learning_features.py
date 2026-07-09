"""Tests for fingerprint feature engineering (Section 2)."""

import numpy as np

from meme_intelligence.learning.features import (
    FEATURE_DIM,
    FEATURE_NAMES,
    FingerprintExtractor,
    StandardScalerBundle,
)
from meme_intelligence.learning.models import CoinSnapshot


def _series(prices, liqs, *, holders=None, step_seconds=60):
    holders = holders or [None] * len(prices)
    return [
        CoinSnapshot(
            age_seconds=i * step_seconds,
            price_usd=p,
            liquidity_usd=l,
            market_cap_usd=(l * 4 if l is not None else None),
            volume_1h_usd=(l * 2 if l is not None else None),
            holder_count=h,
            buys=10,
            sells=5,
            top10_holder_percent=40.0,
        )
        for i, (p, l, h) in enumerate(zip(prices, liqs, holders))
    ]


def test_fixed_length_regardless_of_series_length():
    extractor = FingerprintExtractor()
    short = extractor.extract(_series([1.0, 1.1], [1000.0, 1100.0]))
    long = extractor.extract(_series([1.0, 1.1, 1.2, 1.3, 1.4], [1000.0] * 5))
    assert short.vector.shape == (FEATURE_DIM,)
    assert long.vector.shape == (FEATURE_DIM,)
    assert len(FEATURE_NAMES) == FEATURE_DIM


def test_empty_series_is_zero_vector_zero_coverage():
    fp = FingerprintExtractor().extract([])
    assert fp.vector.shape == (FEATURE_DIM,)
    assert np.all(fp.vector == 0.0)
    assert fp.coverage == 0.0


def test_rising_price_has_positive_slope():
    extractor = FingerprintExtractor()
    fp = extractor.extract(_series([1.0, 2.0, 3.0, 4.0], [1000.0] * 4))
    slope_idx = FEATURE_NAMES.index("price_slope")
    assert fp.vector[slope_idx] > 0.0
    last_idx = FEATURE_NAMES.index("price_last")
    assert fp.vector[last_idx] == 4.0


def test_coverage_reflects_missing_metrics():
    extractor = FingerprintExtractor()
    full = extractor.extract(_series([1.0, 1.1], [1000.0, 1100.0], holders=[100, 120]))
    # A series with price only should have strictly lower coverage.
    price_only = extractor.extract([
        CoinSnapshot(age_seconds=0, price_usd=1.0),
        CoinSnapshot(age_seconds=60, price_usd=1.2),
    ])
    assert 0.0 < price_only.coverage < full.coverage <= 1.0


def test_accepts_dict_snapshots():
    extractor = FingerprintExtractor()
    fp = extractor.extract([
        {"age_seconds": 0, "price_usd": 1.0, "liquidity_usd": 500},
        {"age_seconds": 60, "price_usd": 1.5, "liquidity_usd": 600},
    ])
    assert fp.vector.shape == (FEATURE_DIM,)
    assert fp.coverage > 0.0


def test_no_nan_or_inf_from_zero_denominators():
    # Zero market cap / zero prices must not produce inf ratios or returns.
    series = [
        CoinSnapshot(age_seconds=0, price_usd=0.0, liquidity_usd=100.0, market_cap_usd=0.0),
        CoinSnapshot(age_seconds=60, price_usd=1.0, liquidity_usd=100.0, market_cap_usd=0.0),
        CoinSnapshot(age_seconds=120, price_usd=2.0, liquidity_usd=100.0, market_cap_usd=0.0),
    ]
    fp = FingerprintExtractor().extract(series)
    assert np.all(np.isfinite(fp.vector))


def test_scaler_identity_until_fitted_then_standardizes():
    extractor = FingerprintExtractor()
    vectors = np.array([
        extractor.extract(_series([1.0, 1.1, 1.2], [1000.0, 1100.0, 1200.0])).vector,
        extractor.extract(_series([2.0, 1.5, 1.0], [5000.0, 4000.0, 3000.0])).vector,
        extractor.extract(_series([1.0, 3.0, 9.0], [200.0, 400.0, 800.0])).vector,
    ])
    bundle = StandardScalerBundle()
    one = vectors[0]
    # Before fit: identity passthrough.
    assert np.allclose(bundle.transform(one), one.astype(np.float32))
    assert bundle.is_fitted is False
    bundle.fit(vectors)
    assert bundle.is_fitted is True
    scaled = bundle.transform_many(vectors)
    # Standardized columns with variance should be ~zero mean.
    assert np.all(np.isfinite(scaled))
    assert abs(float(scaled.mean(axis=0).mean())) < 1e-5


def test_scaler_roundtrip(tmp_path):
    extractor = FingerprintExtractor()
    vectors = np.array([
        extractor.extract(_series([1.0, 1.1, 1.2], [1000.0, 1100.0, 1200.0])).vector,
        extractor.extract(_series([2.0, 1.5, 1.0], [5000.0, 4000.0, 3000.0])).vector,
    ])
    bundle = StandardScalerBundle()
    bundle.fit(vectors)
    path = str(tmp_path / "scaler.joblib")
    bundle.save(path)
    reloaded = StandardScalerBundle.load(path)
    assert reloaded.is_fitted is True
    assert np.allclose(reloaded.transform(vectors[0]), bundle.transform(vectors[0]))
