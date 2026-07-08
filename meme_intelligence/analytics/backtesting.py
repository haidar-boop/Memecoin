"""Backtesting, performance tracking & self-improvement (Spec Part 24).

The measurement loop the whole system has been feeding since Part 11:
every assessment snapshot is a *prediction record* (Section 3), and this
module joins those predictions with what the market did afterwards.

Doctrine encoded here (Sections 1 and 14):

* One good call proves nothing — metrics report sample sizes everywhere,
  and weight experiments refuse to run on tiny samples.
* Failures are studied, not ignored: every graded failure names the
  signals that were confidently wrong (Section 7).
* Nothing self-modifies. The Part 31 Framework Consistency Lock keeps the
  shipped weights canonical; Section 5 weight experiments REPORT which
  weighting would have discriminated winners better, and a human applies
  changes via env overrides — recording them per Section 11 (see
  ``record_strategy_change``). Measurement informs; it never silently
  rewrites the framework (Rule 20).
* Undetermined stays undetermined: a prediction whose windows have not
  elapsed, or whose price went sideways, is neither a hit nor a miss
  (Rule 8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from meme_intelligence.config.settings import BacktestSettings, ScoringWeights
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity

_logger = get_logger("analytics.backtesting")

# Classifications graded as positive calls vs explicit avoid calls.
_POSITIVE_CLASSES = ("elite_opportunity", "strong_candidate")
_NEGATIVE_CLASSES = ("avoid",)

# Section 5's example experiment variants: tilt one category up, keep the
# rest proportional. Reported against the Part 31 locked baseline.
WEIGHT_VARIANTS: dict[str, ScoringWeights] = {
    "locked_baseline": ScoringWeights(),
    "security_heavy": ScoringWeights(foundation=0.12, security=0.28, community=0.12,
                                     blockchain=0.12, momentum=0.12, narrative=0.12,
                                     timing=0.12),
    "community_heavy": ScoringWeights(foundation=0.12, security=0.12, community=0.28,
                                      blockchain=0.12, momentum=0.12, narrative=0.12,
                                      timing=0.12),
    "narrative_heavy": ScoringWeights(foundation=0.12, security=0.12, community=0.12,
                                      blockchain=0.12, momentum=0.12, narrative=0.28,
                                      timing=0.12),
}


def _parse_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---- Outcome collection (Sections 2-3) ----

async def refresh_outcomes(
    storage,
    market_service=None,  # optional: fills windows live when no snapshot exists
    *,
    settings: BacktestSettings,
    now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    """Measure every due, unmeasured (prediction, window) pair.

    Preferred source is a later stored snapshot near the window target
    (free — the scanner already collected it); when none exists and a
    market service is provided, the current pair is fetched live and the
    actual elapsed time recorded. Windows that are not due yet stay open.
    Returns the number of outcomes recorded.
    """
    now = now_func()
    recorded = 0
    for prediction in storage.predictions():
        predicted_at = _parse_at(prediction["created_at"])
        base_price = prediction["price_usd"]
        existing = storage.outcomes_for_snapshot(prediction["snapshot_id"])
        series = None  # fetched lazily per token

        for window in settings.window_list:
            if window in existing:
                continue
            target = predicted_at + timedelta(hours=window)
            if target > now:
                continue  # not due yet — never measured early (Section 14)

            if series is None:
                series = storage.snapshots_for_token(prediction["token_id"])
            measurement = _nearest_snapshot(series, prediction["snapshot_id"],
                                            target, window, settings)
            source = "snapshot"
            if measurement is None and market_service is not None:
                measurement = await _live_measurement(market_service, prediction, now)
                source = "live_fetch"
            if measurement is None:
                continue  # honest gap: nothing observed near this window

            price, liquidity, measured_at = measurement
            change = (100.0 * (price - base_price) / base_price
                      if price is not None and base_price else None)
            survived = (liquidity >= settings.survival_min_liquidity_usd
                        if liquidity is not None else None)
            storage.record_outcome(
                snapshot_id=prediction["snapshot_id"],
                token_id=prediction["token_id"],
                window_hours=window,
                target_at=target.isoformat(),
                measured_at=measured_at.isoformat(),
                price_usd=price,
                price_change_percent=change,
                liquidity_usd=liquidity,
                survived=survived,
                source=source,
            )
            recorded += 1
    if recorded:
        _logger.info("recorded %d new outcome measurement(s)", recorded)
    return recorded


def _nearest_snapshot(series, prediction_id, target, window, settings):
    """Closest later snapshot within tolerance of the window target."""
    tolerance = timedelta(hours=window * settings.window_tolerance_fraction)
    best = None
    for row in series:
        if row["id"] == prediction_id or row["price_usd"] is None:
            continue
        at = _parse_at(row["created_at"])
        distance = abs(at - target)
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, row["price_usd"], row["liquidity_usd"], at)
    if best is None:
        return None
    return best[1], best[2], best[3]


async def _live_measurement(market_service, prediction, now):
    token = TokenIdentity(chain=prediction["chain"], address=prediction["address"])
    try:
        pair = await market_service.get_best_pair(token.address, chain=token.chain)
    except CollectorError as exc:
        _logger.info("live outcome fetch failed for %s: %s", token.address, exc)
        return None
    if pair is None:
        # No tradable pair anymore: the token is dead — that IS the outcome.
        return 0.0, 0.0, now
    return pair.price_usd, pair.liquidity_usd, now


# ---- Prediction grading (Sections 3-4) ----

@dataclass(frozen=True)
class PredictionVerdict:
    """One graded prediction: what the framework said vs what happened."""

    token_symbol: str
    chain: str
    classification: str
    confidence: str
    regime: str | None
    final_score: float
    category_scores: dict
    best_change_percent: float | None   # best window (opportunity realized)
    worst_change_percent: float | None  # worst window (drawdown/failure)
    died: bool
    windows_measured: int
    verdict: str  # correct / incorrect / undetermined / ungraded


def evaluate_predictions(storage, settings: BacktestSettings) -> list[PredictionVerdict]:
    """Grade every prediction that has at least one measured window.

    Positive calls (Elite/Strong Candidate) are correct when the success
    threshold was reached, incorrect on failure/death. Avoid calls grade
    inverted. Watchlist/Speculative are middle calls — tracked but
    ungraded for accuracy (they assert neither outcome).
    """
    verdicts: list[PredictionVerdict] = []
    for prediction in storage.predictions():
        outcomes = storage.outcomes_for_snapshot(prediction["snapshot_id"]).values()
        changes = [o["price_change_percent"] for o in outcomes
                   if o["price_change_percent"] is not None]
        died = any(o["survived"] == 0 for o in outcomes)
        if not changes and not died:
            continue  # nothing measured yet
        best = max(changes) if changes else None
        worst = min(changes) if changes else None

        succeeded = best is not None and best >= settings.success_price_change_percent
        failed = died or (worst is not None
                          and worst <= settings.failure_price_change_percent)

        classification = prediction["classification"]
        if classification in _POSITIVE_CLASSES:
            verdict = ("correct" if succeeded
                       else "incorrect" if failed else "undetermined")
        elif classification in _NEGATIVE_CLASSES:
            verdict = ("correct" if failed
                       else "incorrect" if succeeded else "undetermined")
        else:
            verdict = "ungraded"

        verdicts.append(PredictionVerdict(
            token_symbol=prediction["symbol"] or prediction["address"][:8],
            chain=prediction["chain"],
            classification=classification,
            confidence=prediction["confidence"],
            regime=prediction["regime"],
            final_score=prediction["final_score"],
            category_scores=json.loads(prediction["category_scores"]),
            best_change_percent=best,
            worst_change_percent=worst,
            died=died,
            windows_measured=len(outcomes),
            verdict=verdict,
        ))
    return verdicts


@dataclass
class PerformanceMetrics:
    """Section 4 metrics plus Section 9/12 breakdowns. Counts everywhere —
    a rate without its sample size is a lie of omission (Section 1)."""

    predictions_graded: int = 0
    accuracy_percent: float | None = None            # correct / (correct+incorrect)
    opportunity_detection_percent: float | None = None
    false_positive_percent: float | None = None
    risk_detection_percent: float | None = None
    by_classification: dict = field(default_factory=dict)
    by_regime: dict = field(default_factory=dict)
    by_confidence: dict = field(default_factory=dict)  # Section 12 calibration


def performance_metrics(verdicts: list[PredictionVerdict],
                        settings: BacktestSettings) -> PerformanceMetrics:
    metrics = PerformanceMetrics()
    graded = [v for v in verdicts if v.verdict in ("correct", "incorrect")]
    metrics.predictions_graded = len(graded)
    if graded:
        correct = sum(1 for v in graded if v.verdict == "correct")
        metrics.accuracy_percent = 100.0 * correct / len(graded)

    # Opportunity detection (S4): among tokens that actually succeeded, how
    # many did the framework rate positively (any non-avoid class)?
    winners = [v for v in verdicts
               if v.best_change_percent is not None
               and v.best_change_percent >= settings.success_price_change_percent]
    if winners:
        found = sum(1 for v in winners if v.classification not in _NEGATIVE_CLASSES)
        metrics.opportunity_detection_percent = 100.0 * found / len(winners)

    # False positives (S4): positive calls that failed outright.
    positives = [v for v in verdicts if v.classification in _POSITIVE_CLASSES
                 and v.verdict != "undetermined"]
    if positives:
        failed = sum(1 for v in positives if v.verdict == "incorrect")
        metrics.false_positive_percent = 100.0 * failed / len(positives)

    # Risk detection (S4): among tokens that failed/died, how many were
    # called Avoid at prediction time?
    failures = [v for v in verdicts if v.died or (
        v.worst_change_percent is not None
        and v.worst_change_percent <= settings.failure_price_change_percent)]
    if failures:
        caught = sum(1 for v in failures if v.classification in _NEGATIVE_CLASSES)
        metrics.risk_detection_percent = 100.0 * caught / len(failures)

    def bucket(items, key):
        result: dict[str, dict] = {}
        for v in items:
            k = key(v) or "unknown"
            slot = result.setdefault(k, {"graded": 0, "correct": 0})
            if v.verdict in ("correct", "incorrect"):
                slot["graded"] += 1
                slot["correct"] += v.verdict == "correct"
        for slot in result.values():
            slot["accuracy_percent"] = (100.0 * slot["correct"] / slot["graded"]
                                        if slot["graded"] else None)
        return result

    metrics.by_classification = bucket(verdicts, lambda v: v.classification)
    metrics.by_regime = bucket(verdicts, lambda v: v.regime)          # Section 9
    metrics.by_confidence = bucket(verdicts, lambda v: v.confidence)  # Section 12
    return metrics


# ---- Signal performance (Section 6) ----

def signal_performance(verdicts: list[PredictionVerdict],
                       settings: BacktestSettings) -> dict[str, dict]:
    """Per-category comparison: do high scores in a category precede better
    outcomes than low scores? Uses best-window price change as the outcome."""
    categories = ("security", "community", "blockchain", "momentum",
                  "narrative", "foundation", "timing")
    report: dict[str, dict] = {}
    for category in categories:
        high, low = [], []
        for v in verdicts:
            score = v.category_scores.get(category)
            if score is None or v.best_change_percent is None:
                continue
            if score >= settings.signal_high_score:
                high.append(v.best_change_percent)
            elif score < settings.signal_low_score:
                low.append(v.best_change_percent)
        report[category] = {
            "high_n": len(high),
            "high_avg_change": sum(high) / len(high) if high else None,
            "low_n": len(low),
            "low_avg_change": sum(low) / len(low) if low else None,
            "edge": (sum(high) / len(high) - sum(low) / len(low)
                     if high and low else None),
        }
    return report


# ---- Weight experiments (Section 5, under the Part 31 lock) ----

def weight_experiments(verdicts: list[PredictionVerdict],
                       settings: BacktestSettings) -> dict[str, dict] | None:
    """Re-score every prediction under each variant and measure how well the
    variant separates winners from losers (top-half vs bottom-half mean
    outcome). Returns None below the minimum sample (Section 1). REPORT
    ONLY — the shipped weights stay locked (Part 31); apply a change via
    env overrides and record it with :func:`record_strategy_change`.
    """
    usable = [v for v in verdicts if v.best_change_percent is not None]
    if len(usable) < settings.min_predictions_for_weights:
        return None

    results: dict[str, dict] = {}
    for name, weights in WEIGHT_VARIANTS.items():
        weight_map = {k: getattr(weights, k) for k in (
            "foundation", "security", "community", "blockchain",
            "momentum", "narrative", "timing")}
        scored = []
        for v in usable:
            total = available = 0.0
            for category, weight in weight_map.items():
                value = v.category_scores.get(category)
                if value is not None:
                    total += value * weight
                    available += weight
            if available > 0:
                scored.append((total / available, v.best_change_percent))
        scored.sort(key=lambda pair: pair[0])
        half = len(scored) // 2
        bottom, top = scored[:half], scored[half:]
        top_avg = sum(c for _, c in top) / len(top)
        bottom_avg = sum(c for _, c in bottom) / len(bottom)
        results[name] = {
            "n": len(scored),
            "top_half_avg_change": top_avg,
            "bottom_half_avg_change": bottom_avg,
            "discrimination": top_avg - bottom_avg,  # bigger = ranks winners better
        }
    return results


# ---- Failure & success patterns (Sections 7-8) ----

def failure_success_patterns(verdicts: list[PredictionVerdict],
                             settings: BacktestSettings) -> dict[str, dict[str, int]]:
    """Which signals were confidently high on failures (the signal that
    failed) and on successes (the repeatable pattern)."""
    patterns = {"failure_signals": {}, "success_signals": {}}
    for v in verdicts:
        succeeded = (v.best_change_percent is not None
                     and v.best_change_percent >= settings.success_price_change_percent)
        failed = v.died or (v.worst_change_percent is not None and
                            v.worst_change_percent <= settings.failure_price_change_percent)
        target = ("success_signals" if succeeded
                  else "failure_signals" if failed else None)
        if target is None:
            continue
        for category, score in v.category_scores.items():
            if score is not None and score >= settings.signal_high_score:
                patterns[target][category] = patterns[target].get(category, 0) + 1
    return patterns


# ---- Alert outcome labeling (Part 29 S11 -> Part 24 S10) ----

def label_alert_outcomes(storage, settings: BacktestSettings) -> int:
    """Fill ``alerts.outcome`` from measured score drift: opportunity-type
    alerts want positive drift (useful), risk-type alerts want negative
    drift (correct_warning); anything else is noise. Unmeasured alerts
    stay unlabeled."""
    opportunity_types = {"high_priority_opportunity", "early_opportunity",
                         "momentum", "smart_money_accumulation"}
    labeled = 0
    for row in storage.alerts_with_drift():
        if row["outcome"] is not None or row["drift"] is None:
            continue
        drift, kind = row["drift"], row["alert_type"]
        if kind in opportunity_types:
            outcome = "useful" if drift >= settings.alert_useful_drift_points else "noise"
        else:  # risk-side alerts fired correctly when things then got worse
            outcome = ("correct_warning" if drift <= -settings.alert_useful_drift_points
                       else "noise")
        storage.set_alert_outcome(row["id"], outcome)
        labeled += 1
    return labeled


# ---- Strategy version control (Section 11) ----

def record_strategy_change(storage, *, what: str, why: str, results: str) -> int:
    """Journal a framework change. Section 14: never change rules after
    seeing outcomes without recording what/why/results."""
    content = f"WHAT: {what}\nWHY: {why}\nRESULTS: {results}"
    return storage.add_journal(None, "strategy_change", content)


# ---- Dashboard rendering (Section 13) ----

def render_backtest_report(
    metrics: PerformanceMetrics,
    signals: dict[str, dict],
    experiments: dict[str, dict] | None,
    patterns: dict[str, dict[str, int]],
    *,
    alerts_labeled: int = 0,
    outcomes_recorded: int = 0,
) -> str:
    def pct(value):
        return f"{value:.0f}%" if value is not None else "no data"

    lines = [
        "=" * 68,
        "BACKTESTING & SELF-IMPROVEMENT REPORT (Part 24)",
        "=" * 68,
        f"Predictions graded: {metrics.predictions_graded}    "
        f"new outcomes this run: {outcomes_recorded}    "
        f"alerts labeled: {alerts_labeled}",
        "",
        "PERFORMANCE (Section 4)",
        f"  Accuracy:              {pct(metrics.accuracy_percent)}",
        f"  Opportunity detection: {pct(metrics.opportunity_detection_percent)}",
        f"  False positive rate:   {pct(metrics.false_positive_percent)}",
        f"  Risk detection:        {pct(metrics.risk_detection_percent)}",
    ]

    def render_bucket(title, bucket):
        lines.append("")
        lines.append(title)
        if not bucket:
            lines.append("  no data yet")
        for name, slot in sorted(bucket.items()):
            lines.append(f"  {name:<22} graded {slot['graded']:<4} "
                         f"accuracy {pct(slot['accuracy_percent'])}")

    render_bucket("BY CLASSIFICATION", metrics.by_classification)
    render_bucket("BY MARKET REGIME (Section 9)", metrics.by_regime)
    render_bucket("CONFIDENCE CALIBRATION (Section 12)", metrics.by_confidence)

    lines += ["", "SIGNAL PERFORMANCE (Section 6) — best-window price change"]
    measured = False
    for category, row in signals.items():
        if row["high_n"] == 0 and row["low_n"] == 0:
            continue
        measured = True
        high = (f"{row['high_avg_change']:+.0f}% (n={row['high_n']})"
                if row["high_avg_change"] is not None else f"n={row['high_n']}")
        low = (f"{row['low_avg_change']:+.0f}% (n={row['low_n']})"
               if row["low_avg_change"] is not None else f"n={row['low_n']}")
        edge = f"  edge {row['edge']:+.0f}pts" if row["edge"] is not None else ""
        lines.append(f"  {category:<12} high {high:<20} low {low:<20}{edge}")
    if not measured:
        lines.append("  no measured outcomes with category scores yet")

    lines += ["", "WEIGHT EXPERIMENTS (Section 5 — report only; weights stay "
                  "locked per Part 31)"]
    if experiments is None:
        lines.append("  sample too small — experiments run once enough "
                     "predictions have outcomes (Section 1)")
    else:
        for name, row in experiments.items():
            lines.append(f"  {name:<18} n={row['n']:<4} "
                         f"top-half {row['top_half_avg_change']:+.0f}%  "
                         f"bottom-half {row['bottom_half_avg_change']:+.0f}%  "
                         f"discrimination {row['discrimination']:+.0f}pts")
        lines.append("  to adopt a change: set MEMEINTEL_WEIGHTS_* env overrides "
                     "and record it (strategy journal, Section 11)")

    for title, key in (("FAILURE SIGNALS (Section 7 — high scores on failed tokens)",
                        "failure_signals"),
                       ("SUCCESS PATTERNS (Section 8 — high scores on winners)",
                        "success_signals")):
        lines += ["", title]
        entries = patterns.get(key, {})
        if not entries:
            lines.append("  none observed yet")
        for category, count in sorted(entries.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {category:<12} confidently high on {count} token(s)")

    lines += ["", "=" * 68]
    return "\n".join(lines)
