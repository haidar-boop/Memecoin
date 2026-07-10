"""Live liquidity / round-trip sell-test collector (Project 1 — Jupiter quote
simulation). Solana only.

GoPlus's contract analysis is static (what the code says the contract
*could* do). This collector asks Jupiter's swap router for a real quote to
buy the token, then a real quote to sell it straight back — the same "can
you actually sell it?" test a trader would do by hand. A token that can be
bought but has no route to sell it is treated as a confirmed rug regardless
of how clean its static contract analysis looks (Rule 9 — multi-source:
this is deliberately independent evidence from GoPlus, not a restatement
of it).

Requires a free Jupiter Developer Platform API key (``x-api-key`` header)
— Jupiter deprecated its fully keyless "Lite" tier; the $0/month "Free"
plan still requires signup but has no monthly usage cap (rate-limited to
1 request/second). See https://developers.jup.ag/portal.
"""

from __future__ import annotations

from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import LiquidityProbeResult, TokenIdentity

# Wrapped SOL -- used as the round-trip quote currency rather than USDC:
# brand-new Solana meme tokens overwhelmingly pair with SOL first (pump.fun,
# fresh Raydium pools), so probing in SOL finds a route sooner for
# genuinely new-but-legitimate launches than probing in USDC would.
SOL_MINT = "So11111111111111111111111111111111111111112"

_LAMPORTS_PER_SOL = 1_000_000_000

# Statuses on which Jupiter's quote endpoint reports "no route" as a JSON
# error body rather than a 200 quote; treated as a normal, meaningful
# outcome (not a collector failure) whenever the body isn't quote-shaped.
_ROUTING_ERROR_STATUSES = frozenset({400, 404, 422})


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class JupiterClient(BaseCollector):
    """Client for the Jupiter Swap API's quote endpoint."""

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        if not api_key:
            raise ValueError("JupiterClient requires an API key")
        kwargs.setdefault("name", "jupiter")
        kwargs.setdefault("base_url", "https://api.jup.ag")
        super().__init__(**kwargs)
        self._headers = {"x-api-key": api_key}

    async def _quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
    ) -> dict[str, Any] | None:
        """One quote call; returns ``None`` when Jupiter reports no route exists."""
        payload = await self._get_json(
            "swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(slippage_bps),
            },
            headers=self._headers,
            cache_key=f"jupiter:quote:{input_mint}:{output_mint}:{amount}:{slippage_bps}",
            cache_ttl=45.0,
            error_status_as_json=_ROUTING_ERROR_STATUSES,
        )
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object from quote endpoint")
        if "outAmount" in payload and "routePlan" in payload:
            return payload

        reason = payload.get("errorCode") or payload.get("error") or "unknown reason"
        self._logger.info(
            "%s: no route %s -> %s (%s)", self.name, input_mint, output_mint, reason,
        )
        return None

    async def check_round_trip_liquidity(
        self,
        mint: str,
        *,
        probe_sol_amount: float,
        slippage_bps: int = 500,
        quote_mint: str = SOL_MINT,
    ) -> LiquidityProbeResult:
        """The buy-then-sell round trip -- can this token actually be sold back right now?"""
        if not mint:
            raise ValueError("mint must be non-empty")
        probe_lamports = int(probe_sol_amount * _LAMPORTS_PER_SOL)
        if probe_lamports <= 0:
            raise ValueError(
                f"probe_sol_amount {probe_sol_amount} is too small to probe with"
            )

        token = TokenIdentity(chain="solana", address=mint)

        buy = await self._quote(quote_mint, mint, probe_lamports, slippage_bps)
        if buy is None:
            return LiquidityProbeResult(token=token, source="jupiter", live_buy_route_found=False)

        received = _to_int(buy.get("outAmount"))
        if received is None or received <= 0:
            # Route exists but the quote was degenerate (e.g. zero received)
            # -- inconclusive, not a confirmed finding either way (Rule 8).
            return LiquidityProbeResult(token=token, source="jupiter", live_buy_route_found=True)

        sell = await self._quote(mint, quote_mint, received, slippage_bps)
        if sell is None:
            return LiquidityProbeResult(
                token=token, source="jupiter",
                live_buy_route_found=True, live_sell_route_found=False,
            )

        returned = _to_int(sell.get("outAmount")) or 0
        loss_percent = max(0.0, 100.0 * (probe_lamports - returned) / probe_lamports)
        return LiquidityProbeResult(
            token=token, source="jupiter",
            live_buy_route_found=True, live_sell_route_found=True,
            live_round_trip_loss_percent=loss_percent,
        )
