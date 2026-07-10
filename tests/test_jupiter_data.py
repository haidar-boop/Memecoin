"""Tests for the Jupiter live round-trip sell-test collector (Project 1)."""

import pytest

from meme_intelligence.collectors.jupiter_data import SOL_MINT, JupiterClient
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError, TransientCollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

MINT = "MemeMint1111111111111111111111111111111111"

PROBE_SOL_AMOUNT = 1.0
PROBE_LAMPORTS = 1_000_000_000


def make_client() -> JupiterClient:
    return JupiterClient("test-key", rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache())


def route(out_amount: str) -> dict:
    return {"outAmount": out_amount, "routePlan": [{"swapInfo": {}}]}


NO_ROUTE = {"errorCode": "COULD_NOT_FIND_ANY_ROUTE", "error": "no route found"}


def patch_calls(monkeypatch, client, responses: list):
    """Return successive canned responses for successive _get_json calls."""
    calls = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None, error_status_as_json=frozenset()):
        calls.append(dict(params or {}))
        response = responses[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    return calls


async def test_round_trip_low_loss(monkeypatch):
    client = make_client()
    # Buy: 1 SOL -> 500,000 token units. Sell: 500,000 -> 990,000,000 lamports (1% loss).
    calls = patch_calls(monkeypatch, client, [route("500000"), route("990000000")])

    result = await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)

    assert result.live_buy_route_found is True
    assert result.live_sell_route_found is True
    assert result.live_round_trip_loss_percent == pytest.approx(1.0)
    assert len(calls) == 2
    assert calls[0]["inputMint"] == SOL_MINT
    assert calls[0]["outputMint"] == MINT
    assert calls[0]["amount"] == str(PROBE_LAMPORTS)
    assert calls[1]["inputMint"] == MINT
    assert calls[1]["outputMint"] == SOL_MINT
    assert calls[1]["amount"] == "500000"


async def test_buy_route_not_found_short_circuits(monkeypatch):
    client = make_client()
    calls = patch_calls(monkeypatch, client, [NO_ROUTE])

    result = await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)

    assert result.live_buy_route_found is False
    assert result.live_sell_route_found is None
    assert result.live_round_trip_loss_percent is None
    assert len(calls) == 1  # sell leg never attempted


async def test_confirmed_honeypot_when_no_sell_of_any_size_routes(monkeypatch):
    """Full AND confirmation (small) sell both find no route -> real cannot-sell."""
    client = make_client()
    # buy -> 500,000 units; full sell fails; small (5% = 25,000) sell also fails.
    calls = patch_calls(monkeypatch, client, [route("500000"), NO_ROUTE, NO_ROUTE])

    result = await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)

    assert result.live_buy_route_found is True
    assert result.live_sell_route_found is False   # confirmed non-sellable -> destructive
    assert result.live_round_trip_loss_percent is None
    assert len(calls) == 3                          # buy + full sell + confirm sell
    assert calls[2]["amount"] == str(int(500000 * 0.05))  # 25,000


async def test_thin_pool_full_sell_fails_but_small_sell_confirms_route(monkeypatch):
    """A full-size sell that fails ONLY because the pool is thin is NOT a
    honeypot: the small confirmation sell routes, so a sell route exists and
    the token is not condemned (the false positive this fix removes)."""
    client = make_client()
    calls = patch_calls(monkeypatch, client, [route("500000"), NO_ROUTE, route("40000000")])

    result = await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)

    assert result.live_buy_route_found is True
    assert result.live_sell_route_found is True     # a route exists -> NOT destructive
    assert result.live_round_trip_loss_percent is None  # full-position exit unmeasured
    assert len(calls) == 3
    assert calls[2]["amount"] == str(int(500000 * 0.05))


async def test_degenerate_buy_quote_is_inconclusive(monkeypatch):
    """Route exists but zero received -- not a confirmed finding either way (Rule 8)."""
    client = make_client()
    calls = patch_calls(monkeypatch, client, [route("0")])

    result = await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)

    assert result.live_buy_route_found is True
    assert result.live_sell_route_found is None
    assert result.live_round_trip_loss_percent is None
    assert len(calls) == 1  # sell leg never attempted on a degenerate buy


async def test_genuine_server_error_still_raises(monkeypatch):
    """A real collector failure (5xx) must not be treated as 'no route'."""
    client = make_client()
    patch_calls(monkeypatch, client, [TransientCollectorError("jupiter: server error 500")])

    with pytest.raises(TransientCollectorError):
        await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)


async def test_malformed_json_still_raises(monkeypatch):
    client = make_client()
    patch_calls(monkeypatch, client, [CollectorError("jupiter: invalid JSON from ...")])

    with pytest.raises(CollectorError):
        await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)


async def test_non_dict_payload_raises(monkeypatch):
    client = make_client()
    patch_calls(monkeypatch, client, [["not", "a", "dict"]])

    with pytest.raises(CollectorError, match="expected JSON object"):
        await client.check_round_trip_liquidity(MINT, probe_sol_amount=PROBE_SOL_AMOUNT)


def test_missing_api_key_rejected():
    with pytest.raises(ValueError):
        JupiterClient("", rate_limiter=RateLimiter(10.0))


async def test_too_small_probe_amount_rejected():
    client = make_client()
    with pytest.raises(ValueError):
        await client.check_round_trip_liquidity(MINT, probe_sol_amount=0.0)


async def test_empty_mint_rejected():
    client = make_client()
    with pytest.raises(ValueError):
        await client.check_round_trip_liquidity("", probe_sol_amount=PROBE_SOL_AMOUNT)
