"""Alert delivery sinks (Spec Part 29, Sections 7-8).

``TelegramSink`` and ``DiscordSink`` deliver alerts through the channels
the spec names; both are built on the shared collector machinery so they
get rate limiting, retries, and timeouts for free (Rules 6/7/11).

Section 8 channel organization: every alert type belongs to one of the
spec's five channel categories (discoveries / smart money / security /
momentum / reports). Each sink takes an optional ``routes`` mapping of
category -> destination (Telegram chat id, Discord webhook URL); anything
unrouted goes to the sink's default destination, so a single-channel
setup works with zero extra configuration.

External sinks default to MEDIUM-and-above delivery — a phone that buzzes
for background information stops being read (Part 29 Section 1); the
console sink still shows everything. Configurable per Rule 17.
"""

from __future__ import annotations

from meme_intelligence.alerts.notification_engine import AlertEvent
from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.core.errors import CollectorError

# Priority rank for min-priority filtering (lower rank = more urgent).
_PRIORITY_RANK = {
    AlertPriority.CRITICAL: 0,
    AlertPriority.HIGH: 1,
    AlertPriority.MEDIUM: 2,
    AlertPriority.LOW: 3,
}

# Section 8 channel categories, keyed by alert type. Unknown/future alert
# types fall through to "reports".
ALERT_CHANNELS = {
    "high_priority_opportunity": "discoveries",
    "strong_candidate": "discoveries",
    "early_opportunity": "discoveries",
    "new_token_discovery": "discoveries",
    "smart_money_accumulation": "smart_money",
    "whale_exit": "smart_money",
    "emergency_review": "security",
    "risk_warning": "security",
    "security_change": "security",
    "token_death": "security",
    "insider_risk": "security",
    "community_fake": "security",
    "momentum": "momentum",
    "score_drop_review": "reports",
}

# Section 7 "Risk Assessment: Low / Medium / High", derived from priority —
# the priority levels already encode the risk grading (Section 2).
_RISK_FOR_PRIORITY = {
    AlertPriority.CRITICAL: "High",
    AlertPriority.HIGH: "High",
    AlertPriority.MEDIUM: "Medium",
    AlertPriority.LOW: "Low",
}

# Section 2 output headers per priority level.
_HEADER_FOR_PRIORITY = {
    AlertPriority.CRITICAL: "CRITICAL RISK EVENT",
    AlertPriority.HIGH: "HIGH IMPORTANCE UPDATE",
    AlertPriority.MEDIUM: "MONITOR UPDATE",
    AlertPriority.LOW: "INFORMATION UPDATE",
}


def channel_for(event: AlertEvent) -> str:
    return ALERT_CHANNELS.get(event.alert_type, "reports")


def _sanitize_identity(value: str | None, *, max_len: int = 64) -> str:
    """Neutralize attacker-controlled token names/symbols (bug-hunt finding).

    On-chain metadata is unbounded and arbitrary: a token literally named
    with backticks + newlines broke out of Discord's code fence and injected
    live markdown (incl. mention pings) into the owner's alert channel.
    Non-printable characters and newlines are dropped, backticks neutralized,
    and length capped; the contract address (validated charset) stays exact.
    """
    if not value:
        return "unknown"
    cleaned = "".join(ch for ch in value if ch.isprintable()).replace("`", "'")
    cleaned = cleaned.strip()
    return (cleaned[:max_len] + "…") if len(cleaned) > max_len else (cleaned or "unknown")


# Telegram's hard limit on inline-button callback_data is 64 BYTES; the
# feedback prefixes (fb:1: / fb:0: / buy:) leave room for the longest
# on-chain address format in use (Solana base58, 44 chars). An address
# that somehow exceeds the limit simply gets no buttons — never truncated
# (a truncated address would route feedback to the wrong token).
_CALLBACK_DATA_MAX_BYTES = 64


# Telegram's copy_text inline button (Bot API 7.11+) puts a value on the
# phone's clipboard in one tap; its text is capped at 256 chars.
_COPY_TEXT_MAX_CHARS = 256


def copy_address_button(address: str) -> dict | None:
    """One-tap [Copy address] button (operator request: paste the contract
    address straight into a wallet/DEX without long-pressing the message)."""
    if not address or len(address) > _COPY_TEXT_MAX_CHARS:
        return None
    return {"text": "📋 Copy address", "copy_text": {"text": address}}


def copy_keyboard(address: str) -> dict | None:
    """A reply_markup containing only the copy-address button, for command
    replies (/check, /why) that mention a token."""
    button = copy_address_button(address)
    return {"inline_keyboard": [[button]]} if button else None


def _fits(callback_data: str) -> bool:
    return len(callback_data.encode()) <= _CALLBACK_DATA_MAX_BYTES


def feedback_keyboard(address: str, *, buy_presets: tuple[float, ...] = ()) -> dict | None:
    """Inline buttons for one alert: 👍/👎 feedback, a one-tap copy-address
    button, and — when ``buy_presets`` is non-empty (trading enabled) — a row
    of one-tap buy buttons plus a Dump button. Returns ``None`` when no valid
    keyboard can be built."""
    if not address or not _fits(f"fb:1:{address}"):
        return None
    keyboard = [[
        {"text": "👍", "callback_data": f"fb:1:{address}"},
        {"text": "👎", "callback_data": f"fb:0:{address}"},
    ]]
    copy_button = copy_address_button(address)
    if copy_button is not None:
        keyboard.append([copy_button])
    buy_row = [
        {"text": f"Buy {amount:g}◎", "callback_data": f"buy:{address}:{amount:g}"}
        for amount in buy_presets if _fits(f"buy:{address}:{amount:g}")
    ]
    if buy_row:
        keyboard.append(buy_row)
    if buy_presets and _fits(f"dump:{address}"):
        keyboard.append([{"text": "💥 Dump all", "callback_data": f"dump:{address}"}])
    return {"inline_keyboard": keyboard}


def format_alert(event: AlertEvent) -> str:
    """Render the full Part 29 Section 7 message format."""
    lines = [
        _HEADER_FOR_PRIORITY[event.priority],
        event.alert_type.replace("_", " ").upper(),
        "",
        "Token",
        f"  Name: {_sanitize_identity(event.token.name)}",
        f"  Symbol: {_sanitize_identity(event.token.symbol)}",
        f"  Contract: {event.token.address}",
        f"  Chain: {event.token.chain}",
    ]
    if event.detected_at is not None:
        lines += ["", f"Time detected: {event.detected_at.strftime('%Y-%m-%d %H:%M UTC')}"]
    if event.history_note:
        # A re-alert must never read like a brand-new discovery (operator
        # complaint 2026-07-14) — the history rides right under the header.
        lines += ["", f"Seen before: {event.history_note}"]
    lines += ["", "Event summary", f"  {event.title}"]
    if event.why_it_matters:
        lines += ["", "Why it matters", f"  {event.why_it_matters}"]
    if event.reasons:
        lines += ["", "Evidence"]
        lines += [f"  - {reason}" for reason in event.reasons]
    if event.checklist:
        # Pre-rendered with ✅/⚠/ℹ/❔ icons and a "passed X/Y" header line.
        lines += [""]
        lines += [f"  {line}" for line in event.checklist]
    if event.scores:
        lines += ["", "Current scores"]
        lines += [f"  {name}: " + (f"{value:.0f}/100" if value is not None else "no data")
                  for name, value in event.scores.items()]
    lines += ["", f"Risk assessment: {_RISK_FOR_PRIORITY[event.priority]}"]
    if event.monitoring:
        lines += ["", "Recommended monitoring"]
        lines += [f"  - {item}" for item in event.monitoring]
    return "\n".join(lines)


class TelegramSink(BaseCollector):
    """Delivers alerts via a Telegram bot (Part 29, Section 8).

    ``routes`` optionally maps channel categories to chat ids; everything
    else goes to ``chat_id``. Delivery failures are logged and REPORTED
    (``send`` returns False) so the engine can retry after the cooldown —
    but never raised: an unreachable messenger must never stop the scanner
    (Rule 7).
    """

    external = True  # a real delivery target, not a local log (see dispatch)

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        routes: dict[str, str] | None = None,
        min_priority: AlertPriority = AlertPriority.MEDIUM,
        buy_presets_sol: tuple[float, ...] = (),  # non-empty -> show Buy/Dump buttons
        **kwargs,
    ) -> None:
        kwargs.setdefault("name", "telegram")
        kwargs.setdefault("base_url", "https://api.telegram.org")
        kwargs.setdefault("redact", (bot_token,))
        super().__init__(**kwargs)
        self._token = bot_token
        self._chat_id = chat_id
        self._routes = routes or {}
        self._min_rank = _PRIORITY_RANK[min_priority]
        self._buy_presets = tuple(buy_presets_sol)

    async def send(self, event: AlertEvent) -> bool | None:
        """True = delivered, False = FAILED, None = filtered by min-priority.

        Reporting the failure (instead of swallowing it, bug-hunt finding)
        lets the engine skip the cooldown stamp so a lost phone alert
        retries once the window elapses, rather than being counted as
        delivered and gone forever.
        """
        if _PRIORITY_RANK[event.priority] > self._min_rank:
            return None
        chat_id = self._routes.get(channel_for(event), self._chat_id)
        json_body = {
            "chat_id": chat_id,
            "text": format_alert(event)[:4000],  # Telegram hard limit 4096
            "disable_web_page_preview": True,
        }
        # Project 2 feedback loop: every alert carries 👍/👎 buttons (and the
        # buy button only when the operator flipped the execution flag). The
        # sink and the command listener are separate objects — feedback flows
        # back through the listener and lands in storage; no shared state.
        markup = feedback_keyboard(event.token.address, buy_presets=self._buy_presets)
        if markup is not None:
            json_body["reply_markup"] = markup
        try:
            payload = await self._get_json(
                f"bot{self._token}/sendMessage",
                json_body=json_body,
            )
            if not (isinstance(payload, dict) and payload.get("ok")):
                raise CollectorError(f"telegram: unexpected response {payload!r}")
            self._logger.info("telegram alert sent: %s %s -> chat %s",
                              event.priority.value, event.alert_type, chat_id)
            return True
        except CollectorError as exc:
            self._logger.error("telegram delivery failed for %s %s: %s",
                               event.alert_type, event.token.address, exc)
            return False


class DiscordSink(BaseCollector):
    """Delivers alerts via Discord webhooks (Part 29, Section 8).

    A Discord webhook IS a channel, so Section 8's channel organization
    maps naturally: ``routes`` holds per-category webhook URLs, with
    ``webhook_url`` as the default.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        routes: dict[str, str] | None = None,
        min_priority: AlertPriority = AlertPriority.MEDIUM,
        **kwargs,
    ) -> None:
        kwargs.setdefault("name", "discord")
        kwargs.setdefault("base_url", "https://discord.com")
        # Every webhook URL IS a credential (anyone holding it can post to
        # that channel) — the default and every per-category route are
        # redacted from log/error text the same way a bot token is (Rule 16).
        kwargs.setdefault("redact", (webhook_url, *(routes or {}).values()))
        super().__init__(**kwargs)
        self._webhook_url = webhook_url
        self._routes = routes or {}
        self._min_rank = _PRIORITY_RANK[min_priority]

    external = True  # a real delivery target, not a local log (see dispatch)

    async def send(self, event: AlertEvent) -> bool | None:
        """True = delivered, False = FAILED, None = filtered (see TelegramSink)."""
        if _PRIORITY_RANK[event.priority] > self._min_rank:
            return None
        url = self._routes.get(channel_for(event), self._webhook_url)
        try:
            # ?wait=true makes Discord return 200 + JSON instead of a bare 204.
            await self._get_json(
                url + ("&wait=true" if "?" in url else "?wait=true"),
                json_body={
                    "content": f"```\n{format_alert(event)[:1900]}\n```",
                    # Webhook default parses @everyone/@here from content —
                    # alert text embeds on-chain metadata, so mentions are
                    # disabled outright (bug-hunt finding).
                    "allowed_mentions": {"parse": []},
                },
            )
            self._logger.info("discord alert sent: %s %s", event.priority.value,
                              event.alert_type)
            return True
        except CollectorError as exc:
            self._logger.error("discord delivery failed for %s %s: %s",
                               event.alert_type, event.token.address, exc)
            return False


def parse_routes(spec: str) -> dict[str, str]:
    """Parse a ``category=destination,category=destination`` route string.

    Unknown categories are rejected loudly rather than silently ignored —
    a typo in routing must not quietly send security alerts nowhere.
    """
    routes: dict[str, str] = {}
    valid = set(ALERT_CHANNELS.values())
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep or not value.strip() or key.strip() not in valid:
            raise ValueError(
                f"invalid alert route {part!r}: expected category=destination with "
                f"category one of {sorted(valid)}"
            )
        routes[key.strip()] = value.strip()
    return routes
