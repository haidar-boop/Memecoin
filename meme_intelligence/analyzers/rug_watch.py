"""Rug-IN-PROGRESS detection for coins the operator already owns.

The existing :mod:`meme_intelligence.learning.rug_engine` answers "would this
coin rug?" *before* an alert. This module answers a different, much more
tractable question about money already committed: **is this coin rugging right
now, and can I still get out?**

Why this is a separate engine (Rule 4, Rule 19)
-----------------------------------------------
Prediction and detection have opposite error costs. A pre-alert screen that is
too eager just costs a missed opportunity. A watcher that is too eager sells a
healthy position at a loss — real money, immediately. So this engine:

* fires ONLY on positive, measured evidence; unknown liquidity, a failed
  provider call, or a missing sell route reading never move it (Rule 8 — the
  outage that fabricated -100% returns is the cautionary tale here);
* requires the SAME trigger to hold across consecutive independent readings
  before it will recommend selling, so one bad provider tick cannot liquidate
  a position;
* separates "get out now" from "you should know about this", because the two
  deserve different actions.

The critical asymmetry (Rule 21 — simple and honest beats clever)
----------------------------------------------------------------
A vanished sell route is the *end* of a rug, not the start: once Jupiter can
find no path out, selling is no longer possible and an auto-sell would be
futile. The moment worth acting on is liquidity draining hard **while a sell
route still exists**. That is why route-gone raises a loud warning rather than
an exit — there is nothing left to execute — and a confirmed liquidity
collapse with a live route is what triggers the exit.

Pure and synchronous by design: it takes a list of readings and returns a
verdict, so every threshold and confirmation rule is testable without network,
clock, or database.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from meme_intelligence.config.settings import RugWatchSettings

# An INTERIOR reading more than this factor above everything both before AND
# after it is a spike — one glitched HIGH tick (a mis-scaled provider figure),
# not a level the pool ever held. Left in, it poisons the peak so that
# perfectly ordinary readings look like a confirmed collapse — the exact
# single-tick liquidation this engine promises cannot happen (2026-07-31
# adversarial review finding, reproduced). Only interior values can be
# spikes: a high FIRST reading is the honest baseline (the classic
# one-healthy-reading-then-drain rug must still exit), and a high LAST
# reading is the current level (drop from it is zero anyway). A genuine pump
# survives because its other high readings sit before or after each of them.
_PEAK_SPIKE_FACTOR = 2.0

# Verdict actions, in ascending order of urgency.
HOLD = "hold"
WARN = "warn"
EXIT = "exit"


@dataclass(frozen=True)
class LiquidityReading:
    """One observation of a held coin's tradability.

    Every field may be unknown. ``liquidity_usd=None`` means the provider could
    not be read — NOT that liquidity is zero. ``sell_route_ok=None`` means the
    route was not probed or the probe failed; only ``False`` is evidence that
    the exit is closed.
    """

    at: datetime
    liquidity_usd: float | None = None
    sell_route_ok: bool | None = None

    @property
    def liquidity_known(self) -> bool:
        return (self.liquidity_usd is not None
                and math.isfinite(self.liquidity_usd)
                and self.liquidity_usd >= 0.0)


@dataclass(frozen=True)
class RugWatchVerdict:
    """What to do about one held coin, and the evidence behind it."""

    action: str                              # HOLD / WARN / EXIT
    reasons: tuple[str, ...] = ()
    peak_liquidity_usd: float | None = None
    latest_liquidity_usd: float | None = None
    drop_percent: float | None = None
    confirmations: int = 0
    sell_route_ok: bool | None = None

    @property
    def should_exit(self) -> bool:
        return self.action == EXIT

    def summary(self) -> str:
        """One line for the operator's phone."""
        if self.drop_percent is None:
            return ", ".join(self.reasons) or "no measured change"
        return (f"liquidity ${self.latest_liquidity_usd:,.0f} vs peak "
                f"${self.peak_liquidity_usd:,.0f} ({self.drop_percent:.0f}% down)")


def _drop_percent(peak: float, latest: float) -> float | None:
    """How far ``latest`` has fallen from ``peak``, as a positive percentage."""
    if peak <= 0.0 or not math.isfinite(peak) or not math.isfinite(latest):
        return None
    return max(0.0, 100.0 * (peak - latest) / peak)


def assess_rug_in_progress(
    readings: list[LiquidityReading],
    settings: RugWatchSettings,
) -> RugWatchVerdict:
    """Decide whether a held coin is rugging right now.

    ``readings`` are ordered oldest-first. The peak is taken over the *known*
    readings minus interior spikes (see ``_PEAK_SPIKE_FACTOR``): a coin that
    genuinely grew before draining is measured against the size it actually
    reached, while a single glitched high tick between ordinary readings
    cannot poison the baseline. A high first reading is kept — the classic
    one-healthy-reading-then-drain rug must still exit.

    Sell-route evidence expires: a ``False`` probe older than
    ``route_evidence_max_age_seconds`` (measured against the newest reading)
    decays to unknown. Without that, one transient "no route" reading anywhere
    in the window silently blocked every future auto-sell while the armed
    warning kept promising one (2026-07-31 adversarial review finding).
    """
    known = [r for r in readings if r.liquidity_known]
    latest_route = None
    if readings:
        route_cutoff = readings[-1].at - timedelta(
            seconds=settings.route_evidence_max_age_seconds)
        latest_route = next(
            (r.sell_route_ok for r in reversed(readings)
             if r.sell_route_ok is not None and r.at >= route_cutoff),
            None)

    # Not enough measured history to say anything. A verdict built on one
    # reading has no baseline to fall from, and guessing here sells positions.
    if len(known) < settings.min_readings:
        reasons = ()
        if latest_route is False:
            reasons = ("sell route has disappeared — you may not be able to exit",)
            return RugWatchVerdict(action=WARN, reasons=reasons,
                                   sell_route_ok=latest_route)
        return RugWatchVerdict(action=HOLD, sell_route_ok=latest_route,
                               latest_liquidity_usd=(known[-1].liquidity_usd
                                                     if known else None))

    values = [r.liquidity_usd for r in known]
    # Peak over the values that are NOT interior spikes (see
    # _PEAK_SPIKE_FACTOR). First and last values are never spikes, so the
    # kept list is never empty.
    prefix_max: list[float | None] = []
    running: float | None = None
    for v in values:
        prefix_max.append(running)
        running = v if running is None or v > running else running
    suffix_max: list[float | None] = [None] * len(values)
    running = None
    for i in range(len(values) - 1, -1, -1):
        suffix_max[i] = running
        v = values[i]
        running = v if running is None or v > running else running
    peak = max(
        v for i, v in enumerate(values)
        if not (prefix_max[i] is not None and suffix_max[i] is not None
                and prefix_max[i] < v / _PEAK_SPIKE_FACTOR
                and suffix_max[i] < v / _PEAK_SPIKE_FACTOR))
    latest = known[-1].liquidity_usd
    drop = _drop_percent(peak, latest)

    # Count how many of the most recent CONSECUTIVE known readings each
    # independently clear the exit threshold. One bad tick cannot reach the
    # confirmation count; a genuine drain holds across polls.
    confirmations = 0
    for reading in reversed(known):
        reading_drop = _drop_percent(peak, reading.liquidity_usd)
        if reading_drop is not None and reading_drop >= settings.exit_drop_percent:
            confirmations += 1
        else:
            break

    reasons: list[str] = []
    if drop is not None and drop >= settings.warn_drop_percent:
        reasons.append(
            f"liquidity fell {drop:.0f}% from its peak "
            f"(${peak:,.0f} -> ${latest:,.0f})")
    if latest_route is False:
        reasons.append("sell route has disappeared — you may not be able to exit")

    # EXIT requires: a confirmed collapse AND a route still open to sell into.
    # With the route already gone there is nothing to execute, so that case
    # stays a warning however severe the drop (see the module docstring).
    if confirmations >= settings.min_confirmations and latest_route is not False:
        return RugWatchVerdict(
            action=EXIT,
            reasons=tuple(reasons) or (f"liquidity fell {drop:.0f}% from its peak",),
            peak_liquidity_usd=peak, latest_liquidity_usd=latest,
            drop_percent=drop, confirmations=confirmations,
            sell_route_ok=latest_route)

    if reasons:
        return RugWatchVerdict(
            action=WARN, reasons=tuple(reasons),
            peak_liquidity_usd=peak, latest_liquidity_usd=latest,
            drop_percent=drop, confirmations=confirmations,
            sell_route_ok=latest_route)

    return RugWatchVerdict(
        action=HOLD, peak_liquidity_usd=peak, latest_liquidity_usd=latest,
        drop_percent=drop, confirmations=confirmations, sell_route_ok=latest_route)
