"""Tests for the live rug-in-progress watch (operator request 2026-07-29).

This engine can trigger a REAL SELL of a real position, so the tests are
written around the two ways it can cost the operator money:

* a MISS  — it fails to notice a drain and he loses the position;
* a FALSE — it sells a healthy coin on noise.

The false-positive tests matter at least as much as the detection ones.
"""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.analyzers.rug_watch import (
    EXIT,
    HOLD,
    WARN,
    LiquidityReading,
    assess_rug_in_progress,
)
from meme_intelligence.config.settings import RugWatchSettings
from meme_intelligence.core.errors import ConfigurationError

T0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
SETTINGS = RugWatchSettings()


def readings(*values, route=None, start=T0, step_seconds=15):
    """Build a trajectory from raw liquidity values (oldest first)."""
    out = []
    for i, value in enumerate(values):
        out.append(LiquidityReading(
            at=start + timedelta(seconds=step_seconds * i),
            liquidity_usd=value,
            sell_route_ok=route))
    return out


# ---- Detection: the case that cost the operator $20 ----


def test_a_confirmed_liquidity_collapse_triggers_an_exit():
    """The pool drains hard while a sell route still exists — the one moment
    an automated exit can still save the position."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 48_000.0, 12_000.0, 9_000.0, route=True), SETTINGS)
    assert verdict.action == EXIT
    assert verdict.should_exit
    assert verdict.drop_percent == pytest.approx(82.0)
    assert verdict.confirmations >= SETTINGS.min_confirmations
    assert "liquidity fell" in verdict.reasons[0]


def test_the_peak_is_the_size_the_coin_actually_reached():
    """A coin that grew before draining is measured against its peak, not its
    entry size — otherwise a 10x-then-rug reads as still up on entry."""
    verdict = assess_rug_in_progress(
        readings(10_000.0, 90_000.0, 100_000.0, 20_000.0, 15_000.0, route=True),
        SETTINGS)
    assert verdict.peak_liquidity_usd == 100_000.0
    assert verdict.action == EXIT


def test_exit_needs_the_drop_to_hold_across_readings():
    """A single collapsed reading is a provider tick, not evidence. With
    min_confirmations=2 one bad sample must never liquidate a position."""
    one_bad_tick = assess_rug_in_progress(
        readings(50_000.0, 49_000.0, 5_000.0, route=True), SETTINGS)
    assert one_bad_tick.action == WARN
    assert not one_bad_tick.should_exit
    assert one_bad_tick.confirmations == 1


def test_a_recovered_tick_resets_the_confirmation_count():
    """Down, back up, down again is volatility. Confirmations count only the
    most recent CONSECUTIVE readings, so this must not reach the threshold."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 5_000.0, 48_000.0, 4_000.0, route=True), SETTINGS)
    assert verdict.confirmations == 1
    assert verdict.action == WARN


# ---- False positives: every one of these would sell a healthy position ----


def test_an_unreadable_provider_never_triggers_a_sale():
    """Rule 8, and the exact shape of the outage bug that fabricated -100%
    returns: a failed read is not a drained pool."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 48_000.0, None, None, None, route=True), SETTINGS)
    assert verdict.action == HOLD
    assert verdict.latest_liquidity_usd == 48_000.0


def test_zero_liquidity_readings_are_evidence_but_unknown_ones_are_not():
    """A MEASURED zero is a real drain; an absent value is not."""
    measured_zero = assess_rug_in_progress(
        readings(50_000.0, 0.0, 0.0, route=True), SETTINGS)
    assert measured_zero.action == EXIT

    unknown = assess_rug_in_progress(
        readings(50_000.0, None, None, route=True), SETTINGS)
    assert unknown.action == HOLD


def test_an_ordinary_dip_is_not_a_rug():
    """Memecoins are volatile; a 20% wobble must not sell."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 44_000.0, 41_000.0, 40_000.0, route=True), SETTINGS)
    assert verdict.action == HOLD
    assert verdict.drop_percent == pytest.approx(20.0)


def test_a_sizeable_dip_warns_without_selling():
    """Between the warn and exit floors the operator is told, not acted for."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 33_000.0, 32_000.0, route=True), SETTINGS)
    assert verdict.action == WARN
    assert not verdict.should_exit


def test_too_little_history_never_sells():
    """A verdict built on one reading has no baseline to fall from."""
    verdict = assess_rug_in_progress(
        [LiquidityReading(at=T0, liquidity_usd=1_000.0, sell_route_ok=True)],
        SETTINGS)
    assert verdict.action == HOLD


def test_an_empty_trajectory_is_survivable():
    assert assess_rug_in_progress([], SETTINGS).action == HOLD


def test_a_growing_coin_is_left_alone():
    verdict = assess_rug_in_progress(
        readings(10_000.0, 25_000.0, 60_000.0, route=True), SETTINGS)
    assert verdict.action == HOLD
    assert verdict.drop_percent == pytest.approx(0.0)


# ---- The asymmetry: a vanished route is the END of a rug, not the start ----


def test_a_vanished_sell_route_warns_loudly_but_does_not_auto_sell():
    """Once no path out exists, an auto-sell is futile — there is nothing to
    execute. The operator still needs to hear about it immediately."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 10_000.0, 8_000.0, route=False), SETTINGS)
    assert verdict.action == WARN
    assert not verdict.should_exit
    assert any("sell route has disappeared" in r for r in verdict.reasons)


def test_route_gone_warns_even_before_enough_liquidity_history():
    verdict = assess_rug_in_progress(
        [LiquidityReading(at=T0, liquidity_usd=None, sell_route_ok=False)],
        SETTINGS)
    assert verdict.action == WARN
    assert any("sell route" in r for r in verdict.reasons)


def test_an_unprobed_route_does_not_block_a_justified_exit():
    """route=None means "not probed", which is not evidence the exit is shut —
    a confirmed collapse must still trigger the sale (Rule 8 both ways)."""
    verdict = assess_rug_in_progress(
        readings(50_000.0, 9_000.0, 8_000.0, route=None), SETTINGS)
    assert verdict.action == EXIT


# ---- Configuration is the safety catch ----


def test_auto_sell_cannot_be_armed_without_the_watch():
    with pytest.raises(ConfigurationError, match="arm both or neither"):
        RugWatchSettings(auto_sell=True)


def test_a_single_reading_can_never_be_configured_to_sell():
    with pytest.raises(ConfigurationError, match="at least 2"):
        RugWatchSettings(min_confirmations=1)


def test_warn_floor_cannot_exceed_the_exit_floor():
    with pytest.raises(ConfigurationError, match="must not exceed"):
        RugWatchSettings(warn_drop_percent=80.0, exit_drop_percent=50.0)


def test_the_guard_is_off_by_default():
    """It spends real money without asking — it must never be on by accident."""
    assert RugWatchSettings().enabled is False
    assert RugWatchSettings().auto_sell is False


def test_thresholds_are_configurable_from_the_environment():
    from meme_intelligence.config.settings import Settings

    settings = Settings.from_env(env={
        "MEMEINTEL_RUG_WATCH_ENABLED": "true",
        "MEMEINTEL_RUG_WATCH_EXIT_DROP_PERCENT": "70",
        "MEMEINTEL_RUG_WATCH_MIN_CONFIRMATIONS": "3",
    })
    assert settings.rug_watch.exit_drop_percent == 70.0
    assert settings.rug_watch.min_confirmations == 3

    # A stricter operator setting really does take more evidence to fire.
    strict = settings.rug_watch
    assert assess_rug_in_progress(
        readings(50_000.0, 9_000.0, 8_000.0, route=True), strict).action == WARN
    assert assess_rug_in_progress(
        readings(50_000.0, 9_000.0, 8_000.0, 7_000.0, route=True),
        strict).action == EXIT
