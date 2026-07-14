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
* **No hindsight credit** (adversarial-review finding 2026-07-14): a
  wallet whose first sighting POSTdates a token's first measured outcome
  cannot claim it was "early" — restart re-records and late-recheck
  snapshots capture post-pump chasers, not smart money. Such pairs are
  excluded from credit and reported separately.
* ``early_entry_rate`` and ``median_position_usd`` stay ``None``: holder
  snapshots carry no peak-attention timing and no USD position size, and
  the formula already treats unmeasured dimensions as absent, not neutral.
* A wallet below ``min_resolved`` measurable tokens gets NO score — one
  lucky pick is not a track record.

The join and all counting run inside SQLite (``wallet_reputation_rollup``/
``wallet_reputation_totals``), so memory stays proportional to the wallets
actually reported, never to raw sighting rows — this is called from the
monitor's Telegram poll loop on a 1GB droplet (adversarial-review finding).

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

# The data-clock's provenance tag — the single canonical definition;
# workflow/smart_wallets.py (the writer) and the /wallets command (the
# reader) both import it, so the string cannot drift apart silently.
# Other sighting sources (manual /check runs, wallets CLI) have different
# collection biases, so reputation is computed per-source (Rule 9).
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
    hindsight_tokens: int        # sighted only AFTER the outcome was measured — no credit
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
    pairs_hindsight: int         # (wallet, token) pairs excluded as hindsight


def _parse_stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def compute_wallet_reputations(
    storage,
    settings: BacktestSettings,
    *,
    source: str = DEFAULT_SIGHTING_SOURCE,
    min_resolved: int = 3,
) -> ReputationReport:
    """Join wallet sightings against measured token outcomes and score.

    Pure read-and-compute; the heavy lifting is SQL-side. ``min_resolved``
    below 1 is rejected loudly — it would divide by a zero-resolved wallet
    downstream, and "score wallets with no track record" is never a
    meaningful request (Rule 6).
    """
    if min_resolved < 1:
        raise ValueError(f"min_resolved must be at least 1, got {min_resolved}")

    label_params = dict(
        source=source,
        success_change_percent=settings.success_price_change_percent,
        failure_change_percent=settings.failure_price_change_percent,
    )
    rows = storage.wallet_reputation_rollup(min_resolved=min_resolved, **label_params)
    totals = storage.wallet_reputation_totals(**label_params)

    entries: list[WalletReputationEntry] = []
    for row in rows:
        resolved = int(row["resolved"])
        first_seen = _parse_stamp(row.get("first_seen_at"))
        last_seen = _parse_stamp(row.get("last_seen_at"))
        span_days = None
        if first_seen is not None and last_seen is not None:
            span_days = (last_seen - first_seen).total_seconds() / 86400.0
        record = WalletTrackRecord(
            wallet=row["wallet"],
            tokens_traded=resolved,
            win_rate=int(row["wins"]) / resolved,
            # No peak-attention timing and no USD sizes in holder snapshots —
            # unmeasured stays unmeasured (Rule 8), the formula's coverage
            # honestly shrinks instead.
            early_entry_rate=None,
            median_position_usd=None,
            rug_avoidance_rate=1.0 - (int(row["deaths"]) / resolved),
            active_span_days=span_days,
        )
        scored = wallet_reputation(record)
        if scored is None:
            continue  # nothing measurable (cannot happen with resolved>0, but honest)
        score, coverage = scored
        entries.append(WalletReputationEntry(
            wallet=row["wallet"], score=score, coverage=coverage,
            resolved_tokens=resolved, wins=int(row["wins"]),
            deaths=int(row["deaths"]), pending_tokens=int(row["pending"]),
            hindsight_tokens=int(row["hindsight"]),
            first_seen=first_seen, last_seen=last_seen,
        ))
    entries.sort(key=lambda e: (e.score, e.resolved_tokens), reverse=True)

    report = ReputationReport(
        entries=tuple(entries),
        wallets_seen=totals["wallets_seen"],
        wallets_scored=len(entries),
        min_resolved=min_resolved,
        tokens_sighted=totals["tokens_sighted"],
        tokens_resolved=totals["tokens_resolved"],
        tokens_pending=totals["tokens_sighted"] - totals["tokens_resolved"],
        pairs_hindsight=totals["pairs_hindsight"],
    )
    _logger.info(
        "wallet reputation: %d/%d wallets scored (min_resolved=%d) over "
        "%d sighted tokens (%d resolved, %d pending, %d hindsight pairs excluded)",
        report.wallets_scored, report.wallets_seen, min_resolved,
        report.tokens_sighted, report.tokens_resolved, report.tokens_pending,
        report.pairs_hindsight)
    return report


def _short(wallet: str) -> str:
    """Truncate AND neutralize a wallet string for display. Wallet
    "addresses" originate in GoPlus API responses (untrusted): control
    characters and backticks are stripped like every other externally-
    sourced identity, because a short malicious string bypasses truncation
    entirely (adversarial-review finding)."""
    cleaned = "".join(ch for ch in str(wallet) if ch.isprintable()).replace("`", "'")
    cleaned = cleaned.strip() or "unknown"
    return cleaned[:4] + "…" + cleaned[-4:] if len(cleaned) > 12 else cleaned


def render_reputation_report(report: ReputationReport, *, top: int = 20) -> str:
    """Human-readable report for the CLI and the /wallets Telegram command."""
    top = max(1, top)  # a nonsense top would slice from the wrong end below
    lines = ["WALLET REPUTATION (Part 17 × Part 24)"]
    lines.append(
        f"{report.wallets_seen} wallet(s) sighted over {report.tokens_sighted} token(s) "
        f"— {report.tokens_resolved} resolved, {report.tokens_pending} awaiting outcomes")
    if report.pairs_hindsight:
        lines.append(
            f"{report.pairs_hindsight} sighting(s) excluded as hindsight — recorded only "
            "after their token's outcome was already measured (no credit for chasing)")
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
