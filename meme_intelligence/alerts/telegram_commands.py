"""Two-way Telegram control (Project 2, ROADMAP item 2).

The bot already SENDS alerts (:mod:`meme_intelligence.alerts.sinks`); this
module makes it LISTEN. :class:`TelegramCommandListener` long-polls the
Telegram Bot API's ``getUpdates`` endpoint from its own asyncio task inside
the monitor and answers operator commands (/status, /why, /check, /holding,
/watchlist, /mind, /mute, ...), records 👍/👎 feedback pressed on alert
messages, and routes the (dry-run-only) buy button.

Design invariants:

* **Raw Bot API, no third-party library** — built on
  :class:`~meme_intelligence.collectors.base.BaseCollector` so rate
  limiting, retries, timeouts, and token redaction come for free, and the
  dependency count stays at zero (matches the existing sink).
* **Single getUpdates consumer** — Telegram allows exactly one long-poll
  consumer per bot token; this listener is it (the alert sink only ever
  calls ``sendMessage``).
* **Total error isolation (Rule 7)** — the polling loop catches ALL
  exceptions, logs, backs off exponentially (2s -> 60s, reset on success),
  and never propagates. A Telegram outage or a bug in a command handler
  must never touch the scan loop.
* **Security (Rule 16 / D3)** — only updates from the configured chat id
  are processed; everything else is logged (chat id only, never text) and
  ignored silently. All inbound text is untrusted: token addresses must
  pass a strict charset check before any use, nothing is ever echoed back
  raw, replies are plain text (no parse_mode) with web previews disabled,
  and the bot token never reaches a log line (``redact=``).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from meme_intelligence.alerts.sinks import _sanitize_identity, copy_keyboard
from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import TokenIdentity

# Polling-loop backoff (D2): starts small, doubles on consecutive failures,
# resets on the first successful poll. The cap is configurable.
_ERROR_BACKOFF_START = 2.0

_REPLY_MAX_CHARS = 4000        # Telegram sendMessage hard limit is 4096
_CALLBACK_ACK_MAX_CHARS = 190  # answerCallbackQuery text limit is 200

# /check protections (Rule 11): one analysis at a time, and a short
# per-token result cache so button-mashing can't burn API budget.
_CHECK_CACHE_TTL_SECONDS = 60.0
_CHECK_CACHE_MAX_ENTRIES = 64

# Strict address charsets (D3): anything else is rejected before ANY use.
_BASE58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
                             "abcdefghijkmnopqrstuvwxyz")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

_FEEDBACK_ALERT_WINDOW = timedelta(days=7)  # link feedback to a recent alert


def classify_address(candidate: object) -> str | None:
    """Strict token-address check: ``"solana"`` for base58 32-44 chars,
    ``"evm"`` for 0x + 40 hex, ``None`` for anything else (rejected)."""
    if not isinstance(candidate, str):
        return None
    if (len(candidate) == 42 and candidate[:2].lower() == "0x"
            and all(ch in _HEX_DIGITS for ch in candidate[2:])):
        return "evm"
    if 32 <= len(candidate) <= 44 and all(ch in _BASE58_ALPHABET for ch in candidate):
        return "solana"
    return None


_BAD_ADDRESS_REPLY = ("That does not look like a token address (expected Solana "
                      "base58, 32-44 chars, or 0x + 40 hex). Nothing was done.")

_HELP_TEXT = "\n".join([
    "Commands:",
    "/status - scanner health + last cycle stats",
    "/why <address> - why a coin alerted or was vetoed",
    "/check <address> [chain] - analyze a token right now",
    "/holding <address> - mark a coin you actually bought",
    "/unhold <address> - release a holding",
    "/holdings - list active holdings (alias /positions)",
    "/watchlist - top tracked coins by tier",
    "/mind - learning-layer report card + feedback tallies",
    "/mute <address> - silence ALL alerts for a token",
    "/unmute <address> - restore alerts for a token",
    "/help - this list",
])


@dataclass
class CommandContext:
    """Narrow injection surface for the command handlers (Rule 4).

    ``status_provider`` returns the scanner's cheap status dict;
    ``check_runner`` is an async ``(address, chain) -> PipelineResult|None``
    closure over the scanner's shared pipeline; ``learning_service`` and
    ``executor`` are optional layers (None = honestly off).
    """

    storage: Any
    settings: Any
    status_provider: Callable[[], dict]
    check_runner: Callable[..., Awaitable[Any]]
    learning_service: Any = None
    executor: Any = None


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "unknown"


def _opt(value, spec: str = ".2f") -> str:
    """'n/a' when there is no data (Rule 8), else formatted."""
    if value is None:
        return "n/a"
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return "n/a"


# Security facts (analyzers.security_monitor.FACT_FIELDS) worth surfacing
# as red flags in /why: destructive/high-danger booleans that are True,
# plus a live sell probe that found no route (Project 1).
_RED_FLAG_FACTS = (
    ("is_honeypot", "honeypot"),
    ("cannot_sell_all", "cannot sell full balance"),
    ("cannot_buy", "buying blocked"),
    ("is_mintable", "mint authority active"),
    ("is_freezable", "freeze authority active"),
)


class TelegramCommandListener(BaseCollector):
    """Long-polling Telegram command listener (Project 2).

    ``start()`` spawns the background poll task (idempotent, mirroring
    ``PumpPortalClient.start()``); ``stop()`` cancels it gracefully. The
    HTTP session is released with the inherited ``close()``.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        context: CommandContext,
        *,
        poll_timeout_seconds: float = 25.0,
        idle_delay_seconds: float = 2.0,
        error_backoff_max_seconds: float = 60.0,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        time_func: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramCommandListener requires bot_token and chat_id")
        kwargs.setdefault("name", "telegram_commands")
        kwargs.setdefault("base_url", "https://api.telegram.org")
        kwargs.setdefault("redact", (bot_token,))
        # The per-request timeout must exceed the server-side long-poll wait,
        # or every quiet getUpdates call would "time out" as a false error.
        kwargs.setdefault("timeout_seconds", poll_timeout_seconds + 10.0)
        super().__init__(**kwargs)
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._ctx = context
        self._poll_timeout = poll_timeout_seconds
        self._idle_delay = idle_delay_seconds
        self._backoff_max = error_backoff_max_seconds
        self._now = now_func
        self._sleep = sleep_func
        self._time = time_func
        self._offset: int | None = None  # getUpdates cursor (in memory)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._check_lock = asyncio.Lock()
        self._check_cache: "OrderedDict[tuple[str, str], tuple[float, str]]" = OrderedDict()
        self._handlers: dict[str, Callable[[list[str]], Awaitable[str | None]]] = {
            "/help": self._cmd_help,
            "/start": self._cmd_help,
            "/status": self._cmd_status,
            "/why": self._cmd_why,
            "/check": self._cmd_check,
            "/holding": self._cmd_holding,
            "/unhold": self._cmd_unhold,
            "/holdings": self._cmd_holdings,
            "/positions": self._cmd_holdings,
            "/watchlist": self._cmd_watchlist,
            "/mind": self._cmd_mind,
            "/mute": self._cmd_mute,
            "/unmute": self._cmd_unmute,
        }

    # ---- Lifecycle (mirrors PumpPortalClient) ----

    async def start(self) -> None:
        """Spawn the background poll task; safe to call repeatedly."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._poll_forever(),
                                             name="telegram-command-listener")
            self._logger.info("telegram command listener started (chat %s)", self._chat_id)

    async def stop(self) -> None:
        """Cancel the poll task gracefully; safe to call repeatedly."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected — we cancelled it. But if the task RUNNING stop()
                # is itself being cancelled, that must propagate (same
                # shutdown-hang guard as PumpPortalClient.close()).
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    self._task = None
                    raise
            except Exception:  # noqa: BLE001 — listener errors must not block shutdown
                pass
            self._task = None
            self._logger.info("telegram command listener stopped")

    # ---- Poll loop (error-isolated, Rule 7) ----

    async def _poll_forever(self) -> None:
        backoff = _ERROR_BACKOFF_START
        while not self._stopping:
            try:
                await self._poll_once()
                backoff = _ERROR_BACKOFF_START  # success resets the backoff
                await self._sleep(self._idle_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — NOTHING may escape this loop
                self._logger.warning(
                    "telegram command poll failed: %s (backing off %.0fs)",
                    self._scrub(str(exc)), backoff)
                await self._sleep(backoff)
                backoff = min(self._backoff_max, backoff * 2.0)

    async def _poll_once(self) -> int:
        """One getUpdates cycle; returns the number of updates received."""
        params = {"timeout": str(int(self._poll_timeout))}
        if self._offset is not None:
            params["offset"] = str(self._offset)
        payload = await self._get_json(f"bot{self._token}/getUpdates", params=params)
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise CollectorError(f"{self.name}: unexpected getUpdates response")
        updates = payload.get("result") or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance past everything in this batch, even updates whose
                # handlers fail — a poison update must not replay forever.
                self._offset = (update_id + 1 if self._offset is None
                                else max(self._offset, update_id + 1))
            try:
                await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad update never stops polling
                self._logger.error("telegram update handling failed: %s",
                                   self._scrub(str(exc)))
        return len(updates)

    # ---- Update routing + auth (D3) ----

    def _authorized_chat(self, update: dict) -> bool:
        """True only when the update's chat id string-equals the configured
        one. Strangers are logged (chat id only — NEVER their text) and
        silently ignored: no reply, no acknowledgement."""
        message = update.get("message") or update.get("edited_message")
        callback = update.get("callback_query")
        chat: dict = {}
        if isinstance(callback, dict):
            chat = (callback.get("message") or {}).get("chat") or {}
        elif isinstance(message, dict):
            chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None and str(chat_id) == self._chat_id:
            return True
        self._logger.info("ignoring telegram update from unauthorized chat %r",
                          chat_id)
        return False

    async def _handle_update(self, update: dict) -> None:
        if not self._authorized_chat(update):
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback)
            return
        message = update.get("message") or update.get("edited_message")
        if isinstance(message, dict):
            await self._handle_message(message)

    async def _handle_message(self, message: dict) -> None:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip().startswith("/"):
            return  # non-command chatter in the operator's chat: ignore
        parts = text.strip().split()
        # '/cmd@BotName arg' is how groups address a specific bot.
        command = parts[0].split("@", 1)[0].lower()
        handler = self._handlers.get(command)
        if handler is None:
            await self._reply("Unknown command.\n" + _HELP_TEXT)
            return
        try:
            reply = await handler(parts[1:])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a handler bug must not kill the loop
            self._logger.error("telegram command %s failed: %s",
                               _sanitize_identity(command, max_len=32),
                               self._scrub(str(exc)))
            reply = ("Command failed — the error is logged on the droplet. "
                     "The scanner is unaffected.")
        # Handlers return either plain text or (text, reply_markup) — the
        # markup carries the one-tap copy-address button (operator request).
        markup = None
        if isinstance(reply, tuple):
            reply, markup = reply
        if reply:
            await self._reply(reply, reply_markup=markup)

    async def _reply(self, text: str, *, reply_markup: dict | None = None) -> bool:
        """Plain-text reply to the operator chat. No parse_mode (nothing the
        text contains can become live markdown), previews off, length-capped.
        ``reply_markup`` carries inline buttons (e.g. copy-address)."""
        json_body: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text[:_REPLY_MAX_CHARS],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            json_body["reply_markup"] = reply_markup
        try:
            payload = await self._get_json(
                f"bot{self._token}/sendMessage", json_body=json_body,
            )
            return bool(isinstance(payload, dict) and payload.get("ok"))
        except CollectorError as exc:
            self._logger.error("telegram command reply failed: %s", exc)
            return False

    # ---- Address helpers ----

    def _validated_address(self, args: list[str], usage: str) -> tuple[str | None, str | None]:
        """(address, error_reply) — exactly one of the two is None."""
        if not args:
            return None, f"Usage: {usage}"
        if classify_address(args[0]) is None:
            return None, _BAD_ADDRESS_REPLY
        return args[0], None

    def _resolve_token(self, address: str) -> TokenIdentity:
        """Known token from storage when possible (correct chain); otherwise
        an identity on the inferred chain (base58 -> solana, 0x -> ethereum)."""
        known = self._ctx.storage.find_token(address)
        if known is not None:
            return known
        kind = classify_address(address)
        chain = "ethereum" if kind == "evm" else "solana"
        return TokenIdentity(chain=chain, address=address)

    # ---- Commands (D4) ----

    async def _cmd_help(self, args: list[str]) -> str:
        return _HELP_TEXT

    async def _cmd_status(self, args: list[str]) -> str:
        try:
            snap = self._ctx.status_provider() or {}
        except Exception as exc:  # noqa: BLE001 — status must degrade, not fail
            self._logger.warning("status provider failed: %s", exc)
            snap = {}
        lines = ["SCANNER STATUS"]
        lines.append(f"uptime {_fmt_duration(snap.get('uptime_seconds'))} | "
                     f"cycles {snap.get('cycles', 0)}")
        last = snap.get("last_cycle")
        if last:
            lines.append(
                f"last cycle: {last.get('pools_seen', 0)} pools, "
                f"{last.get('candidates', 0)} candidates, {last.get('analyzed', 0)} analyzed, "
                f"{last.get('launches_tracked', 0)} launches tracked, "
                f"{last.get('learned', 0)} learned, {last.get('alerts', 0)} alerts")
        else:
            lines.append("last cycle: none completed yet")
        networks = snap.get("networks") or []
        lines.append("networks: " + (", ".join(str(n) for n in networks) or "unknown"))
        layers = snap.get("layers") or {}

        def onoff(key: str) -> str:
            return "ON" if layers.get(key) else "off"

        lines.append(
            f"layers: wallet intel {onoff('wallet_intel')} | AI {onoff('ai')} | "
            f"learning {onoff('learning')} | pump.fun {onoff('pumpfun')} | "
            f"jupiter probe {onoff('jupiter_probe')} | "
            f"buy button {'DRY-RUN' if layers.get('buy_button') else 'off'}")
        db = snap.get("db") or {}
        lines.append(
            f"db: {db.get('tokens', '?')} tokens, {db.get('alerts', '?')} alerts, "
            f"{db.get('watchlist', '?')} watchlist, {db.get('holdings', '?')} holdings")
        return "\n".join(lines)

    async def _cmd_why(self, args: list[str]):
        address, error = self._validated_address(args, "/why <address>")
        if error:
            return error
        storage = self._ctx.storage
        token = storage.find_token(address)
        if token is None:
            return (f"Never analyzed — send /check {address} to analyze it now.")

        symbol = _sanitize_identity(token.symbol or token.address[:8])
        lines = [f"WHY — {symbol} ({token.chain})"]

        alerts = storage.alert_history(token, limit=3)
        if alerts:
            lines.append(f"latest alerts ({len(alerts)}):")
            for row in alerts:
                title = _sanitize_identity(row.get("title") or "", max_len=120)
                lines.append(f"  {str(row.get('created_at', ''))[:16]} "
                             f"[{row.get('priority')}] {row.get('alert_type')}: {title}")
                for reason in (row.get("reasons") or [])[:4]:
                    lines.append(f"    - {_sanitize_identity(str(reason), max_len=160)}")
        else:
            lines.append("no alerts recorded for this token")

        scores = storage.score_history(token, limit=1)
        if scores:
            lines.append(f"latest score: {scores[0]['final_score']:.0f}/100 "
                         f"({scores[0]['classification']})")
        else:
            lines.append("no score snapshots recorded")

        entry = next((e for e in storage.get_watchlist(include_archived=True)
                      if e.token.address.lower() == token.address.lower()
                      and e.token.chain == token.chain), None)
        if entry is not None:
            lines.append(f"watchlist: {entry.tier.value}")

        held = storage.is_holding(token)
        muted = storage.is_muted(token)
        lines.append(f"holding: {'yes' if held else 'no'} | muted: {'yes' if muted else 'no'}")

        facts = storage.latest_security_facts(token)
        flags = []
        if facts:
            for field, label in _RED_FLAG_FACTS:
                if facts.get(field) is True:
                    flags.append(label)
            if facts.get("live_sell_route_found") is False:
                flags.append("live sell route NOT found (Jupiter probe)")
        lines.append("red flags on record: " + (", ".join(flags) if flags else "none"))
        return "\n".join(lines), copy_keyboard(token.address)

    async def _cmd_check(self, args: list[str]):
        address, error = self._validated_address(args, "/check <address> [chain]")
        if error:
            return error
        chain = "solana"
        if len(args) > 1:
            candidate = args[1].strip().lower()
            if not (candidate.isalnum() and 1 <= len(candidate) <= 20):
                return "Invalid chain id. Example: /check <address> solana"
            chain = candidate

        cache_key = (address.lower(), chain)
        cached = self._check_cache.get(cache_key)
        if cached is not None and cached[0] > self._time():
            return cached[1] + "\n(cached result, <60s old)", copy_keyboard(address)

        # One analysis at a time (Rule 11): analyze_pair fans out to several
        # providers; a second concurrent /check waits its turn... or rather,
        # is asked to retry — simpler and honest on a phone.
        if self._check_lock.locked():
            return "A /check is already running — try again in a few seconds."
        async with self._check_lock:
            result = await self._ctx.check_runner(address, chain)
        if result is None:
            return (f"No data: token not found on {chain} (or security data is "
                    "not indexed yet). Try again in a few minutes or pass an "
                    "explicit chain: /check <address> <chain>")
        card = self._format_check_card(result)
        mind = self._mind_line(result)
        if mind:
            card += "\n" + mind
        self._check_cache[cache_key] = (self._time() + _CHECK_CACHE_TTL_SECONDS, card)
        while len(self._check_cache) > _CHECK_CACHE_MAX_ENTRIES:
            self._check_cache.popitem(last=False)
        return card, copy_keyboard(result.pair.base_token.address)

    @staticmethod
    def _format_check_card(result) -> str:
        """Phone-sized card mirroring the `quick` CLI output (Part 16 S6)."""
        pair = result.pair
        token = pair.base_token
        symbol = _sanitize_identity(token.symbol or token.address[:8])
        security, master = result.security, result.master
        price = f"${pair.price_usd:.8f}" if pair.price_usd is not None else "unknown"
        lines = [
            f"CHECK — {symbol} ({token.chain})",
            f"price {price} | liq {_money(pair.liquidity_usd)} | "
            f"mcap {_money(pair.market_cap)}",
            f"security {security.overall_score:.0f}/100 [{security.band}] "
            f"tier={security.tier.value}",
        ]
        # Red flags are NEVER skipped for speed (Part 16, Section 6).
        if security.destructive_findings:
            lines += [f"RED FLAG: {f.message}" for f in security.destructive_findings[:4]]
        else:
            lines.append(f"red flags: none confirmed "
                         f"({len(security.unknown_fields)} fact(s) unverified)")
        lines.append(f"overall {master.final_score:.0f}/100 — "
                     f"{master.classification.value} (coverage {master.coverage:.0%})")
        momentum = result.momentum
        if momentum is not None:
            lines.append(f"momentum {momentum.overall_score:.0f}/100 "
                         f"zone={momentum.entry_zone.value}")
        lines.append("(quick screen — run a full report before acting)")
        return "\n".join(lines)

    def _mind_line(self, result) -> str | None:
        """Advisory mind-layer verdict line, or None when the layer is off /
        errors (never blocks the /check answer — the line is a bonus)."""
        service = self._ctx.learning_service
        if service is None:
            return None
        try:
            pair = result.pair
            profile = getattr(result, "security_profile", None)
            age_seconds = 0.0
            if getattr(pair, "pair_created_at", None) is not None:
                age_seconds = max(0.0, (self._now() - pair.pair_created_at).total_seconds())
            snapshot = {
                "age_seconds": age_seconds,
                "price_usd": pair.price_usd,
                "liquidity_usd": pair.liquidity_usd,
                "market_cap_usd": pair.market_cap,
                "volume_1h_usd": pair.volume_1h,
                "holder_count": profile.holder_count if profile is not None else None,
                "buys": pair.buys_1h,
                "sells": pair.sells_1h,
                "top10_holder_percent":
                    profile.top10_holder_percent if profile is not None else None,
            }
            verdict = service.evaluate_coin(
                pair.base_token.address, pair.base_token.chain, [snapshot],
                security=profile,
                creator=profile.creator_address if profile is not None else None)
            p_rug = float(verdict.get("final_probabilities", {}).get("rug", 0.0))
            confidence = float(verdict.get("model_confidence", 0.0))
            return (f"mind (advisory): p(rug) {p_rug:.0%}, confidence {confidence:.0%}, "
                    f"n={verdict.get('sample_size', 0)}")
        except Exception as exc:  # noqa: BLE001 — advisory only
            self._logger.warning("mind line unavailable for /check: %s", exc)
            return None

    async def _cmd_holding(self, args: list[str]) -> str:
        address, error = self._validated_address(args, "/holding <address>")
        if error:
            return error
        storage = self._ctx.storage
        token = self._resolve_token(address)
        added = storage.set_holding(token)
        count = len(storage.get_holdings(active_only=True))
        state = "Holding recorded" if added else "Already marked as held"
        return (f"{state}: {_sanitize_identity(token.symbol or token.address[:8])} "
                f"({token.chain}). Active holdings: {count}. Protective alerts stay "
                "at full priority for held coins.")

    async def _cmd_unhold(self, args: list[str]) -> str:
        address, error = self._validated_address(args, "/unhold <address>")
        if error:
            return error
        storage = self._ctx.storage
        token = self._resolve_token(address)
        released = storage.release_holding(token)
        count = len(storage.get_holdings(active_only=True))
        state = "Holding released" if released else "No active holding found"
        return f"{state}. Active holdings: {count}."

    async def _cmd_holdings(self, args: list[str]) -> str:
        holdings = self._ctx.storage.get_holdings(active_only=True)
        if not holdings:
            return "No active holdings. Mark one with /holding <address>."
        lines = [f"HOLDINGS ({len(holdings)} active)"]
        for row in holdings:
            symbol = _sanitize_identity(row.get("symbol") or row.get("address", "?")[:8])
            score = (f"{row['last_score']:.0f}" if row.get("last_score") is not None
                     else "?")
            lines.append(f"  {symbol} ({row.get('chain')}) since "
                         f"{str(row.get('acquired_at', ''))[:10]} — last score {score}")
        return "\n".join(lines)

    async def _cmd_watchlist(self, args: list[str]) -> str:
        entries = self._ctx.storage.get_watchlist()
        if not entries:
            return "Watchlist is empty."
        lines = [f"WATCHLIST — top {min(8, len(entries))} of {len(entries)}"]
        for entry in entries[:8]:
            symbol = _sanitize_identity(entry.token.symbol or entry.token.address[:8])
            score = f"{entry.last_score:.0f}" if entry.last_score is not None else "?"
            lines.append(f"  [{entry.tier.value}] {symbol} — score {score}")
        return "\n".join(lines)

    async def _cmd_mind(self, args: list[str]) -> str:
        service = self._ctx.learning_service
        feedback = {"up": 0, "down": 0}
        try:
            feedback = self._ctx.storage.feedback_summary()
        except Exception as exc:  # noqa: BLE001 — tallies are a bonus line
            self._logger.warning("feedback summary unavailable: %s", exc)
        if service is None:
            return ("Mind layer is off (MEMEINTEL_LEARNING_ENABLE_IN_MONITOR).\n"
                    f"Operator feedback: {feedback['up']} up / {feedback['down']} down "
                    "(advisory only)")
        metrics = service.get_learning_metrics(persist=False)
        rug = metrics.get("rug") or {}
        directional = metrics.get("directional") or {}
        lines = [
            "MIND LAYER REPORT CARD",
            f"memory: {metrics.get('analog_memory_size', 0)} coins | "
            f"resolved: {metrics.get('resolved_coins_total', metrics.get('resolved_count', 0))} | "
            f"graded: {metrics.get('resolved_count', 0)}",
            f"hit rate: {_opt(directional.get('hit_rate'))} "
            f"(n={directional.get('samples', 0)})",
            f"rug precision/recall: {_opt(rug.get('precision'))} / {_opt(rug.get('recall'))}",
        ]
        if metrics.get("brier_score") is not None:
            lines.append(f"brier score: {_opt(metrics.get('brier_score'))}")
        ensemble = metrics.get("ensemble_accuracy") or {}
        weights = [f"{source} {_opt(report.get('accuracy'))} (n={report.get('samples', 0)})"
                   for source, report in ensemble.items()
                   if isinstance(report, dict) and source != "ensemble_final"]
        if weights:
            lines.append("ensemble: " + " | ".join(weights))
        lines.append(f"classifier ready: {metrics.get('classifier_ready', False)}")
        lines.append(f"operator feedback: {feedback['up']} up / {feedback['down']} down "
                     "(advisory only — never a training label)")
        return "\n".join(lines)

    async def _cmd_mute(self, args: list[str]) -> str:
        address, error = self._validated_address(args, "/mute <address>")
        if error:
            return error
        token = self._resolve_token(address)
        newly = self._ctx.storage.mute_token(token)
        state = "Muted" if newly else "Already muted"
        return (f"{state}: {_sanitize_identity(token.symbol or token.address[:8])} "
                f"({token.chain}). ALL alert delivery for this token is suppressed; "
                "analysis continues. Undo with /unmute.")

    async def _cmd_unmute(self, args: list[str]) -> str:
        address, error = self._validated_address(args, "/unmute <address>")
        if error:
            return error
        token = self._resolve_token(address)
        was_muted = self._ctx.storage.unmute_token(token)
        return ("Unmuted — alerts restored." if was_muted
                else "That token was not muted.")

    # ---- Callback queries: 👍/👎 feedback + buy button (D5/D7) ----

    async def _handle_callback(self, callback: dict) -> None:
        callback_id = callback.get("id")
        data = callback.get("data")
        try:
            ack = await self._dispatch_callback(data if isinstance(data, str) else "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a button bug must not kill the loop
            self._logger.error("telegram callback handling failed: %s",
                               self._scrub(str(exc)))
            ack = "Failed — logged on the droplet."
        # ALWAYS answer (stops the client-side spinner), even on failure.
        await self._answer_callback(callback_id, ack)

    async def _dispatch_callback(self, data: str) -> str:
        if data.startswith("fb:"):
            return await self._handle_feedback(data)
        if data.startswith("buy:"):
            return await self._handle_buy(data)
        return "Unknown button."

    async def _handle_feedback(self, data: str) -> str:
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] not in ("0", "1") or classify_address(parts[2]) is None:
            return "Invalid feedback payload."
        verdict = "up" if parts[1] == "1" else "down"
        storage = self._ctx.storage
        token = self._resolve_token(parts[2])
        # Link the feedback to the most recent alert for this token when one
        # exists within the last 7 days — otherwise it stands alone.
        alert_id = None
        try:
            rows = storage.alert_history(token, limit=1)
            if rows:
                created = datetime.fromisoformat(rows[0]["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if self._now() - created <= _FEEDBACK_ALERT_WINDOW:
                    alert_id = rows[0]["id"]
        except Exception as exc:  # noqa: BLE001 — the link is best-effort
            self._logger.warning("could not link feedback to an alert: %s", exc)
        storage.record_feedback(token, verdict, alert_id=alert_id)
        # Advisory only (Rule 8): deliberately NOT written into alerts.outcome
        # and NOT fed into learning labels — operator opinion is an opinion.
        return f"Feedback recorded: {'👍' if verdict == 'up' else '👎'} (advisory)"

    async def _handle_buy(self, data: str) -> str:
        address = data.split(":", 1)[1]
        if classify_address(address) is None:
            return "Invalid token address."
        execution = self._ctx.settings.execution
        if not execution.buy_button_enabled:
            return "Buy is not enabled."
        if self._ctx.executor is None:
            return "Buy is not wired up in this process."
        from meme_intelligence.trading.execution import TradeIntent

        token = self._resolve_token(address)
        intent = TradeIntent(
            token_address=token.address, chain=token.chain,
            sol_amount=execution.max_buy_sol, requested_at=self._now(),
            source="telegram",
        )
        message = await self._ctx.executor.execute_buy(intent)
        if not execution.dry_run:
            # Honesty over configuration: there IS no live executor, so the
            # flag cannot change behavior (see trading/execution.py docstring).
            message += (" Note: MEMEINTEL_EXECUTION_DRY_RUN=false has no effect — "
                        "no live executor exists.")
        await self._reply(message)
        return "Dry run only — details sent."

    async def _answer_callback(self, callback_id, text: str) -> None:
        if not callback_id:
            return
        try:
            await self._get_json(
                f"bot{self._token}/answerCallbackQuery",
                json_body={"callback_query_id": callback_id,
                           "text": text[:_CALLBACK_ACK_MAX_CHARS]},
            )
        except CollectorError as exc:
            self._logger.warning("answerCallbackQuery failed: %s", exc)
