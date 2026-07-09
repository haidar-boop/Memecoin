"""Multi-provider market data service (Spec Part 15, Sections 4 and 10).

Wraps the market providers behind a failover pool so no single API outage
blinds the scanner (Rule 9), and implements the reliability rule that
*important events must be confirmed from multiple sources* before the
system acts on them with confidence.

Cross-checking philosophy (Part 32, Section 7 / Part 32.5, Section 9):

* Two sources agree  -> confidence increases (``True``).
* Two sources disagree -> confidence must DROP, not pick a favorite
  (``False`` + explanation).
* Second source unavailable -> verification is ``None`` (unknown), never
  silently treated as confirmed.
"""

from __future__ import annotations

from typing import Sequence

from meme_intelligence.core.errors import AllProvidersFailedError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair
from meme_intelligence.core.provider_pool import ProviderPool

# Liquidity figures within this factor of each other count as agreement;
# provider snapshots are never tick-identical, so exact matches are not
# expected — but a 2x disagreement means someone is wrong.
_AGREEMENT_FACTOR = 2.0


class MarketDataService:
    """Failover-pooled pair lookup + cross-source verification."""

    def __init__(
        self,
        providers: Sequence,  # clients exposing get_token_pairs(token_address, chain=None)
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._providers = list(providers)
        # Tracks which provider actually answered for each pair address, so
        # cross_check_liquidity can exclude the TRUE source (failover means
        # it is not always providers[0]) instead of guessing by position —
        # see cross_check_liquidity for why that guess was wrong.
        self._last_provider_by_pair: dict[str, str] = {}
        self._pool = ProviderPool(
            self._providers,
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )
        self._logger = get_logger("collectors.market_service")

    async def get_token_pairs(self, token_address: str, chain: str | None = None) -> list[DexPair]:
        """Pairs for a token from the first healthy provider (automatic failover)."""
        pairs, provider_name = await self._pool.call_with_provider(
            "get_token_pairs", token_address, chain=chain)
        for pair in pairs:
            self._last_provider_by_pair[pair.pair_address.lower()] = provider_name
        return pairs

    async def get_best_pair(self, token_address: str, chain: str | None = None) -> DexPair | None:
        """The deepest-liquidity pair, or ``None`` when no provider knows the token."""
        try:
            pairs = await self.get_token_pairs(token_address, chain=chain)
        except AllProvidersFailedError as exc:
            self._logger.warning("all market providers failed for %s: %s", token_address, exc)
            return None
        if not pairs:
            return None
        return max(pairs, key=lambda p: p.liquidity_usd or 0.0)

    async def cross_check_liquidity(self, pair: DexPair) -> tuple[bool | None, str]:
        """Confirm a pair's liquidity against a second source (Part 15, Section 10).

        Returns ``(verdict, note)``: ``True`` = independent source agrees,
        ``False`` = sources disagree materially (reduce confidence),
        ``None`` = no second source could verify (unknown, not confirmed).
        """
        if pair.liquidity_usd is None:
            return None, "no liquidity figure to verify"
        if len(self._providers) < 2:
            return None, "no second source configured for verification"

        # Exclude the provider that actually supplied `pair` — failover
        # means that is not always providers[0] (a hardcoded providers[1:]
        # skip let a provider confirm its own data as "independent"
        # whenever failover had returned providers[1]'s pair; bug-hunt
        # finding). Falls back to "ask everyone" if provenance wasn't
        # tracked (e.g. a pair built outside this service).
        source_name = self._last_provider_by_pair.get(pair.pair_address.lower())
        for provider in self._providers:
            name = getattr(provider, "name", type(provider).__name__)
            if source_name is not None and name == source_name:
                continue
            try:
                pairs = await provider.get_token_pairs(pair.base_token.address, chain=pair.chain)
            except Exception as exc:  # provider-specific failure: try the next one
                self._logger.debug("verifier %s unavailable: %s", name, exc)
                continue
            other = next((p for p in pairs if p.pair_address.lower() == pair.pair_address.lower()),
                         None)
            if other is None and pairs:
                other = max(pairs, key=lambda p: p.liquidity_usd or 0.0)
            if other is None or other.liquidity_usd is None:
                continue

            low, high = sorted((pair.liquidity_usd, other.liquidity_usd))
            # Both sources reporting exactly $0 is agreement (a dead pool),
            # not a disagreement — `low > 0` alone would report a $0-vs-$0
            # pair as unable to verify instead of confirming the pool is
            # genuinely empty on both sides (bug-hunt finding).
            if low == high == 0.0 or (low > 0 and high / low <= _AGREEMENT_FACTOR):
                return True, (f"liquidity confirmed by {name} "
                              f"(${other.liquidity_usd:,.0f} vs ${pair.liquidity_usd:,.0f})")
            return False, (f"sources disagree on liquidity: "
                           f"${pair.liquidity_usd:,.0f} vs ${other.liquidity_usd:,.0f} "
                           f"({name})")

        return None, "second source could not verify (unavailable or token unknown)"

    def health(self):
        """Provider health snapshot (for the dashboard phase)."""
        return self._pool.health()
