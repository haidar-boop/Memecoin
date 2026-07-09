"""Pump.fun launch-discovery collectors (Spec Part 32.5 Section 3).

Two complementary sources:

* **PumpPortal WebSocket** (:class:`PumpPortalClient`) — free, keyless
  real-time stream of token-creation and bonding-curve-migration events.
  Event-driven monitoring instead of polling, exactly as Part 32.5
  Section 6 and Rule 10 prescribe. PumpPortal allows ONE data connection
  per client (multiple connections risk a temporary ban), so the client
  maintains a single background listener with reconnect-and-backoff
  (Rule 7) and hands events to the scanner through bounded buffers.
* **Pump.fun frontend API** (:class:`PumpFunFrontendClient`) — the
  unofficial per-coin endpoint used by the launch monitor to recheck
  traction on tracked launches. It is an unofficial surface that has
  changed before (v1/v2 hosts were deprecated); every field is treated
  as optional and failures degrade to data gaps, never crashes (Rules
  6/8/9). Payload shape verified live against ``frontend-api-v3`` on
  2026-07-08.

Neither source is confirmation: a launch enters the analysis pipeline
only after an independent market-data provider sees it (Part 32.5
Section 2 — "The AI must never treat discovery as confirmation").
"""

from __future__ import annotations

import asyncio
import json
import math
import ssl
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import PumpFunCoinState, PumpFunLaunch, TokenIdentity


def _to_float(value: Any) -> float | None:
    """Parse a numeric field that providers send as float, int, or string."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _from_ms_timestamp(value: Any) -> datetime | None:
    """Parse a millisecond epoch timestamp; any out-of-range/non-finite value
    is treated as missing, never a crash (Rule 6/8) — this frontend API is
    unofficial and has sent garbage before."""
    ms = _to_float(value)
    if ms is None or not math.isfinite(ms) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# Buffer bounds: the scanner drains every cycle (default 45s); Pump.fun
# launch rates are a few per second at peak, so 2048 covers well over a
# cycle of backlog. Overflow drops the OLDEST events — for a freshness-
# driven discovery stream, stale launches are the right ones to lose.
_LAUNCH_BUFFER_MAX = 2048
_MIGRATION_BUFFER_MAX = 512

# Reconnect backoff bounds for the WebSocket listener (Rule 7).
_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0


class PumpPortalClient:
    """Single-connection PumpPortal data-stream listener (Part 32.5 Sections 3/6).

    Not a :class:`BaseCollector` — that contract is request/response HTTP,
    while this is a long-lived event stream. The scanner interacts only
    with the buffers::

        client = PumpPortalClient(ws_url)
        await client.start()             # idempotent; spawns the listener
        launches = client.drain_launches()
        graduated = client.drain_migrations()
        await client.close()

    The listener task survives disconnects by reconnecting with
    exponential backoff and re-subscribing; events during an outage are
    lost (the stream has no replay), which is an accepted gap — the
    frontend-API recheck path and normal pool discovery still see any
    token that matters (Rule 9: degrade, keep operating).
    """

    def __init__(
        self,
        ws_url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        sleep_func: Callable[[float], Any] = asyncio.sleep,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.name = "pumpportal"
        self._ws_url = ws_url
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep_func
        self._now = now_func
        self._launches: deque[PumpFunLaunch] = deque(maxlen=_LAUNCH_BUFFER_MAX)
        self._migrations: deque[TokenIdentity] = deque(maxlen=_MIGRATION_BUFFER_MAX)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._connected = False
        self._logger = get_logger("collectors.pumpportal")

    @property
    def connected(self) -> bool:
        """Whether the listener currently holds an open stream connection."""
        return self._connected

    async def start(self) -> None:
        """Spawn the background listener; safe to call repeatedly."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._listen_forever(), name="pumpportal-listener")
            self._logger.info("pumpportal listener started (%s)", self._ws_url)

    async def close(self) -> None:
        """Stop the listener and release the session if this client owns it."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown must not raise
                pass
            self._task = None
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "PumpPortalClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    def drain_launches(self) -> list[PumpFunLaunch]:
        """Return and clear all buffered launch events (oldest first)."""
        drained = list(self._launches)
        self._launches.clear()
        return drained

    def drain_migrations(self) -> list[TokenIdentity]:
        """Return and clear all buffered graduation/migration events."""
        drained = list(self._migrations)
        self._migrations.clear()
        return drained

    # ---- Connection management (Rule 7: recover, keep operating) ----

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
            self._session = aiohttp.ClientSession(trust_env=True, connector=connector)
            self._owns_session = True
        return self._session

    async def _listen_forever(self) -> None:
        delay = _RECONNECT_BASE_DELAY
        while not self._stopping:
            try:
                session = await self._get_session()
                async with session.ws_connect(self._ws_url, heartbeat=30.0) as ws:
                    await ws.send_json({"method": "subscribeNewToken"})
                    await ws.send_json({"method": "subscribeMigration"})
                    self._connected = True
                    delay = _RECONNECT_BASE_DELAY  # healthy connection resets backoff
                    self._logger.info("pumpportal stream connected and subscribed")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the listener must outlive any error
                self._logger.warning("pumpportal stream error: %s: %s",
                                     type(exc).__name__, exc)
            finally:
                self._connected = False
            if self._stopping:
                break
            self._logger.info("pumpportal reconnecting in %.0fs", delay)
            await self._sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    # ---- Message handling (separable for tests) ----

    def _handle_message(self, raw: str) -> None:
        """Parse one stream message into a buffer; malformed input is logged, never fatal."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            self._logger.warning("pumpportal: discarding invalid JSON message: %s", exc)
            return
        if not isinstance(data, dict):
            return
        if "message" in data and "mint" not in data:
            return  # subscription acknowledgment
        tx_type = data.get("txType")
        if tx_type == "create":
            launch = self._parse_launch(data)
            if launch is not None:
                self._launches.append(launch)
        elif tx_type == "migrate":
            mint = data.get("mint")
            if isinstance(mint, str) and mint:
                self._migrations.append(TokenIdentity(chain="solana", address=mint))

    def _parse_launch(self, data: dict) -> PumpFunLaunch | None:
        """Normalize one ``txType=create`` event (payload shape verified live)."""
        mint = data.get("mint")
        if not isinstance(mint, str) or not mint:
            self._logger.warning("pumpportal: create event without mint, discarding")
            return None
        name = data.get("name")
        symbol = data.get("symbol")
        initial_buy_tokens = _to_float(data.get("initialBuy"))
        # Dev-buy as a supply percentage: the stream reports the buy in UI
        # token units against a fixed 1B launch supply. Derived from two
        # fields of the same event — a computation, not a fabrication.
        total_supply_tokens = 1_000_000_000.0
        initial_buy_percent = None
        if initial_buy_tokens is not None:
            initial_buy_percent = max(0.0, min(100.0, 100.0 * initial_buy_tokens / total_supply_tokens))
        return PumpFunLaunch(
            token=TokenIdentity(
                chain="solana",
                address=mint,
                name=name if isinstance(name, str) and name else None,
                symbol=symbol if isinstance(symbol, str) and symbol else None,
            ),
            source=self.name,
            launchpad=data.get("pool") if isinstance(data.get("pool"), str) else None,
            creator=data.get("traderPublicKey") if isinstance(data.get("traderPublicKey"), str) else None,
            created_at=self._now(),  # stream events carry no timestamp; arrival time is honest
            initial_buy_tokens=initial_buy_tokens,
            initial_buy_sol=_to_float(data.get("solAmount")),
            initial_buy_percent=initial_buy_percent,
            market_cap_sol=_to_float(data.get("marketCapSol")),
            bonding_curve=data.get("bondingCurveKey") if isinstance(data.get("bondingCurveKey"), str) else None,
            signature=data.get("signature") if isinstance(data.get("signature"), str) else None,
        )


class PumpFunFrontendClient(BaseCollector):
    """Client for the unofficial Pump.fun frontend API (Part 32.5 Section 3).

    Used only for per-coin traction rechecks on already-discovered
    launches — never for bulk polling (the WebSocket is the discovery
    feed). Keyless as of 2026-07; the host has rotated before
    (v1/v2 deprecated), so the base URL is configuration (Rule 17) and
    every failure degrades to a data gap.
    """

    # Tokens initially tradable on the bonding curve (protocol constant:
    # 793.1M of the 1B supply, in 6-decimal base units). Progress toward
    # graduation = fraction of these already bought out of the curve.
    _INITIAL_CURVE_TOKEN_RESERVES = 793_100_000_000_000

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "pumpfun")
        kwargs.setdefault("base_url", "https://frontend-api-v3.pump.fun")
        super().__init__(**kwargs)

    async def get_coin_state(self, token: TokenIdentity) -> PumpFunCoinState | None:
        """Current traction snapshot for one launch, or ``None`` if unknown there.

        A 404 means the coin is not (or no longer) served by the frontend
        API — an honest gap, not an error (Rule 8).
        """
        if token.chain != "solana":
            return None
        try:
            payload = await self._get_json(
                f"coins/{token.address}",
                cache_key=f"pumpfun:coin:{token.address.lower()}",
                cache_ttl=60.0,  # rechecks are per-token and infrequent; short TTL
            )
        except CollectorError as exc:
            if exc.status_code == 404:
                self._logger.info("%s: %s not found on pump.fun frontend API",
                                  self.name, token.address)
                return None
            raise
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object, got {type(payload).__name__}")
        return self._parse_coin(token, payload)

    def _parse_coin(self, token: TokenIdentity, payload: dict) -> PumpFunCoinState:
        real_token_reserves = _to_float(payload.get("real_token_reserves"))
        curve_progress = None
        if real_token_reserves is not None and 0 <= real_token_reserves <= self._INITIAL_CURVE_TOKEN_RESERVES:
            curve_progress = 100.0 * (1.0 - real_token_reserves / self._INITIAL_CURVE_TOKEN_RESERVES)
        name = payload.get("name")
        symbol = payload.get("symbol")
        return PumpFunCoinState(
            token=TokenIdentity(
                chain=token.chain,
                address=token.address,
                name=name if isinstance(name, str) and name else token.name,
                symbol=symbol if isinstance(symbol, str) and symbol else token.symbol,
            ),
            source=self.name,
            fetched_at=datetime.now(timezone.utc),
            market_cap_sol=_to_float(payload.get("market_cap")),
            usd_market_cap=_to_float(payload.get("usd_market_cap")),
            reply_count=_to_int(payload.get("reply_count")),
            complete=_to_bool(payload.get("complete")),
            curve_progress_percent=curve_progress,
            is_banned=_to_bool(payload.get("is_banned")),
            nsfw=_to_bool(payload.get("nsfw")),
            created_at=_from_ms_timestamp(payload.get("created_timestamp")),
            last_trade_at=_from_ms_timestamp(payload.get("last_trade_timestamp")),
            ath_market_cap_sol=_to_float(payload.get("ath_market_cap")),
            creator=payload.get("creator") if isinstance(payload.get("creator"), str) else None,
        )
