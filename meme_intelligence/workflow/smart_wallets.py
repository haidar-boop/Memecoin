"""Smart-wallet data collection (Part 17 groundwork) — the data clock.

Records which wallets hold each analyzed token early in its life, using
the top-holder addresses ALREADY present in every GoPlus security
response (previously discarded at parse time). Zero new API calls, zero
cost — this only keeps data the bot already fetched.

Why holders and not live buys: PumpPortal's per-trade streams
(``subscribeTokenTrade``/``subscribeAccountTrade``) are metered — 0.01
SOL per 10k events against a funded linked wallet — which fails the
operator's free-only constraint (verified against PumpPortal's docs
2026-07-14; decision recorded in DECISIONS_LOG.md). Early-top-holder
co-occurrence is the weaker but genuinely free signal: once outcome
tracking labels these tokens as winners or losers, "which wallets keep
showing up early on winners" becomes computable from this table.

This module is a passive recorder ONLY. It never alerts, never scores,
never touches analysis or the trading path; a storage failure degrades
to a logged warning and the scan cycle continues untouched (Rule 7).
Reputation scoring and the live smart-money alert are LATER steps,
deliberately unbuilt until enough labeled data accumulates (Rule 2).
"""

from __future__ import annotations

from meme_intelligence.analytics.wallet_reputation import DEFAULT_SIGHTING_SOURCE
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import SecurityProfile
from meme_intelligence.workflow.controller import _BoundedKeySet

# Provenance tag stored on every sighting this recorder writes, so later
# feeds (e.g. a real trade stream) stay distinguishable (Rule 9). Imported
# from the reputation module so writer and reader can never drift apart.
_SOURCE = DEFAULT_SIGHTING_SOURCE

# Sighting side for "appeared in the circulating top-N holder list".
# Deliberately distinct from "buy"/"sell": a holder snapshot carries no
# timing or size-of-entry information, and reputation scoring must be
# able to weigh the two kinds of evidence differently (Rule 8).
_SIDE = "hold_top10"


class SmartWalletRecorder:
    """Records top-holder wallet sightings once per token (Part 17).

    Sits behind ``settings.smart_wallet.enabled`` (authoritative, like
    every other opt-in layer) and dedups per token with a bounded key
    set: the FIRST security profile the scanner obtains for a token is
    the earliest holder snapshot the bot will ever have — exactly the
    one reputation needs — and rechecks add rows without adding signal,
    so they are skipped. The dedup is in-memory: after a restart a
    tracked token can record once more; reputation queries aggregate
    with DISTINCT, so duplicate rows cost bytes, not correctness.
    """

    def __init__(self, storage, settings) -> None:
        self._storage = storage
        self._s = settings
        self._recorded = _BoundedKeySet(settings.max_seen_keys)
        self._logger = get_logger("workflow.smart_wallets")

    def record(self, profile: SecurityProfile | None) -> int:
        """Record ``profile``'s top holders; returns rows written (0 = skipped).

        Never raises: this runs inside the scan cycle, and no recording
        problem is ever worth disturbing analysis or alerting (Rule 7).
        """
        if not self._s.enabled or profile is None:
            return 0
        holders = profile.top_holders[: self._s.max_holders_per_token]
        if not holders:
            # No holder data is a gap, not a fact (Rule 8) — leave the token
            # unmarked so a later recheck WITH data still records it.
            return 0
        token = profile.token
        key = (token.chain, token.address.lower())
        if key in self._recorded:
            return 0
        try:
            written = self._storage.record_wallet_sightings(
                token,
                [(h.address, _SIDE, None, h.percent) for h in holders],
                source=_SOURCE,
            )
        except Exception as exc:  # noqa: BLE001 — recording must never break the scan
            self._logger.warning("wallet sighting recording failed for %s/%s: %s: %s",
                                 token.chain, token.address, type(exc).__name__, exc)
            return 0
        self._recorded.add(key)
        self._logger.info("recorded %d top-holder sightings for %s/%s",
                          written, token.chain, token.address)
        return written
