"""Plain-language "what did my winners look like" report (operator request, 2026-07-28).

READ-ONLY BY CONTRACT: this module renders what the learning store already
recorded about resolved coins. It computes nothing that feeds back into
scoring, alerting, training, or vetoes, and nothing in the scan/alert path
imports it — only the /winners Telegram command does. Deleting this file
would change zero bot decisions.

Method: for the most recent PUMP-bucket coins and a comparison group of
coins that died (RUG/DUMP), compare what each looked like at the moment the
bot FIRST saw it (its earliest stored snapshot) — the honest stand-in for
"the beginning", since that is when a buy decision could actually have been
made. Medians, not means (one whale coin must not skew the picture), and
any metric with too few real observations reports "not enough data" rather
than a fabricated number (Rule 8). Descriptive, not predictive — for every
winner's early pattern there were many identical-looking coins that died,
which is exactly why the comparison column exists.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Callable, Sequence

from meme_intelligence.learning.models import CoinRecord, CoinSnapshot

# Below this many known values on a side, a comparison line is dishonest.
_MIN_KNOWN_PER_SIDE = 3
# Below this many winners overall there is no report worth rendering.
MIN_WINNERS = 5


def _earliest(record: CoinRecord) -> CoinSnapshot | None:
    """The coin's first stored observation — "what it looked like at the start"."""
    if not record.snapshots:
        return None
    return min(record.snapshots, key=lambda s: s.age_seconds)


def _known(values: Sequence[float | None]) -> list[float]:
    return [float(v) for v in values
            if v is not None and math.isfinite(float(v))]


def _median_of(records: Sequence[CoinRecord],
               pick: Callable[[CoinSnapshot], float | None]) -> tuple[float, int] | None:
    """Median of ``pick(earliest snapshot)`` across records, with sample size.

    Returns ``None`` when fewer than ``_MIN_KNOWN_PER_SIDE`` records had a
    real value — absence of data is reported, never averaged over (Rule 8).
    """
    values = _known([pick(snap) for snap in map(_earliest, records) if snap is not None])
    if len(values) < _MIN_KNOWN_PER_SIDE:
        return None
    return median(values), len(values)


def _fraction(records: Sequence[CoinRecord],
              test: Callable[[CoinRecord], bool | None]) -> tuple[float, int] | None:
    """Fraction of records where ``test`` is True, over those where it is knowable."""
    votes = [t for t in (test(r) for r in records) if t is not None]
    if len(votes) < _MIN_KNOWN_PER_SIDE:
        return None
    return sum(1 for v in votes if v) / len(votes), len(votes)


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _count(value: float) -> str:
    return f"{value:,.0f}"


def _percent(value: float) -> str:
    return f"{value:.0f}%"


def _compare_line(label: str, winners_stat: tuple[float, int] | None,
                  losers_stat: tuple[float, int] | None,
                  fmt: Callable[[float], str]) -> str:
    if winners_stat is None or losers_stat is None:
        return f"  {label}: not enough data"
    return f"  {label}: {fmt(winners_stat[0])} vs {fmt(losers_stat[0])}"


def _best_return_percent(record: CoinRecord) -> float | None:
    """The coin's best measured forward return across its graded horizons."""
    returns = _known([label.forward_return_percent for label in record.labels])
    return max(returns) if returns else None


def _buy_pressure(snapshot: CoinSnapshot) -> float | None:
    if snapshot.buys is None or snapshot.sells is None:
        return None
    total = snapshot.buys + snapshot.sells
    if total <= 0:
        return None
    return 100.0 * snapshot.buys / total


def build_winners_report(winners: Sequence[CoinRecord],
                         losers: Sequence[CoinRecord]) -> str:
    """Render the winners-vs-died comparison card for Telegram.

    ``winners`` are PUMP-bucket records, ``losers`` RUG/DUMP-bucket records,
    both expected newest-first from the store. Pure function — safe to call
    from a worker thread with no side effects anywhere.
    """
    if len(winners) < MIN_WINNERS:
        return (f"WINNERS REPORT\nNot enough winners recorded yet: the bot has "
                f"graded {len(winners)} pump(s) so far (needs {MIN_WINNERS}). "
                "It keeps learning — try again in a few days.")

    lines = [
        "WINNERS REPORT — what the bot's recorded winners had in common",
        f"(last {len(winners)} coins that pumped vs {len(losers)} that died; "
        "read-only — changes nothing about scanning or alerts)",
        "",
        "At first sight (winner vs died):",
        _compare_line("Holders", _median_of(winners, lambda s: s.holder_count),
                      _median_of(losers, lambda s: s.holder_count), _count),
        _compare_line("Liquidity", _median_of(winners, lambda s: s.liquidity_usd),
                      _median_of(losers, lambda s: s.liquidity_usd), _money),
        _compare_line("1h volume", _median_of(winners, lambda s: s.volume_1h_usd),
                      _median_of(losers, lambda s: s.volume_1h_usd), _money),
        _compare_line("Buy share of trades", _median_of(winners, _buy_pressure),
                      _median_of(losers, _buy_pressure), _percent),
        _compare_line("Top-10 wallets hold", _median_of(winners, lambda s: s.top10_holder_percent),
                      _median_of(losers, lambda s: s.top10_holder_percent), _percent),
    ]

    dev_selling = (_fraction(winners, lambda r: (None if _earliest(r) is None
                                                 or _earliest(r).dev_outflow_usd is None
                                                 else _earliest(r).dev_outflow_usd > 0)),
                   _fraction(losers, lambda r: (None if _earliest(r) is None
                                                or _earliest(r).dev_outflow_usd is None
                                                else _earliest(r).dev_outflow_usd > 0)))
    if dev_selling[0] is not None and dev_selling[1] is not None:
        lines.append(f"  Dev already selling: {_percent(100 * dev_selling[0][0])} "
                     f"vs {_percent(100 * dev_selling[1][0])}")

    rug_flags = (_fraction(winners, lambda r: bool(r.rug_signals)),
                 _fraction(losers, lambda r: bool(r.rug_signals)))
    if rug_flags[0] is not None and rug_flags[1] is not None:
        lines.append(f"  Tripped a rug signal during life: "
                     f"{_percent(100 * rug_flags[0][0])} vs {_percent(100 * rug_flags[1][0])}")

    best = _known([_best_return_percent(r) for r in winners])
    if len(best) >= _MIN_KNOWN_PER_SIDE:
        lines.append("")
        lines.append(f"Median winner peaked at +{median(best):,.0f}% vs where the bot found it.")

    lines.append("")
    lines.append("Descriptive, not predictive: plenty of dead coins started out "
                 "looking exactly like these winners — that gap is why the buy "
                 "decision stays yours.")
    return "\n".join(lines)
