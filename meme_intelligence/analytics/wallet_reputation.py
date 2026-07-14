"""Wallet reputation from measured outcomes (Part 17 Section 2 × Part 24).

The connector between two systems that already exist:

* the **smart-wallet data clock** (``workflow/smart_wallets.py``) records
  which wallets held each analyzed token early in its life
  (``wallet_sightings``, source ``goplus_holders``);
* **outcome tracking** (``analytics/backtesting.py``, the 6-hourly
  ``backtest --refresh`` cron) measures what each of those tokens then
  actually did (``outcomes``: price change per window, survival).

Joining them answers the only question reputation needs: *which wallets
keep showing up early on tokens that turn out well?* Each wallet's join
result becomes a :class:`WalletTrackRecord` scored by the Part 17
``wallet_reputation`` formula that has been waiting for this data.

Honesty contract (Rule 8):

* Token labels reuse the EXACT backtest thresholds
  (``BacktestSettings.success/failure_price_change_percent``, survival
  floor) — no second definition of "winner" (Rule 17/18). A token whose
  measured windows hit neither threshold is *undetermined* and counts
  toward neither win_rate nor rug_avoidance (sideways proves nothing).
* ``early_entry_rate`` and ``median_position_usd`` stay ``None``: holder
  snapshots carry no peak-attention timing and no USD position size, and
  the formula already treats unmeasured dimensions as absent, not neutral.
* A wallet below ``min_resolved`` measurable tokens gets NO score — one
  lucky pick is not a track record.

This module only reads and computes. Persisting scores, feeding them into
the live scan, and the smart-money alert are later, separate steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from meme_intelligence.analyzers.wallet_intelligence import (
    WalletTrackRecord,
    wallet_reputation,
)
from meme_intelligence.config.settings import BacktestSettings
from meme_intelligence.core.logging_setup import get_logger

_logger = get_logger("analytics.wallet_reputation")

# The data-clock's provenance tag (workflow/smart_wallets.py). Other sighting
# sources (manual /check runs, wallets CLI) have different collection biases,
# so reputation is computed per-source rather than blended silently (Rule 9).
DEFAULT_SIGHTING_SOURCE = "goplus_holders"


@dataclass(frozen=True)
class WalletReputationEntry:
    """One wallet's measured track record and its Part 17 score."""

    wallet: str
    score: float                 # 0-100 (wallet_reputation formula)
    coverage: float              # 0-1: fraction of formula weight measurable
    resolved_tokens: int         # tokens with a win or a loss on record
    wins: int                    # tokens that hit the success threshold
    deaths: int                  # tokens whose liquidity died (confirmed rug/failure)
    pending_tokens: int          # sightings still awaiting measurable outcomes
    first_seen: datetime | None  # earliest sighting (clock time, UTC)
    last_seen: datetime | None


@dataclass(frozen=True)
class ReputationReport:
    """The full join result plus honest denominators (a rate without its
    sample size is a lie of omission — Part 24 Section 1)."""

    entries: tuple[WalletReputationEntry, ...]  # scored wallets, best first
    wallets_seen: int            # distinct wallets with any sighting
    wallets_scored: int          # wallets clearing min_resolved
    min_resolved: int            # the gate the scored wallets cleared
    tokens_sighted: int          # distinct tokens with sightings
    tokens_resolved: int         # sighted tokens with a win/loss label
    tokens_pending: int          # sighted tokens with no measurable outcome yet


def _parse_stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _label(aggregate: dict, settings: BacktestSettings) -> tuple[bool, bool]:
    """(won, died) for one token's measured outcome windows.

    Mirrors ``evaluate_predictions``: won when the BEST window hit the
    success threshold; died/failed when liquidity fell below the survival
    floor or the WORST window hit the failure threshold. The two are
    independent — a token can pump 60% and then rug, and an early holder's
    record honestly carries both the win and the death.
    """
    best = aggregate.get("best_change")
    worst = aggregate.get("worst_change")
    died = bool(aggregate.get("died"))
    won = best is not None and best >= settings.success_price_change_percent
    lost = died or (worst is not None
                    and worst <= settings.failure_price_change_percent)
    return won, lost


def compute_wallet_reputations(
    storage,
    settings: BacktestSettings,
    *,
    source: str = DEFAULT_SIGHTING_SOURCE,
    min_resolved: int = 3,
) -> ReputationReport:
    """Join wallet sightings against measured token outcomes and score.

    Pure read-and-compute over two indexed SQLite tables — cheap enough to
    run on demand (no persisted score table yet; that is a later step once
    the numbers have been watched for a while).
    """
    sightings = storage.wallet_sightings_for_reputation(source=source)
    aggregates = {row["token_id"]: row for row in storage.token_outcome_aggregates()}

    per_wallet: dict[str, dict] = {}
    tokens_sighted: set[int] = set()
    tokens_resolved: set[int] = set()
    tokens_pending: set[int] = set()

    for row in sightings:
        wallet, token_id = row["wallet"], row["token_id"]
        tokens_sighted.add(token_id)
        stats = per_wallet.setdefault(wallet, {
            "resolved": 0, "wins": 0, "deaths": 0, "pending": 0,
            "first_seen": None, "last_seen": None,
        })
        seen = _parse_stamp(row.get("first_seen_at"))
        if seen is not None:
            if stats["first_seen"] is None or seen < stats["first_seen"]:
                stats["first_seen"] = seen
            if stats["last_seen"] is None or seen > stats["last_seen"]:
                stats["last_seen"] = seen

        aggregate = aggregates.get(token_id)
        if aggregate is None:
            stats["pending"] += 1
            tokens_pending.add(token_id)
            continue
        won, lost = _label(aggregate, settings)
        if not won and not lost:
            # Measured but undetermined: sideways proves nothing (Rule 8) —
            # it is neither a win nor an avoided/hit rug.
            stats["pending"] += 1
            tokens_pending.add(token_id)
            continue
        tokens_resolved.add(token_id)
        stats["resolved"] += 1
        if won:
            stats["wins"] += 1
        if lost and bool(aggregate.get("died")):
            stats["deaths"] += 1

    entries: list[WalletReputationEntry] = []
    for wallet, stats in per_wallet.items():
        if stats["resolved"] < min_resolved:
            continue
        span_days = None
        if stats["first_seen"] is not None and stats["last_seen"] is not None:
            span_days = (stats["last_seen"] - stats["first_seen"]).total_seconds() / 86400.0
        record = WalletTrackRecord(
            wallet=wallet,
            tokens_traded=stats["resolved"],
            win_rate=stats["wins"] / stats["resolved"],
            # No peak-attention timing and no USD sizes in holder snapshots —
            # unmeasured stays unmeasured (Rule 8), the formula's coverage
            # honestly shrinks instead.
            early_entry_rate=None,
            median_position_usd=None,
            rug_avoidance_rate=1.0 - (stats["deaths"] / stats["resolved"]),
            active_span_days=span_days,
        )
        scored = wallet_reputation(record)
        if scored is None:
            continue  # nothing measurable (cannot happen with resolved>0, but honest)
        score, coverage = scored
        entries.append(WalletReputationEntry(
            wallet=wallet, score=score, coverage=coverage,
            resolved_tokens=stats["resolved"], wins=stats["wins"],
            deaths=stats["deaths"], pending_tokens=stats["pending"],
            first_seen=stats["first_seen"], last_seen=stats["last_seen"],
        ))
    entries.sort(key=lambda e: (e.score, e.resolved_tokens), reverse=True)

    report = ReputationReport(
        entries=tuple(entries),
        wallets_seen=len(per_wallet),
        wallets_scored=len(entries),
        min_resolved=min_resolved,
        tokens_sighted=len(tokens_sighted),
        tokens_resolved=len(tokens_resolved),
        tokens_pending=len(tokens_pending),
    )
    _logger.info(
        "wallet reputation: %d/%d wallets scored (min_resolved=%d) over "
        "%d sighted tokens (%d resolved, %d pending)",
        report.wallets_scored, report.wallets_seen, min_resolved,
        report.tokens_sighted, report.tokens_resolved, report.tokens_pending)
    return report


def _short(wallet: str) -> str:
    return wallet[:4] + "…" + wallet[-4:] if len(wallet) > 12 else wallet


def render_reputation_report(report: ReputationReport, *, top: int = 20) -> str:
    """Human-readable report for the CLI and the /wallets Telegram command."""
    lines = ["WALLET REPUTATION (Part 17 × Part 24)"]
    lines.append(
        f"{report.wallets_seen} wallet(s) sighted over {report.tokens_sighted} token(s) "
        f"— {report.tokens_resolved} resolved, {report.tokens_pending} awaiting outcomes")
    if not report.entries:
        lines.append(
            f"no wallet has ≥{report.min_resolved} resolved tokens yet — the clock "
            "needs to run longer before any score means anything")
        return "\n".join(lines)
    lines.append(f"{report.wallets_scored} wallet(s) scored "
                 f"(each ≥{report.min_resolved} resolved tokens):")
    for entry in report.entries[:top]:
        lines.append(
            f"  {_short(entry.wallet)}  {entry.score:.0f}/100 "
            f"(coverage {entry.coverage:.0%}) — {entry.resolved_tokens} resolved: "
            f"{entry.wins} win(s), {entry.deaths} death(s), "
            f"{entry.pending_tokens} pending")
    if report.wallets_scored > top:
        lines.append(f"  … and {report.wallets_scored - top} more")
    lines.append("A track record, not a guarantee — scores only say a wallet was "
                 "repeatedly early on measured winners.")
    return "\n".join(lines)
