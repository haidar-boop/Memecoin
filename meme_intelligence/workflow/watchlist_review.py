"""Shared watchlist review (Spec Part 16 Section 8, Part 11 Sections 6-7, Part 28).

One implementation of "re-check the tokens we track": refresh market data,
re-run the research pipeline, re-tier or archive, and report every change.
Used by the ``watchlist --refresh`` command and the daily routine (the
continuous scanner keeps its own variant because each result there also
flows through alert verification).
"""

from __future__ import annotations

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
) -> list[WatchlistChange]:
    """Re-assess up to ``limit`` tracked tokens; returns every change made.

    * Tokens whose trading pairs disappeared are archived (dead market).
    * Tokens re-assessed to Avoid are archived with the score recorded.
    * Everything else is re-tiered per its new classification; unchanged
      entries produce an "updated" change (score refresh) the caller may
      filter out of user-facing summaries.
    * ``skip`` (lowercased addresses) excludes tokens already analyzed in
      the same run — nothing new to learn seconds later (Rule 10).
    """
    changes: list[WatchlistChange] = []
    reviewed = 0
    for entry in storage.get_watchlist():
        if reviewed >= limit:
            break
        if entry.token.address.lower() in skip:
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
        storage.record_snapshot(result.master, source=snapshot_source, pair=result.pair)
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
