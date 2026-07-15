"""Tests for the scanner stall watchdog (operator request, 2026-07-15).

The operator's hard condition: the watchdog must NEVER interfere with
scanning. These tests pin both halves — it alerts correctly when cycles
stall, and every failure mode inside it degrades to a log line.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.config.settings import WorkflowSettings
from meme_intelligence.workflow.watchdog import ScannerWatchdog

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

WD_SETTINGS = WorkflowSettings(
    watchdog_stall_seconds=900.0,
    watchdog_check_seconds=60.0,
    watchdog_realert_seconds=3600.0,
)


class Clock:
    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class RecordingNotify:
    def __init__(self, result=True, exc=None):
        self.messages: list[str] = []
        self.result = result
        self.exc = exc

    async def __call__(self, text: str) -> bool:
        self.messages.append(text)
        if self.exc is not None:
            raise self.exc
        return self.result


def make_watchdog(age_value, notify=None, clock=None):
    """Watchdog with a mutable age source: age_value is a one-item list."""
    clock = clock or Clock()
    wd = ScannerWatchdog(WD_SETTINGS, lambda: age_value[0], notify,
                         now_func=clock)
    return wd, clock


async def test_healthy_scanner_never_alerts():
    notify = RecordingNotify()
    wd, _ = make_watchdog([120.0], notify)
    for _ in range(10):
        await wd._check_once()
    assert notify.messages == []


async def test_before_start_age_none_is_silent():
    notify = RecordingNotify()
    wd, _ = make_watchdog([None], notify)
    await wd._check_once()
    assert notify.messages == []


async def test_stall_sends_one_alert_with_restart_instructions():
    notify = RecordingNotify()
    wd, _ = make_watchdog([1200.0], notify)
    await wd._check_once()
    await wd._check_once()  # still within the re-alert cooldown: no repeat
    assert len(notify.messages) == 1
    assert "WATCHDOG" in notify.messages[0]
    assert "systemctl restart meme-intelligence" in notify.messages[0]
    assert "/status" in notify.messages[0]


async def test_realert_after_cooldown_elapses():
    notify = RecordingNotify()
    wd, clock = make_watchdog([1200.0], notify)
    await wd._check_once()
    clock.advance(3599)
    await wd._check_once()
    assert len(notify.messages) == 1  # cooldown not yet elapsed
    clock.advance(2)
    await wd._check_once()
    assert len(notify.messages) == 2  # re-alerted after the cooldown


async def test_recovery_message_sent_once():
    notify = RecordingNotify()
    age = [1200.0]
    wd, _ = make_watchdog(age, notify)
    await wd._check_once()          # stall alert
    age[0] = 30.0                   # cycles resumed
    await wd._check_once()          # recovery note
    await wd._check_once()          # healthy again: silence
    assert len(notify.messages) == 2
    assert "resumed" in notify.messages[1]


async def test_stall_after_recovery_alerts_again_immediately():
    # Recovery resets the cooldown: a NEW stall is new news, not a repeat.
    notify = RecordingNotify()
    age = [1200.0]
    wd, _ = make_watchdog(age, notify)
    await wd._check_once()
    age[0] = 30.0
    await wd._check_once()
    age[0] = 1200.0
    await wd._check_once()
    assert len(notify.messages) == 3


async def test_notify_failure_never_raises():
    notify = RecordingNotify(exc=RuntimeError("telegram down"))
    wd, _ = make_watchdog([1200.0], notify)
    await wd._check_once()  # must not raise
    assert len(notify.messages) == 1


async def test_log_only_mode_without_notifier():
    wd, _ = make_watchdog([1200.0], notify=None)
    await wd._check_once()  # must not raise
    assert wd._stalled is True


async def test_age_func_failure_is_contained_by_the_loop():
    # A broken age source raises inside _check_once; the loop must swallow
    # it and keep ticking (Rule 7 — a watchdog bug can't hurt the scanner).
    def broken_age():
        raise RuntimeError("boom")

    ticks = []

    async def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) >= 3:
            raise asyncio.CancelledError  # end the loop after 3 ticks

    wd = ScannerWatchdog(WD_SETTINGS, broken_age, None, sleep_func=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await wd._watch_forever()
    assert len(ticks) == 3  # kept checking despite every check failing


async def test_start_stop_lifecycle():
    wd, _ = make_watchdog([30.0], None)
    await wd.start()
    assert wd._task is not None and not wd._task.done()
    await wd.stop()
    assert wd._task is None
    await wd.stop()  # idempotent
