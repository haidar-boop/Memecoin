"""Multi-token comparison (Spec Part 16, Section 7).

Renders a category-by-category table across analyzed tokens and produces
an explicit ranking. Ranking rules:

* Tokens with red-flag overrides (honeypot, fake community, extreme risk)
  rank below every non-overridden token regardless of score — an Avoid
  with a pretty number is still an Avoid (Part 10, Section 5).
* Otherwise rank by final score; evidence coverage breaks ties (a 75
  built on full data beats a 75 built on half the framework).
"""

from __future__ import annotations

import dataclasses

from meme_intelligence.core.enums import Classification
from meme_intelligence.workflow.pipeline import PipelineResult

_CATEGORY_ORDER = (
    "security", "community", "blockchain", "foundation",
    "momentum", "narrative", "timing",
)
_COLUMN_WIDTH = 14


def rank_results(results: list[PipelineResult]) -> list[PipelineResult]:
    """Order results best-first per the comparison ranking rules.

    Classification outranks the raw number: a decision-tree-REJECTED token
    is classified Avoid with an EMPTY overrides tuple, so the overrides-only
    sort let it top the ranking on a pretty score (bug-hunt finding) — but
    an Avoid with a pretty number is still an Avoid.
    """
    return sorted(
        results,
        key=lambda r: (
            r.master.classification is not Classification.AVOID,  # Avoid sinks
            len(r.master.overrides) == 0,   # non-overridden tokens first
            r.master.final_score,
            r.master.coverage,
        ),
        reverse=True,
    )


def render_comparison(results: list[PipelineResult]) -> str:
    """The Part 16 Section 7 output: comparison table, then explicit ranking."""
    if not results:
        return "Nothing to compare."

    ranked = rank_results(results)

    def label(result: PipelineResult) -> str:
        token = result.pair.base_token
        return (token.symbol or token.address[:8])[: _COLUMN_WIDTH - 2]

    def cell(value: float | None) -> str:
        return f"{value:.0f}" if value is not None else "no data"

    lines: list[str] = ["TOKEN COMPARISON", ""]

    header = f"{'category':>12} | " + " | ".join(f"{label(r):>{_COLUMN_WIDTH}}" for r in ranked)
    lines.append(header)
    lines.append("-" * len(header))

    for category in _CATEGORY_ORDER:
        row = [
            f"{cell(dataclasses.asdict(r.master.category_scores)[category]):>{_COLUMN_WIDTH}}"
            for r in ranked
        ]
        lines.append(f"{category:>12} | " + " | ".join(row))

    lines.append(f"{'FINAL':>12} | " + " | ".join(
        f"{r.master.final_score:>{_COLUMN_WIDTH}.0f}" for r in ranked))
    lines.append(f"{'class':>12} | " + " | ".join(
        f"{r.master.classification.value[:_COLUMN_WIDTH]:>{_COLUMN_WIDTH}}" for r in ranked))
    lines.append(f"{'coverage':>12} | " + " | ".join(
        f"{r.master.coverage:>{_COLUMN_WIDTH}.0%}" for r in ranked))
    lines.append(f"{'risk tier':>12} | " + " | ".join(
        f"{r.security.tier.value[:_COLUMN_WIDTH]:>{_COLUMN_WIDTH}}" for r in ranked))

    lines.append("")
    lines.append("RANKING")
    for position, result in enumerate(ranked, 1):
        reason = _rank_reason(result)
        lines.append(f"  {position}. {label(result)} — {result.master.final_score:.0f}/100, "
                     f"{result.master.classification.value} ({reason})")
    return "\n".join(lines)


def _rank_reason(result: PipelineResult) -> str:
    if result.master.overrides:
        return f"red-flag override: {result.master.overrides[0]}"
    scores = {k: v for k, v in dataclasses.asdict(result.master.category_scores).items()
              if v is not None}
    if not scores:
        return "insufficient data"
    strongest = max(scores, key=scores.get)
    weakest = min(scores, key=scores.get)
    return (f"strongest: {strongest} {scores[strongest]:.0f}, "
            f"weakest: {weakest} {scores[weakest]:.0f}, "
            f"coverage {result.master.coverage:.0%}")
