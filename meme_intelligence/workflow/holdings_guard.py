"""Live rug guard for coins the operator already owns (2026-07-29).

The 24/7 scanner re-checks a watchlist token every N cycles — minutes apart.
That cadence is fine for deciding whether to *pitch* a coin and far too slow to
protect one that is *draining*. This module runs its own fast loop over the
handful of coins the operator actually holds, and when the evidence is
unambiguous it exits the position by itself.

Operator decision, 2026-07-29: "auto-sell, then tell me", after losing a
position to a rug. That makes this the only place in the system where the bot
moves real money without a human tap, so the safety model is explicit:

* **Off unless armed twice.** ``rug_watch.enabled`` starts the loop;
  ``rug_watch.auto_sell`` lets it trade. Neither defaults to on, and the
  settings refuse a half-armed configuration.
* **Positive evidence only.** A failed provider read, an unknown liquidity, or
  an unprobed sell route never move the verdict (Rule 8). This is the same
  failure that once wrote fabricated -100% "deaths" into the mind layer; here
  it would sell a healthy position.
* **Confirmation before money moves.** The verdict engine requires the trigger
  to hold across consecutive readings, so a single bad tick cannot liquidate.
* **Once per position.** A coin is sold at most once; the attempt is recorded
  before the trade so a crash mid-sell cannot produce a second one.
* **Never silent.** Every exit, every refusal to exit, and every disarmed
  trigger is logged and pushed to Telegram.

Decision logic lives in :mod:`meme_intelligence.analyzers.rug_watch` and is
pure; this module is only orchestration (Rule 4).
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from typing import Awaitable, Callable

from meme_intelligence.alerts.notification_engine import AlertEvent
from meme_intelligence.analyzers.rug_watch import (
    EXIT,
    WARN,
    LiquidityReading,
    assess_rug_in_progress,
)
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.core.errors import CollectorError, MemeIntelError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.trading.execution import TradeOutcome

# Readings retained per coin. Only the trailing few decide a verdict, but the
# peak is taken across the window, so this bounds memory while still measuring
# a drop against the size the coin actually reached.
_MAX_READINGS = 240

# How many times a provably-unsent auto-sell is re-attempted before the guard
# stops trying and leaves it to the operator. Bounded so a permanently broken
# exit cannot hammer the trading RPC every poll (Rule 11).
_MAX_EXIT_ATTEMPTS = 3


class HoldingsGuard:
    """Fast rug watch over the operator's open positions."""

    def __init__(
        self,
        settings: Settings,
        storage,
        notifier,
        market_service,
        *,
        executor=None,
        jupiter_client=None,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._s = settings.rug_watch
        self._storage = storage
        self._notifier = notifier
        self._market = market_service
        self._executor = executor
        self._jupiter = jupiter_client
        self._now = now_func
        self._sleep = sleep_func
        self._logger = get_logger("workflow.holdings_guard")
        # Polling faster than the HTTP cache TTL re-reads the SAME cached
        # answer, and two identical cached reads would count as two
        # "consecutive confirmations" of one measurement — the single-glitch
        # liquidation the confirmation rule exists to prevent (2026-07-31
        # review, confirmed with a reproduction). Clamp, don't trust config.
        self._poll_seconds = max(self._s.poll_seconds,
                                 settings.http.cache_ttl_seconds)
        if self._poll_seconds > self._s.poll_seconds:
            self._logger.warning(
                "rug_watch poll_seconds %.0fs is below the HTTP cache TTL "
                "%.0fs — polling at %.0fs so consecutive readings stay "
                "independent measurements",
                self._s.poll_seconds, settings.http.cache_ttl_seconds,
                self._poll_seconds)
        # Keyed by POSITION, not by mint: the same coin bought again is a new
        # position and must start with a clean history. Keyed by mint, a
        # re-buy was judged against the PREVIOUS position's peak and the
        # armed guard liquidated the fresh position within two polls
        # (2026-07-31 bug hunt, reproduced). State for positions that are no
        # longer held is dropped each pass.
        self._readings: dict[str, list[LiquidityReading]] = {}
        # The pool each mint is measured against. Reading whichever pool
        # happens to be deepest in each response fabricates a collapse when
        # the main pool's liquidity field is missing for one tick (max() then
        # picks a $2k side pool as a MEASURED value) or when provider failover
        # switches to a source that does not carry the deepest pool
        # (2026-07-31 review, confirmed with reproductions). Locking onto one
        # pool address keeps every reading a measurement of the SAME thing;
        # a response without that pool degrades to unknown, never to a number.
        self._tracked_pool: dict[str, str] = {}
        # Addresses already acted on. Recorded BEFORE the sell is attempted so
        # a crash or a restart mid-trade cannot produce a second sale.
        self._exited: set[str] = set()
        # Exit attempts per position, so a sell that provably never reached
        # the network can be retried a bounded number of times.
        self._exit_attempts: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- lifecycle ----

    async def start(self) -> None:
        """Spawn the background watch; safe to call repeatedly."""
        if not self._s.enabled:
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever(), name="holdings-guard")
            self._logger.info(
                "holdings rug guard started (poll %.0fs, exit at -%.0f%% liquidity "
                "confirmed %dx, auto-sell %s)",
                self._poll_seconds, self._s.exit_drop_percent,
                self._s.min_confirmations, "ARMED" if self._s.auto_sell else "off")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    self._task = None
                    raise
            except Exception:  # noqa: BLE001 — shutdown must not hang on the guard
                pass
            self._task = None
            self._logger.info("holdings rug guard stopped")

    def disarm(self, reason: str) -> None:
        """Kill switch: stop selling, keep watching and warning."""
        if self._s.auto_sell:
            self._s = dataclasses.replace(self._s, auto_sell=False)
            self._logger.warning("auto-sell DISARMED: %s", reason)

    @property
    def armed(self) -> bool:
        return bool(self._s.enabled and self._s.auto_sell)

    def status(self) -> dict:
        return {
            "enabled": self._s.enabled,
            "auto_sell": self._s.auto_sell,
            "watching": len(self._readings),
            "exited": len(self._exited),
        }

    # ---- loop ----

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the guard must outlive any error
                self._logger.error("holdings guard cycle failed: %s", exc)
            await self._sleep_unless_stopping(self._poll_seconds)

    async def _sleep_unless_stopping(self, seconds: float) -> None:
        if self._stop.is_set():
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waiter = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)

    async def poll_once(self) -> list[tuple[TokenIdentity, object]]:
        """One pass over every open position. Returns (token, verdict) pairs."""
        try:
            holdings = self._storage.get_holdings(active_only=True)
        except Exception as exc:  # noqa: BLE001 — a read failure is not a rug
            self._logger.warning("could not read holdings: %s", exc)
            return []

        # Drop state for positions that are no longer held. Computed from the
        # FULL holdings list, never the max_positions slice, so a position
        # merely queued behind the cap is not mistaken for a closed one.
        active = {self._position_key(row) for row in holdings if row.get("address")}
        for stale in [k for k in self._readings if k not in active]:
            del self._readings[stale]
            self._tracked_pool.pop(stale, None)
            self._exited.discard(stale)
            self._exit_attempts.pop(stale, None)
            self._logger.info("position closed — cleared rug-watch state for %s", stale)

        results = []
        for row in holdings[: self._s.max_positions]:
            address = row.get("address")
            if not address:
                continue
            token = TokenIdentity(chain=row.get("chain") or "solana", address=address,
                                  symbol=row.get("symbol"))
            try:
                verdict = await self._check_one(token, self._position_key(row))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one coin never stops the rest
                self._logger.error("rug watch failed for %s: %s", address, exc)
                continue
            if verdict is not None:
                results.append((token, verdict))
        if len(holdings) > self._s.max_positions:
            self._logger.warning(
                "%d open positions exceeds max_positions=%d — %d not watched this pass",
                len(holdings), self._s.max_positions,
                len(holdings) - self._s.max_positions)
        return results

    def _position_key(self, row) -> str:
        """Identity of ONE position. ``acquired_at`` changes on every re-buy,
        so a coin sold and bought back is a genuinely new position with a
        fresh history, a fresh tracked pool, and a fresh right to be sold."""
        return f"{(row.get('address') or '').lower()}@{row.get('acquired_at') or ''}"

    async def _check_one(self, token: TokenIdentity, pkey: str):
        reading = await self._read(token, pkey)
        history = self._readings.setdefault(pkey, [])
        history.append(reading)
        if len(history) > _MAX_READINGS:
            del history[: len(history) - _MAX_READINGS]

        verdict = assess_rug_in_progress(history, self._s)
        if verdict.action == EXIT:
            await self._on_exit(token, verdict, pkey)
        elif verdict.action == WARN:
            await self._on_warn(token, verdict)
        return verdict

    async def _read(self, token: TokenIdentity, pkey: str) -> LiquidityReading:
        """One observation. Every failure degrades to "unknown", never to zero."""
        liquidity = None
        try:
            pairs = await self._market.get_token_pairs_confirmed(
                token.address, chain=token.chain)
            if pairs:
                liquidity = self._same_pool_liquidity(pkey, pairs)
            else:
                # Every provider agrees there is no tradable pair left. That IS
                # a measured zero, not a failed read.
                liquidity = 0.0
        except (CollectorError, MemeIntelError) as exc:
            self._logger.info("liquidity unreadable for %s (no verdict from this "
                              "reading): %s", token.address, exc)
        except Exception as exc:  # noqa: BLE001 — unknown beats a wrong number
            self._logger.warning("unexpected liquidity read failure for %s: %s",
                                 token.address, exc)

        sell_route = None
        if self._s.probe_sell_route and self._jupiter is not None:
            probe = self._settings.liquidity_probe
            try:
                result = await self._jupiter.check_round_trip_liquidity(
                    token.address, probe_sol_amount=probe.probe_sol_amount,
                    slippage_bps=probe.slippage_bps)
                # Only a definitive "the buy worked but the sell has no route"
                # counts. A missing BUY route commonly just means Jupiter has
                # not indexed the pool, which says nothing about the exit.
                if result.live_buy_route_found is True:
                    sell_route = result.live_sell_route_found
            except Exception as exc:  # noqa: BLE001 — a probe failure is not evidence
                self._logger.debug("sell-route probe unavailable for %s: %s",
                                   token.address, exc)
        return LiquidityReading(at=self._now(), liquidity_usd=liquidity,
                                sell_route_ok=sell_route)

    def _same_pool_liquidity(self, pkey: str, pairs) -> float | None:
        """Liquidity of the ONE pool this mint is measured against.

        Locks onto the deepest pool with a KNOWN liquidity on first sight and
        measures that same pool ever after. A response missing the tracked
        pool (provider failover, delisting glitch) or carrying it without a
        readable liquidity figure is UNKNOWN, never a substitute number — the
        drop the engine measures must mean "this pool drained", not "a
        different/smaller pool was measured this tick" (2026-07-31 review).
        A genuinely drained-to-nothing pool still reads as a measured zero
        via the confirmed-empty branch in ``_read``.
        """
        import math as _math

        def known(value) -> bool:
            return (value is not None and _math.isfinite(value)
                    and value >= 0.0)

        tracked = self._tracked_pool.get(pkey)
        if tracked is not None:
            match = next((p for p in pairs if p.pair_address == tracked), None)
            if match is None:
                self._logger.info(
                    "tracked pool %s for %s absent from this response "
                    "(provider/pool-set change) — reading unknown", tracked, pkey)
                return None
            return match.liquidity_usd if known(match.liquidity_usd) else None

        candidates = [p for p in pairs if known(p.liquidity_usd)]
        if not candidates:
            return None  # pools exist but none has a readable figure
        best = max(candidates, key=lambda p: p.liquidity_usd)
        self._tracked_pool[pkey] = best.pair_address
        return best.liquidity_usd

    # ---- actions ----

    async def _on_warn(self, token: TokenIdentity, verdict) -> None:
        # A CONFIRMED collapse that stayed a WARN only because the sell route
        # read as gone is not a "watching" situation — it is the worst case:
        # the drain is real and the automated exit is blocked. Telling the
        # operator "will auto-sell if confirmed" here was a false promise
        # (2026-07-31 review). Escalate to the CRITICAL exit type instead;
        # NOT marked exited, so if the route comes back while the drain still
        # confirms, the real auto-sell still fires. The notifier's cooldown
        # (keyed type+priority) keeps this from spamming every poll.
        if (verdict.sell_route_ok is False
                and verdict.confirmations >= self._s.min_confirmations):
            await self._alert(
                token, AlertPriority.CRITICAL, "rug_watch_blocked",
                f"RUG IN PROGRESS: {token.symbol or token.address[:8]} "
                f"— sell route gone",
                verdict.reasons + (
                    "Confirmed drain but NO sell route found — the automated "
                    "exit is blocked. Try /dump NOW anyway.",))
            return
        await self._alert(
            token, AlertPriority.HIGH, "rug_watch_warning",
            f"Position warning: {token.symbol or token.address[:8]}",
            verdict.reasons + (
                "Watching. Tap /dump to exit now." if not self.armed else
                "Watching — will auto-sell if this becomes a confirmed drain.",))

    async def _on_exit(self, token: TokenIdentity, verdict, pkey: str) -> None:
        if pkey in self._exited:
            return
        label = token.symbol or token.address[:8]
        if not self._s.auto_sell:
            self._exited.add(pkey)
            self._logger.warning(
                "rug confirmed for %s (%s) but auto-sell is OFF — alerting only",
                token.address, verdict.summary())
            await self._alert(
                token, AlertPriority.CRITICAL, "rug_watch_exit",
                f"RUG IN PROGRESS: {label}",
                verdict.reasons + ("Auto-sell is OFF — /dump NOW to exit.",))
            return
        # ``live`` is the executors' real capability flag (execution.py —
        # LiveExecutor.live=True, DryRunExecutor.live=False). The original
        # 2026-07-29 build checked a nonexistent ``enabled`` attribute here,
        # so a fully-armed guard would ALWAYS have taken this branch and never
        # sold; its tests passed only because the fake defined ``enabled``
        # (2026-07-31 port review finding). A dry-run executor lands here too:
        # claiming "AUTO-SOLD" on a simulated trade would be a lie.
        if self._executor is None or not getattr(self._executor, "live", False):
            self._exited.add(pkey)
            self._logger.error(
                "rug confirmed for %s but no trading executor is available", token.address)
            await self._alert(
                token, AlertPriority.CRITICAL, "rug_watch_exit",
                f"RUG IN PROGRESS: {label}",
                verdict.reasons + ("Trading is not configured — /dump NOW.",))
            return

        # Record the attempt BEFORE trading: if this process dies mid-sell, the
        # restart must not fire a second one. Cleared again ONLY when the
        # executor proves nothing reached the network (below).
        self._exited.add(pkey)
        attempts = self._exit_attempts.get(pkey, 0) + 1
        self._exit_attempts[pkey] = attempts
        self._logger.warning("AUTO-SELLING %s — %s", token.address, verdict.summary())
        try:
            outcome = await self._sell(token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — report, never crash the guard
            self._logger.error("auto-sell FAILED for %s: %s", token.address, exc)
            await self._alert(
                token, AlertPriority.CRITICAL, "rug_watch_exit",
                f"AUTO-SELL FAILED: {label} — nothing was sold",
                verdict.reasons + (f"auto-sell failed: {exc} — /dump manually NOW.",))
            return

        # The executor RETURNS failures rather than raising, so the outcome
        # must be read: reporting a refused sell as "AUTO-SOLD" told the
        # operator he was out while the position drained to zero, and the
        # position was never retried (2026-07-31 bug hunt).
        if outcome.sold is True:
            title = f"AUTO-SOLD {label} — rug in progress"
        elif outcome.sold is None:
            title = f"AUTO-SELL SUBMITTED: {label} — confirm on Solscan"
        else:
            retriable = outcome.retry_safe and attempts < _MAX_EXIT_ATTEMPTS
            if retriable:
                # Nothing reached the network, so re-attempting is the same
                # exit re-quoted, not a second sale. Let the next poll try.
                self._exited.discard(pkey)
                title = (f"AUTO-SELL FAILED: {label} — nothing sold, retrying "
                         f"(attempt {attempts}/{_MAX_EXIT_ATTEMPTS})")
            else:
                title = f"AUTO-SELL FAILED: {label} — nothing was sold"
        reasons = verdict.reasons + (outcome.message,)
        if outcome.sold is False:
            reasons += ("/dump NOW — the automated exit did not sell.",)
        await self._alert(token, AlertPriority.CRITICAL, "rug_watch_exit",
                          title, reasons)

    async def _sell(self, token: TokenIdentity):
        """Dump the position, preferring the executor's structured outcome.

        Falls back to the prose-only API for duck-typed executors that predate
        ``sell_all_outcome``; that path cannot tell success from failure, so it
        is reported as UNKNOWN rather than as a sale (Rule 8, Rule 18)."""
        structured = getattr(self._executor, "sell_all_outcome", None)
        if structured is not None:
            return await structured(token.address, token.chain)
        message = await self._executor.execute_sell_all(token.address, token.chain)
        return TradeOutcome(message, sold=None, retry_safe=False)

    async def _alert(self, token: TokenIdentity, priority: AlertPriority,
                     alert_type: str, title: str, reasons: tuple[str, ...]) -> None:
        event = AlertEvent(
            priority=priority, alert_type=alert_type, token=token, title=title,
            reasons=tuple(reasons),
            why_it_matters=("This is a coin you own. Liquidity leaving the pool is "
                            "how a rug takes your money."),
            detected_at=self._now())
        try:
            await self._notifier.dispatch([event])
        except Exception as exc:  # noqa: BLE001 — a failed alert must not stop the guard
            self._logger.error("could not deliver rug-watch alert for %s: %s",
                               token.address, exc)
