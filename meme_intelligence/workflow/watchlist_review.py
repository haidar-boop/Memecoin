"""Shared watchlist review (Spec Part 16 Section 8, Part 11 Sections 6-7, Part 28).

One implementation of "re-check the tokens we track": refresh market data,
re-run the research pipeline, re-tier or archive, and report every change.
Used by the ``watchlist --refresh`` command and the daily routine (the
continuous scanner keeps its own variant because each result there also
flows through alert verification).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

from meme_intelligence.core.enums import Classification, MarketRegime, WatchlistTier
from meme_intelligence.core.errors import AllProvidersFailedError, CollectorError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.database.storage import Storage, WatchlistChange
from meme_intelligence.workflow.pipeline import PipelineResult, ResearchPipeline

# Master classification -> tracking tier (Part 11, Section 5). AVOID has no
# tier: those tokens are archived, never tracked.
TIER_FOR_CLASSIFICATION = {
    Classification.ELITE_OPPORTUNITY: WatchlistTier.TIER_1_HIGH_PRIORITY,
    Classification.STRONG_CANDIDATE: WatchlistTier.TIER_1_HIGH_PRIORITY,
    Classification.WATCHLIST: WatchlistTier.TIER_2_DEVELOPING,
    Classification.SPECULATIVE: WatchlistTier.TIER_3_RESEARCH_ONLY,
}

_logger = get_logger("workflow.watchlist_review")


def stale_watchlist_reason(entry, *, max_age_days: float, now,
                           holding: bool) -> str | None:
    """The watchlist STALENESS DOOR (handoff Part 14, approved design).

    Archive reason when ``entry`` has sat on the watchlist longer than
    ``max_age_days``, else ``None``. The watchlist's only other exits are
    death (<$500 liquidity), falling to Avoid, or pairs vanishing — a
    mediocre "undead" coin otherwise lingers in the recheck rotation forever.
    A simple age cap only, no clever conditions (Rule 21). Operator holdings
    are exempt (``holding``) — never auto-prune what he owns. ``max_age_days``
    0 = OFF. Archive is not delete: history/learning/reputation rows all
    remain, and a truly revived coin re-enters via fresh discovery."""
    if max_age_days <= 0.0 or holding:
        return None
    try:
        age_days = (now - entry.added_at).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001 — a bad timestamp must never break the review
        return None
    if age_days <= max_age_days:
        return None
    return (f"watchlist staleness door: tracked {age_days:.1f}d "
            f"(cap {max_age_days:g}d) without graduating")


async def review_entries(
    storage: Storage,
    market_client,  # exposes get_token_pairs(token_address, chain=None)
    pipeline: ResearchPipeline,
    *,
    regime: MarketRegime = MarketRegime.UNKNOWN,
    limit: int = 10,
    skip: frozenset[str] | set[str] = frozenset(),
    snapshot_source: str = "watchlist_review",
    on_result: Callable[[PipelineResult], Awaitable[None]] | None = None,
    max_age_days: float = 0.0,
    now_func: Callable[[], object] | None = None,
) -> list[WatchlistChange]:
    """Re-assess up to ``limit`` tracked tokens; returns every change made.

    * Tokens past the staleness door (``max_age_days`` on the watchlist,
      holdings exempt, 0 = off) are archived without spending a provider
      call — see :func:`stale_watchlist_reason`.
    * Tokens whose trading pairs disappeared are archived (dead market).
    * Tokens re-assessed to Avoid are archived with the score recorded.
    * Everything else is re-tiered per its new classification; unchanged
      entries produce an "updated" change (score refresh) the caller may
      filter out of user-facing summaries.
    * ``skip`` (lowercased addresses) excludes tokens already analyzed in
      the same run — nothing new to learn seconds later (Rule 10).
    """
    now = (now_func or (lambda: datetime.now(timezone.utc)))()
    changes: list[WatchlistChange] = []
    reviewed = 0
    # Least-recently-updated first: with the default tier/score ordering the
    # per-run limit re-reviewed the same top-N forever and starved everything
    # below (never re-assessed, never archived). Reviews bump updated_at, so
    # this ordering rotates the limit through the whole watchlist.
    for entry in sorted(storage.get_watchlist(), key=lambda e: e.updated_at):
        if reviewed >= limit:
            break
        if entry.token.address.lower() in skip:
            continue
        stale = stale_watchlist_reason(
            entry, max_age_days=max_age_days, now=now,
            holding=storage.is_holding(entry.token))
        if stale is not None:
            changes.append(storage.archive(entry.token, stale))
            continue

        try:
            pairs = await market_client.get_token_pairs(
                entry.token.address, chain=entry.token.chain,
            )
        except (CollectorError, AllProvidersFailedError) as exc:
            _logger.info("review skipped for %s: market data unavailable (%s)",
                         entry.token.address, exc)
            continue
        if not pairs:
            changes.append(storage.archive(entry.token, "no active trading pairs remain"))
            continue

        pair = max(pairs, key=lambda p: p.liquidity_usd or 0.0)
        result = await pipeline.analyze_pair(pair, regime=regime)
        if result is None:
            continue
        reviewed += 1
        storage.record_snapshot(
            result.master, source=snapshot_source, pair=result.pair,
            # The only recorder that dropped the regime — review-created
            # predictions landed in Part 24's "unknown" regime bucket even
            # though the regime was known at analysis time (bug-hunt finding).
            regime=regime.value if regime is not MarketRegime.UNKNOWN else None,
            opportunity_rank=result.opportunity.score if result.opportunity else None)
        if on_result is not None:
            await on_result(result)

        tier = TIER_FOR_CLASSIFICATION.get(result.master.classification)
        if tier is None:
            changes.append(storage.archive(
                entry.token,
                f"re-assessment fell to Avoid (score {result.master.final_score:.0f})",
            ))
        else:
            changes.append(storage.update_watchlist(
                entry.token, tier,
                score=result.master.final_score,
                classification=result.master.classification,
            ))

    _logger.info("watchlist review: %d re-assessed, %d change(s)", reviewed, len(changes))
    return changes
