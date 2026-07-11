"""FAISS analog memory + recency-weighted k-NN forecasting (Section 3).

This is the heart of the mind layer: recognizing that a coin *now* looks like
coins that came before it, and forecasting its outcome from how those analogs
resolved. The central question — "which past coins does this one most resemble
right now, and how did those end up?" — is answered here.

Two properties matter:

* **Append-only, real-time.** The moment a past coin resolves, its fingerprint
  is inserted; the very next similar coin benefits with zero retraining. This
  is the *instant-learning* mechanism (Section 3 / Section 7).
* **Recency-weighted voting.** The meme meta shifts, so a recent analog counts
  more than a stale one. Each neighbor's vote is weighted by
  ``similarity × exp(-age_days / half_life)`` (Section 3).

Similarity is cosine similarity: fingerprints are L2-normalized and stored in a
FAISS inner-product index, so the nearest neighbors are the most similar coins
and the inner product *is* the cosine. Cosine is clamped to ``[0, 1]`` for
weighting — a coin pointing the opposite way in fingerprint space is "not an
analog" (weight ~0), never a negative vote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

import numpy as np

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.learning.features import FEATURE_DIM, FingerprintExtractor
from meme_intelligence.learning.models import (
    AnalogNeighbor,
    CoinRecord,
    OutcomeBucket,
    uniform_distribution,
)

_logger = get_logger("learning.analog")


@dataclass(frozen=True)
class AnalogEntry:
    """Metadata for one fingerprint stored in the analog index."""

    address: str
    chain: str
    bucket: OutcomeBucket        # how this past coin resolved
    resolved_at: datetime        # when it resolved (drives recency decay)


@dataclass(frozen=True)
class AnalogVote:
    """Result of recency-weighted neighbor voting (Section 3).

    ``distribution`` always carries all four training labels (0.0 for absent),
    so the ensemble can blend it uniformly. ``abstained`` is True when there
    were too few neighbors or zero total weight — the analog engine says "I
    don't have enough memory yet" rather than inventing a confident call
    (Rule 8, Section 11 cold start).
    """

    distribution: dict[str, float]
    neighbors: tuple[AnalogNeighbor, ...]
    effective_weight: float
    abstained: bool


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize each row; a zero row stays zero (cosine undefined -> 0)."""
    mat = np.ascontiguousarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return mat / norms


class AnalogMemory:
    """An append-only FAISS index of resolved-coin fingerprints (Section 3).

    Stores L2-normalized scaled fingerprints in a flat inner-product index
    (exact search — correctness over speed at this scale; a coin count in the
    tens of thousands searches in well under a millisecond). Each vector is
    paired with an :class:`AnalogEntry` carrying its outcome and resolution
    time.
    """

    def __init__(
        self,
        *,
        dim: int = FEATURE_DIM,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        import faiss

        self._faiss = faiss
        self._dim = dim
        self._index = faiss.IndexFlatIP(dim)
        self._entries: list[AnalogEntry] = []
        self._now = now_func

    @property
    def size(self) -> int:
        return len(self._entries)

    # ---- Building & inserting ----

    def add(self, entry: AnalogEntry, scaled_vector: np.ndarray) -> None:
        """Insert one resolved coin's fingerprint (the instant-learning path)."""
        vec = _normalize_rows(np.asarray(scaled_vector, dtype=np.float32).reshape(1, -1))
        self._index.add(vec)
        self._entries.append(entry)

    def add_many(self, entries: Sequence[AnalogEntry], scaled_vectors: np.ndarray) -> None:
        matrix = np.asarray(scaled_vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(entries):
            raise ValueError("entries and scaled_vectors length mismatch")
        if not entries:
            return
        self._index.add(_normalize_rows(matrix))
        self._entries.extend(entries)

    def build_from_records(
        self,
        records: Sequence[CoinRecord],
        extractor: FingerprintExtractor,
        scaler,
    ) -> int:
        """Bulk-load resolved records: extract -> scale -> insert. Returns count.

        Records whose outcome is still unresolved are skipped (they carry no
        label to vote with). Resolution time is the latest horizon resolution,
        falling back to detection time when a label lacks a timestamp.
        """
        entries: list[AnalogEntry] = []
        vectors: list[np.ndarray] = []
        for record in records:
            bucket = record.final_bucket
            if not bucket.is_resolved:
                continue
            fp = extractor.extract(record.snapshots)
            scaled = scaler.transform(fp.vector)
            resolved_at = _resolution_time(record)
            entries.append(AnalogEntry(
                address=record.token.address,
                chain=record.token.chain,
                bucket=bucket,
                resolved_at=resolved_at,
            ))
            vectors.append(scaled)
        if not entries:
            return 0
        self.add_many(entries, np.vstack(vectors))
        _logger.info("analog memory built from %d resolved records", len(entries))
        return len(entries)

    # ---- Querying & voting ----

    def query(self, scaled_vector: np.ndarray, k: int) -> list[AnalogNeighbor]:
        """The k most similar past coins, most-similar first (Section 3).

        Returns fewer than k when the index holds fewer coins. Similarity is
        the cosine clamped to ``[0, 1]``; ``age_days`` is the analog's age at
        query time, used for recency weighting.
        """
        if self.size == 0 or k <= 0:
            return []
        query_vec = _normalize_rows(np.asarray(scaled_vector, dtype=np.float32).reshape(1, -1))
        k_eff = min(k, self.size)
        sims, idxs = self._index.search(query_vec, k_eff)
        now = self._now()
        neighbors: list[AnalogNeighbor] = []
        # strict=True: FAISS returns sims and idxs of identical shape (k_eff).
        for sim, idx in zip(sims[0], idxs[0], strict=True):
            if idx < 0:                      # FAISS pads with -1 when short
                continue
            entry = self._entries[int(idx)]
            age_days = max(0.0, (now - entry.resolved_at).total_seconds() / 86400.0)
            neighbors.append(AnalogNeighbor(
                address=entry.address,
                chain=entry.chain,
                similarity=float(min(1.0, max(0.0, sim))),
                resolved_as=entry.bucket,
                age_days=age_days,
            ))
        return neighbors

    def vote(
        self,
        neighbors: Sequence[AnalogNeighbor],
        *,
        half_life_days: float,
        min_neighbors: int,
    ) -> AnalogVote:
        """Recency-weighted outcome distribution over the neighbors (Section 3).

        ``weight = similarity × exp(-age_days / half_life)``. Abstains (uniform
        distribution) when there are fewer than ``min_neighbors`` analogs or the
        total weight rounds to zero — honest uncertainty, not a fabricated call.
        """
        distribution = uniform_distribution()
        neighbors = tuple(neighbors)
        if len(neighbors) < min_neighbors:
            return AnalogVote(distribution=distribution, neighbors=neighbors,
                              effective_weight=0.0, abstained=True)

        weights: dict[str, float] = {label.value: 0.0 for label in OutcomeBucket.training_labels()}
        total = 0.0
        for n in neighbors:
            # Only neighbors that resolved to a *training* label vote. A
            # non-training-label bucket (e.g. UNRESOLVED, if one ever reaches
            # the index via the low-level add() path) must contribute to
            # neither the numerator nor the denominator — otherwise it would
            # silently under-normalize the distribution while leaving
            # abstained=False, a fabricated-confidence result (Rule 8). If no
            # neighbor carries a valid outcome, total stays 0 and we abstain.
            if n.resolved_as.value not in weights:
                continue
            decay = float(np.exp(-n.age_days / half_life_days))
            weight = n.similarity * decay
            weights[n.resolved_as.value] += weight
            total += weight

        if total <= 0.0:
            return AnalogVote(distribution=distribution, neighbors=neighbors,
                              effective_weight=0.0, abstained=True)

        distribution = {label: weight / total for label, weight in weights.items()}
        return AnalogVote(distribution=distribution, neighbors=neighbors,
                          effective_weight=total, abstained=False)

    def forecast(
        self,
        scaled_vector: np.ndarray,
        *,
        k: int,
        half_life_days: float,
        min_neighbors: int,
    ) -> AnalogVote:
        """Convenience: query k neighbors then vote (Section 3 end to end)."""
        neighbors = self.query(scaled_vector, k)
        return self.vote(neighbors, half_life_days=half_life_days, min_neighbors=min_neighbors)

    # ---- Persistence (Section 9) ----

    def save(self, index_path: str, meta_path: str) -> None:
        import joblib

        self._faiss.write_index(self._index, index_path)
        joblib.dump({"entries": self._entries, "dim": self._dim}, meta_path)

    @classmethod
    def load(
        cls,
        index_path: str,
        meta_path: str,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> "AnalogMemory":
        import faiss
        import joblib

        payload = joblib.load(meta_path)
        memory = cls(dim=payload.get("dim", FEATURE_DIM), now_func=now_func)
        memory._index = faiss.read_index(index_path)
        memory._entries = list(payload["entries"])
        return memory


def _resolution_time(record: CoinRecord) -> datetime:
    """Latest label resolution time, falling back to detection time."""
    times = [lb.resolved_at for lb in record.labels if lb.resolved_at is not None]
    return max(times) if times else record.detected_at
