"""Token discovery engine (Spec Part 3, Part 15 Sections 6-7, Part 27).

Layer 1 of the scanning architecture: pull newly created pools from the
market collectors, apply the initial hard filters (Part 15 — reject "no
liquidity" and stale pools immediately), deduplicate, and rank survivors
with a Discovery Score.

Discovery is NOT confirmation (Part 31, Section 6 / Part 32.5, Section 2):
a candidate produced here has passed *basic* gates only. It must still go
through the security scanner (Layer 2) and deeper intelligence analysis
(Layer 3) before it can be called an opportunity.

The Discovery Score (Part 15, Section 7) grades four components 0-25 each:
freshness, liquidity, volume, and trading activity. Holder-growth data
joins the score when the blockchain collectors land; until then the score
is computed from what is actually observable (Rule 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from meme_intelligence.config.settings import DiscoverySettings
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair

_COMPONENT_MAX = 25.0  # four components x 25 = 100

_logger = get_logger("scanners.discovery")


@dataclass(frozen=True)
class TokenCandidate:
    """A newly discovered token that passed initial filtering (Part 27, Tier ranking)."""

    pair: DexPair
    discovery_score: float
    components: dict[str, float]
    reasons: tuple[str, ...]
    discovered_at: datetime


@dataclass(frozen=True)
class RejectedPool:
    """A pool that failed initial filtering, kept for logging and learning (Part 24)."""

    pair: DexPair
    reason: str


class DiscoveryEngine:
    """Filters and ranks new pools into research candidates."""

    def __init__(
        self,
        settings: DiscoverySettings,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._s = settings
        self._now = now_func
        self._logger = get_logger("scanners.discovery")

    def evaluate(self, pools: Iterable[DexPair]) -> tuple[list[TokenCandidate], list[RejectedPool]]:
        """Filter, dedupe, score, and rank a batch of pools.

        Returns (candidates sorted by score descending, rejected pools with
        reasons). Rejections are returned rather than silently dropped so
        the caller can log them and the future learning system can measure
        false negatives (Part 24, Section 4).
        """
        pools = list(pools)
        candidates: list[TokenCandidate] = []
        rejected: list[RejectedPool] = []

        # Filter first, then dedupe survivors (matches this method's docstring
        # order). Deduping before filtering let a deeper-but-stale/invalid pool
        # win dedupe and then get rejected, dropping the base token entirely
        # even when a fresher valid pool for it existed (false negative). Every
        # filtered-out pool is still recorded in `rejected` for logging/learning.
        survivors: list[DexPair] = []
        for pair in pools:
            reason = self._initial_filter(pair)
            if reason is not None:
                rejected.append(RejectedPool(pair, reason))
                continue
            survivors.append(pair)

        for pair in self._dedupe(survivors):
            candidates.append(self._score(pair))

        candidates.sort(key=lambda c: c.discovery_score, reverse=True)
        self._logger.info(
            "discovery batch: %d pools in, %d candidates, %d rejected",
            len(pools), len(candidates), len(rejected),
        )
        return candidates, rejected

    def _dedupe(self, pools: Iterable[DexPair]) -> list[DexPair]:
        """One entry per base token, keeping its deepest-liquidity pool."""
        best: dict[tuple[str, str], DexPair] = {}
        for pair in pools:
            key = (pair.chain, pair.base_token.address.lower())
            current = best.get(key)
            if current is None or (pair.liquidity_usd or 0.0) > (current.liquidity_usd or 0.0):
                best[key] = pair
        return list(best.values())

    def _initial_filter(self, pair: DexPair) -> str | None:
        """Hard gates from Part 15 Section 6. Returns a rejection reason or None.

        Unknown liquidity is rejected too: discovery cannot verify a pool it
        cannot measure, and unverifiable pools must not reach analysis
        (Part 32.5 Section 8 — deep analysis only after minimum evidence).
        """
        if pair.liquidity_usd is None:
            return "liquidity unknown: cannot verify pool"
        if pair.liquidity_usd < self._s.min_liquidity_usd:
            return (
                f"liquidity ${pair.liquidity_usd:,.0f} below minimum "
                f"${self._s.min_liquidity_usd:,.0f}"
            )
        age_hours = self._age_hours(pair)
        if age_hours is not None and age_hours > self._s.max_age_hours:
            return f"pool age {age_hours:.1f}h exceeds discovery window {self._s.max_age_hours:.0f}h"
        return None

    def _score(self, pair: DexPair) -> TokenCandidate:
        components: dict[str, float] = {}
        reasons: list[str] = []

        # Freshness: full marks at creation, decaying linearly across the window.
        age_hours = self._age_hours(pair)
        if age_hours is None:
            components["freshness"] = 0.0
            reasons.append("pool age unknown")
        else:
            remaining = max(0.0, 1.0 - age_hours / self._s.max_age_hours)
            components["freshness"] = _COMPONENT_MAX * remaining
            reasons.append(f"pool is {age_hours:.1f}h old")

        components["liquidity"] = self._scaled(
            pair.liquidity_usd, self._s.min_liquidity_usd, self._s.target_liquidity_usd
        )
        reasons.append(f"liquidity ${pair.liquidity_usd:,.0f}")

        if pair.volume_24h is None:
            components["volume"] = 0.0
            reasons.append("volume unknown")
        else:
            components["volume"] = self._scaled(
                pair.volume_24h, self._s.min_volume_24h_usd, self._s.target_volume_24h_usd
            )
            reasons.append(f"24h volume ${pair.volume_24h:,.0f}")

        txns = None
        if pair.buys_24h is not None or pair.sells_24h is not None:
            txns = (pair.buys_24h or 0) + (pair.sells_24h or 0)
        if txns is None:
            components["activity"] = 0.0
            reasons.append("transaction counts unknown")
        else:
            components["activity"] = min(
                _COMPONENT_MAX, _COMPONENT_MAX * txns / self._s.target_txns_24h
            )
            reasons.append(f"{txns} trades in 24h ({pair.buys_24h or 0} buys / {pair.sells_24h or 0} sells)")

        return TokenCandidate(
            pair=pair,
            discovery_score=round(sum(components.values()), 1),
            components=components,
            reasons=tuple(reasons),
            discovered_at=self._now(),
        )

    def _scaled(self, value: float | None, minimum: float, target: float) -> float:
        """Map [minimum, target] linearly onto [0, 25], clamped at both ends."""
        if value is None or value < minimum:
            return 0.0
        if target <= minimum:
            return _COMPONENT_MAX
        fraction = (value - minimum) / (target - minimum)
        return _COMPONENT_MAX * min(1.0, fraction)

    def _age_hours(self, pair: DexPair) -> float | None:
        if pair.pair_created_at is None:
            return None
        delta = self._now() - pair.pair_created_at
        return max(0.0, delta.total_seconds() / 3600.0)


async def scan_new_pools(
    client,  # GeckoTerminalClient or any provider with get_new_pools(network)
    engine: DiscoveryEngine,
    networks: Sequence[str],
) -> tuple[list[TokenCandidate], list[RejectedPool]]:
    """Convenience wrapper: fetch new pools across networks and evaluate them.

    Networks are isolated: one network's collector failure logs and is skipped
    so the pools already fetched from the other networks still get evaluated
    this cycle (Rule 9). Only when *every* network failed and nothing was
    collected is the failure re-raised, so the controller backs off instead of
    treating a total outage as an empty-but-healthy cycle.
    """
    pools: list[DexPair] = []
    errors: list[Exception] = []
    for network in networks:
        try:
            pools.extend(await client.get_new_pools(network))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            _logger.warning("discovery: network %s failed this cycle, skipping it: %s",
                            network, exc)
    if errors and not pools:
        raise errors[0]
    return engine.evaluate(pools)
