"""Tests for HDBSCAN archetype discovery + novelty detection (Section 3)."""

import numpy as np
import pytest

from meme_intelligence.learning.archetypes import ArchetypeModel
from meme_intelligence.learning.models import OutcomeBucket


def _two_blobs(n_per=40, dim=8, sep=10.0, seed=0):
    """Two well-separated clusters: one RUG-dominant, one PUMP-dominant."""
    rng = np.random.default_rng(seed)
    a = rng.normal(loc=0.0, scale=0.5, size=(n_per, dim))
    b = rng.normal(loc=sep, scale=0.5, size=(n_per, dim))
    vectors = np.vstack([a, b])
    buckets = ([OutcomeBucket.RUG] * n_per) + ([OutcomeBucket.PUMP] * n_per)
    return vectors, buckets


def test_discovers_clusters_and_labels_by_dominant_outcome():
    vectors, buckets = _two_blobs()
    model = ArchetypeModel()
    n = model.fit(vectors, buckets, min_cluster_size=10)
    assert n >= 2
    dominant = {a.dominant_bucket for a in model.archetypes}
    assert OutcomeBucket.RUG in dominant
    assert OutcomeBucket.PUMP in dominant


def test_assign_matches_nearest_cluster():
    vectors, buckets = _two_blobs(dim=8, sep=10.0)
    model = ArchetypeModel()
    model.fit(vectors, buckets, min_cluster_size=10)
    # A point near the RUG blob (loc 0) should match a rug archetype.
    point = np.zeros(8)
    assignment = model.assign(point)
    assert assignment.dominant_bucket is OutcomeBucket.RUG
    assert assignment.novelty_score is not None


def test_novel_point_scores_high_novelty():
    vectors, buckets = _two_blobs(dim=8, sep=10.0)
    model = ArchetypeModel()
    model.fit(vectors, buckets, min_cluster_size=10)
    # Far from both blobs -> high novelty percentile.
    far = np.full(8, 100.0)
    near = np.zeros(8)
    assert model.assign(far).novelty_score > model.assign(near).novelty_score
    assert model.assign(far).novelty_score >= 0.99


def test_unfit_model_returns_empty_assignment():
    model = ArchetypeModel()
    assignment = model.assign(np.zeros(8))
    assert assignment.name is None
    assert assignment.novelty_score is None
    assert model.is_fitted is False


def test_too_few_samples_finds_no_archetypes():
    rng = np.random.default_rng(1)
    vectors = rng.normal(size=(5, 8))
    buckets = [OutcomeBucket.FLAT] * 5
    model = ArchetypeModel()
    assert model.fit(vectors, buckets, min_cluster_size=15) == 0
    assert model.is_fitted is False


def test_min_cluster_size_below_two_degrades_gracefully():
    """min_cluster_size < 2 (invalid for HDBSCAN) must not crash fit()."""
    rng = np.random.default_rng(2)
    vectors = rng.normal(size=(10, 4))
    buckets = [OutcomeBucket.FLAT] * 10
    model = ArchetypeModel()
    assert model.fit(vectors, buckets, min_cluster_size=1) == 0
    assert model.is_fitted is False


def test_save_load_roundtrip(tmp_path):
    vectors, buckets = _two_blobs()
    model = ArchetypeModel()
    model.fit(vectors, buckets, min_cluster_size=10)
    path = str(tmp_path / "arch.joblib")
    model.save(path)
    reloaded = ArchetypeModel.load(path)
    assert reloaded.is_fitted is True
    a = model.assign(np.zeros(8))
    b = reloaded.assign(np.zeros(8))
    assert a.name == b.name
    assert a.novelty_score == pytest.approx(b.novelty_score)


def test_is_novel_at_threshold():
    vectors, buckets = _two_blobs(dim=8, sep=10.0)
    model = ArchetypeModel()
    model.fit(vectors, buckets, min_cluster_size=10)
    far = model.assign(np.full(8, 100.0))
    assert far.is_novel_at(90.0) is True


def test_nearest_distances_matches_naive_broadcast_reference():
    """Bug-hunt regression (2026-07-24): _nearest_distances used to build the
    full (N, n_clusters, dim) difference tensor via broadcasting before
    reducing it -- on the live droplet's 39,570-coin training set this hit
    "Unable to allocate 6.97 GiB for an array with shape (39570, 221, 107)".
    The per-centroid loop must produce numerically identical distances to
    that original broadcast-then-reduce formula at a realistic scale."""
    rng = np.random.default_rng(3)
    n, k, dim = 3000, 60, 24
    matrix = rng.normal(size=(n, dim))
    centroids = rng.normal(size=(k, dim))

    def naive_broadcast_reference(matrix, centroids):
        diffs = matrix[:, None, :] - centroids[None, :, :]
        return np.linalg.norm(diffs, axis=2).min(axis=1)

    model = ArchetypeModel()
    model._centroids = centroids
    fixed = model._nearest_distances(matrix)
    expected = naive_broadcast_reference(matrix, centroids)
    assert np.allclose(fixed, expected)
