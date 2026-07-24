"""HDBSCAN archetype discovery + novelty detection (Section 3).

Over the growing fingerprint set the layer periodically discovers recurring
*archetypes* — "clean organic grower", "slow rug", "pump-and-dump",
"honeypot", "wash-traded shell" — as density clusters, and labels each by the
dominant outcome of its members. For a live coin it then reports:

* its **nearest archetype** (what kind of coin it most looks like), and
* a **novelty score**: how far it sits from every known archetype.

Novelty is the "catch what's coming next" signal. A high novelty score means
the coin matches nothing seen before — an early sign of an emerging pattern
the system hasn't learned yet, which the dashboard should surface loudly.

Novelty is expressed as an empirical percentile in ``[0, 1]``: the fraction of
past coins that sat *closer* to a known archetype than this one does. So
``novelty_score = 0.95`` means the coin is more unlike known archetypes than
95% of history — directly comparable against the configurable
``novelty_percentile`` flag threshold, regardless of the raw feature scale.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.learning.models import OutcomeBucket

_logger = get_logger("learning.archetypes")


@dataclass(frozen=True)
class Archetype:
    """One discovered archetype cluster (Section 3)."""

    cluster_id: int
    name: str                       # e.g. "rug_archetype_2"
    dominant_bucket: OutcomeBucket
    size: int
    centroid: np.ndarray
    distribution: dict[str, float]  # member outcome mix


@dataclass(frozen=True)
class ArchetypeAssignment:
    """A live coin's nearest archetype and its novelty (Section 3)."""

    name: str | None
    cluster_id: int | None
    dominant_bucket: OutcomeBucket | None
    novelty_score: float | None     # empirical percentile in [0, 1]
    distance: float | None          # raw distance to nearest centroid

    def is_novel_at(self, percentile: float) -> bool:
        """True when this coin is more novel than ``percentile``% of history."""
        return self.novelty_score is not None and self.novelty_score * 100.0 >= percentile


class ArchetypeModel:
    """Clusters fingerprints into archetypes and scores novelty against them.

    Fit on the standardized (scaled) fingerprint set — the same space the
    classifier uses — with Euclidean density clustering. Only the derived
    artifacts (centroids, labels, the training novelty distribution) are kept
    after fitting, so the model is cheap to persist and query and does not
    depend on the HDBSCAN object surviving (Section 9).
    """

    def __init__(self) -> None:
        self._archetypes: list[Archetype] = []
        self._centroids: np.ndarray | None = None      # (n_clusters, dim)
        self._train_novelty: np.ndarray | None = None  # sorted training distances
        self._logger = get_logger("learning.archetypes")

    @property
    def is_fitted(self) -> bool:
        return self._centroids is not None and len(self._archetypes) > 0

    @property
    def archetypes(self) -> tuple[Archetype, ...]:
        return tuple(self._archetypes)

    def fit(
        self,
        scaled_vectors: np.ndarray,
        buckets: Sequence[OutcomeBucket],
        *,
        min_cluster_size: int,
    ) -> int:
        """Discover archetypes. Returns the number of clusters found.

        Points HDBSCAN marks as noise (label -1) do not form archetypes but
        still count toward the novelty distribution — they are, by definition,
        the coins that matched nothing, so they anchor "how novel is novel".
        Finding zero clusters is a valid outcome (too little/too diffuse data);
        the model reports not-fitted rather than fabricating archetypes.
        """
        import hdbscan

        matrix = np.ascontiguousarray(scaled_vectors, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[0] == 0:
            self._reset()
            return 0
        if len(buckets) != matrix.shape[0]:
            raise ValueError("buckets length must match number of vectors")
        if matrix.shape[0] < min_cluster_size:
            self._logger.info("archetype fit skipped: %d samples < min_cluster_size %d",
                              matrix.shape[0], min_cluster_size)
            self._reset()
            return 0
        # HDBSCAN mandates min_cluster_size >= 2. Config validation already
        # enforces this, but fit() takes the value as a parameter, so guard
        # defensively and degrade gracefully (as the docstring promises)
        # rather than letting HDBSCAN raise.
        if min_cluster_size < 2:
            self._logger.info("archetype fit skipped: min_cluster_size %d < 2", min_cluster_size)
            self._reset()
            return 0

        clusterer = hdbscan.HDBSCAN(min_cluster_size=int(min_cluster_size),
                                    metric="euclidean")
        labels = clusterer.fit_predict(matrix)

        archetypes: list[Archetype] = []
        for cluster_id in sorted(set(int(lbl) for lbl in labels)):
            if cluster_id < 0:                     # noise
                continue
            member_mask = labels == cluster_id
            members = matrix[member_mask]
            member_buckets = [buckets[i] for i in np.where(member_mask)[0]]
            centroid = members.mean(axis=0)
            counts = Counter(b.value for b in member_buckets)
            total = sum(counts.values())
            dominant_value = counts.most_common(1)[0][0]
            distribution = {v: c / total for v, c in counts.items()}
            archetypes.append(Archetype(
                cluster_id=cluster_id,
                name=f"{dominant_value}_archetype_{cluster_id}",
                dominant_bucket=OutcomeBucket(dominant_value),
                size=int(member_mask.sum()),
                centroid=centroid.astype(np.float32),
                distribution=distribution,
            ))

        if not archetypes:
            self._logger.info("archetype fit found only noise; no archetypes")
            self._reset()
            return 0

        self._archetypes = archetypes
        self._centroids = np.vstack([a.centroid for a in archetypes]).astype(np.float64)
        # Training novelty distribution: every point's distance to its nearest
        # centroid, sorted, so assign() can turn a raw distance into a percentile.
        dists = self._nearest_distances(matrix)
        self._train_novelty = np.sort(dists)
        self._logger.info("discovered %d archetypes from %d coins",
                          len(archetypes), matrix.shape[0])
        return len(archetypes)

    def assign(self, scaled_vector: np.ndarray) -> ArchetypeAssignment:
        """Nearest archetype + novelty percentile for a live coin (Section 3)."""
        if not self.is_fitted:
            return ArchetypeAssignment(name=None, cluster_id=None, dominant_bucket=None,
                                       novelty_score=None, distance=None)
        vec = np.asarray(scaled_vector, dtype=np.float64).reshape(1, -1)
        distances = np.linalg.norm(self._centroids - vec, axis=1)
        nearest = int(np.argmin(distances))
        distance = float(distances[nearest])
        archetype = self._archetypes[nearest]
        novelty = self._novelty_percentile(distance)
        return ArchetypeAssignment(
            name=archetype.name,
            cluster_id=archetype.cluster_id,
            dominant_bucket=archetype.dominant_bucket,
            novelty_score=novelty,
            distance=distance,
        )

    def _nearest_distances(self, matrix: np.ndarray) -> np.ndarray:
        """(N,) distance from every point to its nearest centroid.

        Computed one centroid at a time instead of broadcasting the full
        (N, n_clusters, dim) difference tensor up front -- that intermediate
        reached 6.97 GiB on a 39,570-coin training set (OOM: "Unable to
        allocate 6.97 GiB for an array with shape (39570, 221, 107)",
        bug-hunt finding 2026-07-24) even though the actual output here is
        just N floats. Mathematically identical result, same Euclidean
        distances, only the memory footprint changes.
        """
        nearest = None
        for centroid in self._centroids:
            distance = np.linalg.norm(matrix - centroid, axis=1)
            nearest = distance if nearest is None else np.minimum(nearest, distance)
        return nearest

    def _novelty_percentile(self, distance: float) -> float:
        assert self._train_novelty is not None
        n = self._train_novelty.size
        if n == 0:
            return 1.0
        rank = int(np.searchsorted(self._train_novelty, distance, side="right"))
        return min(1.0, max(0.0, rank / n))

    def _reset(self) -> None:
        self._archetypes = []
        self._centroids = None
        self._train_novelty = None

    # ---- Persistence (Section 9) ----

    def save(self, path: str) -> None:
        import joblib

        joblib.dump({
            "archetypes": self._archetypes,
            "centroids": self._centroids,
            "train_novelty": self._train_novelty,
        }, path)

    @classmethod
    def load(cls, path: str) -> "ArchetypeModel":
        import joblib

        payload = joblib.load(path)
        model = cls()
        model._archetypes = list(payload["archetypes"])
        model._centroids = payload["centroids"]
        model._train_novelty = payload["train_novelty"]
        return model
