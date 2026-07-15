"""Scanner stall watchdog (operator request, 2026-07-15).

After the OOM crash-loop incident the operator asked for a way to know —
without SSH — when the bot stops making progress. This watchdog runs as an
ISOLATED background task beside the scan loop: it periodically reads how
long ago the last scan cycle completed and, past a configured stall
threshold, sends one Telegram message (re-alerting only after a cooldown),
plus a recovery message when cycles resume.

Hard isolation guarantees (the operator's explicit condition — "do not let
it interfere with the scanning"):

* The scan loop is never touched: the watchdog only READS a timestamp the
  loop already maintains. No locks, no shared mutable state, no work added
  to any cycle.
* Every check iteration is wrapped in a catch-all — a watchdog bug (or a
  Telegram outage) logs a warning and waits for the next tick; it can never
  raise into, slow down, or stop the scanner (Rule 7).
* A missing/failed notifier degrades to log-only operation (Rule 9).

Known limitation (by design, documented for the operator): an in-process
watchdog dies with the process, so it cannot report a hard crash/OOM kill —
systemd's Restart= handles those. What it catches is the sneakier failure:
a process that is ALIVE but silently stuck (hung provider call, starved
event loop, wedged cycle).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from meme_intelligence.config.settings import WorkflowSettings
from meme_intelligence.core.logging_setup import get_logger


class ScannerWatchdog:
    """Alerts the operator when scan cycles stop completing.

    ``age_func`` returns seconds since the last completed cycle (or since
    scanner start when no cycle has completed yet; ``None`` before start).
    ``notify`` is an async callable delivering plain text to the operator
    (the Telegram listener's ``send_text``); ``None`` = log-only mode.
    """

    def __init__(
        self,
        settings: WorkflowSettings,
        age_func: Callable[[], float | None],
        notify: Callable[[str], Awaitable[bool]] | None = None,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._age = age_func
        self._notify = notify
        self._now = now_func
        self._sleep = sleep_func
        self._logger = get_logger("workflow.watchdog")
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_alert_at: datetime | None = None
        self._stalled = False  # currently in a reported-stall state

    # ---- Lifecycle (mirrors TelegramCommandListener / PumpPortalClient) ----

    async def start(self) -> None:
        """Spawn the background check task; safe to call repeatedly."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._watch_forever(),
                                             name="scanner-watchdog")
            self._logger.info(
                "scanner watchdog started: stall threshold %.0fs, check every %.0fs, "
                "re-alert after %.0fs, notify=%s",
                self._settings.watchdog_stall_seconds,
                self._settings.watchdog_check_seconds,
                self._settings.watchdog_realert_seconds,
                "telegram" if self._notify is not None else "log-only",
            )

    async def stop(self) -> None:
        """Cancel the check task gracefully; safe to call repeatedly."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected — we cancelled it. If the task RUNNING stop() is
                # itself being cancelled, propagate (shutdown-hang guard,
                # same pattern as TelegramCommandListener.stop()).
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    self._task = None
                    raise
            except Exception:  # noqa: BLE001 — watchdog errors must not block shutdown
                pass
            self._task = None
            self._logger.info("scanner watchdog stopped")

    # ---- Check loop (error-isolated, Rule 7) ----

    async def _watch_forever(self) -> None:
        while not self._stopping:
            await self._sleep(self._settings.watchdog_check_seconds)
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the watchdog must never die noisily
                self._logger.warning("watchdog check failed (next tick continues): %s", exc)

    async def _check_once(self) -> None:
        age = self._age()
        if age is None:
            return  # scanner not started yet: nothing to judge
        if age < self._settings.watchdog_stall_seconds:
            if self._stalled:
                # Cycles resumed after a reported stall: say so once, so the
                # operator knows a restart is no longer needed.
                self._stalled = False
                self._last_alert_at = None
                await self._deliver(
                    "✅ WATCHDOG: scanning resumed — last cycle completed "
                    f"{age:.0f}s ago. No action needed.")
            return
        # Stalled. Alert once, then hold until the re-alert cooldown elapses.
        if self._last_alert_at is not None:
            since_alert = (self._now() - self._last_alert_at).total_seconds()
            if since_alert < self._settings.watchdog_realert_seconds:
                return
        self._stalled = True
        self._last_alert_at = self._now()
        minutes = age / 60.0
        await self._deliver(
            f"⚠️ WATCHDOG: no scan cycle has completed in {minutes:.0f} minutes "
            f"(threshold {self._settings.watchdog_stall_seconds / 60.0:.0f}m). "
            "The process is alive but scanning looks stuck.\n\n"
            "Try /status. If it doesn't answer, SSH and run:\n"
            "sudo systemctl restart meme-intelligence")

    async def _deliver(self, text: str) -> None:
        """Send via Telegram when wired; always log (Rule 13). A delivery
        failure is logged and dropped — the next due alert retries naturally."""
        self._logger.warning("watchdog: %s", text.replace("\n", " "))
        if self._notify is None:
            return
        try:
            delivered = await self._notify(text)
            if not delivered:
                self._logger.warning("watchdog alert not delivered (telegram said no)")
        except Exception as exc:  # noqa: BLE001 — a dead messenger must not kill the watchdog
            self._logger.warning("watchdog alert delivery failed: %s", exc)
