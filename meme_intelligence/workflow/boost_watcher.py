"""DexScreener boost radar (Project 5).

A standalone background watcher that polls DexScreener's free, keyless boost
feed and DMs the operator the FIRST time any token crosses a boost threshold
(default 100). A boost is PAID promotion, not organic traction or a safety
signal — this is a "what is being pumped for visibility right now" heads-up,
never a buy signal, and the emitted alert says so.

Independent of the scan cycle by design: its own poll loop, its own alert,
off by default. Mirrors ``PumpPortalClient``'s background-task lifecycle
(start/close plus a fully error-isolated loop) so a DexScreener hiccup can
never disturb the scanner, storage, or the trading path.

Latency note: DexScreener caches the boost feed ~30s server-side, so a
crossing surfaces within roughly half a minute — not instantly. Polling
faster than that only re-reads the same cached body.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from meme_intelligence.alerts.notification_engine import AlertEvent
from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.workflow.controller import _BoundedKeySet

# Poll-loop error backoff (Rule 7): starts small, doubles on consecutive
# failures, resets on the first healthy poll.
_POLL_BACKOFF_START = 5.0
_POLL_BACKOFF_MAX = 300.0


class BoostWatcher:
    """Polls the DexScreener boost feed; alerts once per token per crossing."""

    def __init__(
        self,
        dex,           # DexScreenerClient: get_boosts() -> list[TokenBoost]
        notifier,      # NotificationEngine: dispatch(list[AlertEvent])
        settings,      # BoostWatcherSettings
        *,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._dex = dex
        self._notifier = notifier
        self._s = settings
        self._sleep = sleep_func
        self._now = now_func
        self._alerted = _BoundedKeySet(settings.max_seen_keys)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._primed = False
        self._logger = get_logger("workflow.boost_watcher")

    async def start(self) -> None:
        """Spawn the background poll loop; safe to call repeatedly."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._poll_forever(), name="boost-watcher")
            self._logger.info(
                "boost watcher started (threshold=%.0f, chain=%s, every %.0fs)",
                self._s.threshold, self._s.chain_filter or "all",
                self._s.poll_interval_seconds)

    async def close(self) -> None:
        """Stop the poll loop (mirrors PumpPortalClient.close)."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected: we cancelled our own task. But if the task running
                # close() is itself being cancelled by an outer shutdown, that
                # must propagate rather than be swallowed here.
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    self._task = None
                    raise
            except Exception:  # noqa: BLE001 — watcher errors must not block shutdown
                pass
            self._task = None

    async def __aenter__(self) -> "BoostWatcher":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def _poll_forever(self) -> None:
        delay = _POLL_BACKOFF_START
        while not self._stopping:
            try:
                await self.poll_once()
                delay = _POLL_BACKOFF_START  # a healthy poll resets the backoff
                await self._sleep(self._s.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the watcher must outlive any error
                self._logger.warning("boost watcher poll error: %s: %s",
                                     type(exc).__name__, exc)
                await self._sleep(delay)
                delay = min(_POLL_BACKOFF_MAX, delay * 2)

    async def poll_once(self) -> list:
        """One poll: fetch the feed, alert on NEW threshold crossings.

        The FIRST poll only records the baseline — every token already over the
        threshold is marked seen WITHOUT alerting — so enabling the watcher
        never dumps the whole current boosted set onto the phone at once; only
        crossings from here on notify. Because ``totalAmount`` is cumulative and
        boosts arrive in discrete packs, a token seen at/above the threshold in
        any poll is a real crossing; the bounded seen-set fires it exactly once.
        Returns the boosts alerted on (for tests/observability)."""
        boosts = await self._dex.get_boosts()
        fresh = []
        for boost in boosts:
            if boost.total_amount is None or boost.total_amount < self._s.threshold:
                continue
            if self._s.chain_filter and boost.chain != self._s.chain_filter:
                continue
            key = (boost.chain, boost.token_address.lower())
            if key in self._alerted:
                continue
            self._alerted.add(key)   # mark seen whether we prime or emit
            fresh.append(boost)
        if not self._primed:
            self._primed = True      # baseline pass: record current set, do not alert
            return []
        for boost in fresh:
            await self._emit(boost)
        return fresh

    async def _emit(self, boost) -> None:
        token = TokenIdentity(chain=boost.chain, address=boost.token_address)
        short = (boost.token_address[:4] + "…" + boost.token_address[-4:]
                 if len(boost.token_address) > 8 else boost.token_address)
        event = AlertEvent(
            priority=AlertPriority.MEDIUM,
            alert_type="boost",
            token=token,
            title=f"DexScreener boost {boost.total_amount:.0f} — {short}",
            reasons=(
                "just crossed the boost threshold — someone is paying to promote it",
                "a boost is marketing spend, NOT organic hype or a safety signal "
                "(rugs buy boosts too)",
            ),
            why_it_matters="Early heads-up that a coin is being actively promoted — "
                           "attention, not endorsement. Verify before acting.",
            monitoring=(f"/check {boost.token_address} for safety first",),
        )
        await self._notifier.dispatch([event])
        self._logger.info("boost alert: %s total=%.0f chain=%s",
                         boost.token_address, boost.total_amount, boost.chain)
