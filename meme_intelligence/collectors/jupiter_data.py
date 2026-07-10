"""Live liquidity / round-trip sell-test collector (Project 1 — Jupiter quote
simulation). Solana only.

GoPlus's contract analysis is static (what the code says the contract
*could* do). This collector asks Jupiter's swap router for a real quote to
buy the token, then a real quote to sell it straight back — the same "can
you actually sell it?" test a trader would do by hand. A token that can be
bought but has no route to sell it back AT ANY SIZE is treated as a
confirmed rug regardless of how clean its static contract analysis looks
(Rule 9 — multi-source: this is deliberately independent evidence from
GoPlus, not a restatement of it). A full-size sell that fails only because
the pool is too thin to exit the whole position at once is NOT a rug and is
confirmed with a small follow-up sell before any verdict (Rule 8 —
unknown/thin != unsafe).

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
        *, use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """One quote call; returns ``None`` when Jupiter reports no route exists.

        ``use_cache`` MUST be False for live trading: a signed on-chain swap
        must be built from a quote fetched at trade time, never a cached one up
        to 45s stale (the cache is fine for the read-only liquidity probe)."""
        cache_key = (f"jupiter:quote:{input_mint}:{output_mint}:{amount}:{slippage_bps}"
                     if use_cache else None)
        payload = await self._get_json(
            "swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(slippage_bps),
            },
            headers=self._headers,
            cache_key=cache_key,
            cache_ttl=45.0 if use_cache else None,
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

    async def get_quote(self, input_mint: str, output_mint: str, amount: int,
                        slippage_bps: int, *, use_cache: bool = True) -> dict[str, Any] | None:
        """Public quote for one swap direction (used by the live executor).

        Returns the full quote response dict, or ``None`` when Jupiter reports
        no route exists (never confused with an API failure). Live trades pass
        ``use_cache=False`` so every signed swap uses a trade-time quote."""
        return await self._quote(input_mint, output_mint, amount, slippage_bps,
                                 use_cache=use_cache)

    async def build_swap_transaction(
        self, quote_response: dict, user_public_key: str, *,
        priority_fee_max_lamports: int = 1_000_000,
        max_slippage_bps: int = 500,
    ) -> str:
        """POST /swap/v1/swap: turn a quote into a base64 transaction to sign.

        Slippage is bounded by ``max_slippage_bps``: dynamic slippage is used
        for good landing, but capped so the SIGNED transaction can never
        tolerate more slippage than the operator configured (a bare
        ``dynamicSlippage: true`` would let Jupiter fill a thin meme pool
        20-50% below quote — money-safety fix). Caps the priority fee and
        wraps/unwraps SOL automatically. The returned transaction is unsigned —
        the caller signs it with the trading wallet and submits it (Project 6)."""
        if not user_public_key:
            raise ValueError("build_swap_transaction requires a user public key")
        payload = await self._get_json(
            "swap/v1/swap",
            headers=self._headers,
            json_body={
                "quoteResponse": quote_response,
                "userPublicKey": user_public_key,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "dynamicSlippage": {"maxBps": int(max_slippage_bps)},
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": int(priority_fee_max_lamports),
                        "priorityLevel": "veryHigh",
                    }
                },
            },
        )
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object from swap endpoint")
        if payload.get("simulationError"):
            raise CollectorError(
                f"{self.name}: swap simulation failed: {payload['simulationError']}")
        swap_tx = payload.get("swapTransaction")
        if not isinstance(swap_tx, str) or not swap_tx:
            raise CollectorError(f"{self.name}: swap endpoint returned no transaction")
        return swap_tx

    async def check_round_trip_liquidity(
        self,
        mint: str,
        *,
        probe_sol_amount: float,
        slippage_bps: int = 500,
        quote_mint: str = SOL_MINT,
        sell_confirm_fraction: float = 0.05,
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
            # A FULL-size sell found no route -- but Jupiter returns the same
            # "no route" for three different things: a real honeypot (no sell
            # ever), a not-yet-indexed sell direction on a brand-new pool, or
            # simply a pool too thin to absorb the whole position in one swap.
            # Only the first is a rug. Condemning on the full-size failure
            # alone would sink fresh/thin but legitimate launches -- the exact
            # false positive the buy leg is careful to avoid (Rule 8). So we
            # CONFIRM with a tiny sell before calling it non-sellable:
            #   * tiny sell also fails -> nothing sells -> real cannot-sell
            #     (honeypot; the destructive override fires downstream);
            #   * tiny sell succeeds  -> a sell route DOES exist, the full
            #     size just exceeded instantaneous depth -> NOT a honeypot.
            #     Sellability is confirmed; round-trip loss stays unknown
            #     (a 5% sell doesn't measure a full-position exit), and the
            #     liquidity sub-score already handles thin pools.
            small = max(1, int(received * sell_confirm_fraction))
            confirm = await self._quote(mint, quote_mint, small, slippage_bps)
            if confirm is None:
                return LiquidityProbeResult(
                    token=token, source="jupiter",
                    live_buy_route_found=True, live_sell_route_found=False,
                )
            return LiquidityProbeResult(
                token=token, source="jupiter",
                live_buy_route_found=True, live_sell_route_found=True,
            )

        returned = _to_int(sell.get("outAmount")) or 0
        loss_percent = max(0.0, 100.0 * (probe_lamports - returned) / probe_lamports)
        return LiquidityProbeResult(
            token=token, source="jupiter",
            live_buy_route_found=True, live_sell_route_found=True,
            live_round_trip_loss_percent=loss_percent,
        )
