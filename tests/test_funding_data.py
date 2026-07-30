"""Tests for the funding-data collector behind /bundle."""

import pytest

from meme_intelligence.analyzers.wallet_clusters import ESTABLISHED, FRESH, UNKNOWN
from meme_intelligence.collectors.funding_data import (
    _SIGNATURE_PAGE_LIMIT,
    BundleService,
    FundingClient,
)
from meme_intelligence.config.settings import WalletClusterSettings
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

SETTINGS = WalletClusterSettings()
# Real on-curve addresses (verified 2026-07-30) so the custody filter keeps them.
WALLET = "6uRXCp3v13N4FKfMiU1oS7ztnnbNXPLWc9HSp6X8a5oq"
FUNDER = "ArB1hBWhRKxpqEykMN25cFrZdwUfg71FNUfh921PFapW"
VAULT = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"  # off-curve custody


def make_client():
    return FundingClient(SETTINGS, api_key="test-key",
                         rate_limiter=RateLimiter(1000.0, burst=100),
                         cache=TTLCache())


def patch_rpc(monkeypatch, client, handler):
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None,
                            error_status_as_json=frozenset()):
        result = handler(json_body["method"], json_body["params"])
        if isinstance(result, Exception):
            raise result
        return {"jsonrpc": "2.0", "id": 1, "result": result}
    monkeypatch.setattr(client, "_get_json", fake_get_json)


def sig_entries(count, prefix="sig"):
    return [{"signature": f"{prefix}{i}", "slot": 1000 + i} for i in range(count)]


def tx_with_payer(payer):
    return {"transaction": {"message": {"accountKeys": [
        {"pubkey": payer, "signer": True, "writable": True},
        {"pubkey": "SomeoneElse", "signer": False},
    ]}}}


# ---------------------------------------------------------------------------
# get_wallet_origin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_fresh_wallet_yields_its_funder_and_first_signature(monkeypatch):
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            return sig_entries(5)          # oldest is sig4
        if method == "getTransaction":
            assert params[0] == "sig4", "must fetch the OLDEST signature"
            return tx_with_payer(FUNDER)
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == FRESH
    assert origin.funder == FUNDER
    assert origin.first_signature == "sig4"
    assert origin.first_slot == 1004


@pytest.mark.asyncio
async def test_a_saturated_page_means_established_and_no_funder_claim(monkeypatch):
    """A full page proves long history, not its contents. Claiming a funder from
    the truncated page would name the wrong transaction as 'first'."""
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            return sig_entries(_SIGNATURE_PAGE_LIMIT)
        raise AssertionError(f"must not call {method} for an established wallet")

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == ESTABLISHED
    assert origin.funder is None
    assert origin.first_signature is None


@pytest.mark.asyncio
async def test_a_self_paid_first_transaction_claims_no_funder(monkeypatch):
    """If the wallet paid its own first fee, no funder is observable here —
    inventing one would be fabrication (Rule 8)."""
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            return sig_entries(2)
        if method == "getTransaction":
            return tx_with_payer(WALLET)   # payer == the wallet itself
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == FRESH
    assert origin.funder is None
    assert origin.first_signature == "sig1"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    CollectorError("rpc down"),
    "not-a-list",
    None,
])
async def test_unreadable_history_is_unknown_never_guessed(monkeypatch, failure):
    client = make_client()
    patch_rpc(monkeypatch, client, lambda m, p: failure)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == UNKNOWN
    assert origin.funder is None
    assert origin.note


@pytest.mark.asyncio
async def test_a_failed_transaction_fetch_degrades_to_no_funder(monkeypatch):
    """The first-signature evidence survives even when the transaction body
    cannot be read — co-creation clustering still works, funder edges don't."""
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            return sig_entries(3)
        if method == "getTransaction":
            return CollectorError("tx fetch failed")
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == FRESH
    assert origin.funder is None
    assert origin.first_signature == "sig2"


@pytest.mark.asyncio
async def test_malformed_oldest_entry_is_unknown(monkeypatch):
    client = make_client()
    patch_rpc(monkeypatch, client,
              lambda m, p: [{"slot": 5}] if m == "getSignaturesForAddress" else None)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == UNKNOWN


# ---------------------------------------------------------------------------
# is_high_activity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_activity_detection(monkeypatch):
    client = make_client()
    patch_rpc(monkeypatch, client,
              lambda m, p: sig_entries(_SIGNATURE_PAGE_LIMIT))
    assert await client.is_high_activity(FUNDER) is True

    client2 = make_client()
    patch_rpc(monkeypatch, client2, lambda m, p: sig_entries(40))
    assert await client2.is_high_activity(FUNDER) is False

    client3 = make_client()
    patch_rpc(monkeypatch, client3, lambda m, p: CollectorError("down"))
    assert await client3.is_high_activity(FUNDER) is None


# ---------------------------------------------------------------------------
# BundleService orchestration
# ---------------------------------------------------------------------------

class FakeHolding:
    def __init__(self, owner, percent):
        self.owner = owner
        self.percent = percent


class FakeHelius:
    def __init__(self, holdings):
        self._holdings = holdings

    async def get_top_holders(self, mint, limit=20):
        return self._holdings[:limit]


@pytest.mark.asyncio
async def test_service_excludes_custody_before_spending_lookups(monkeypatch):
    client = make_client()
    calls = []

    def handler(method, params):
        calls.append((method, params[0]))
        if method == "getSignaturesForAddress":
            return sig_entries(3)
        if method == "getTransaction":
            return tx_with_payer(FUNDER)
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    service = BundleService(
        FakeHelius([FakeHolding(VAULT, 60.0), FakeHolding(WALLET, 10.0)]),
        client, SETTINGS)
    stakes, origins, notes = await service.gather("Mint111111111111111111111111111111111111111")

    assert [s.wallet for s in stakes] == [WALLET]
    assert all(addr != VAULT for _, addr in calls), \
        "no RPC call may be spent on a custody account"
    assert any("custody" in n for n in notes)
    assert origins[WALLET].kind == FRESH
    # The funder is not a top holder, so its activity must have been classified.
    assert origins[WALLET].funder_is_infrastructure is False


@pytest.mark.asyncio
async def test_holder_funders_are_activity_checked_so_pass3_can_honour_infra(monkeypatch):
    """A funder who is himself a top holder IS activity-checked (review fix): a
    CEX/router that happens to be a top holder must carry a real infrastructure
    flag, or the engine's Pass 3 has nothing to honour and clusters independent
    withdrawers. The extra call per distinct funder is the accepted cost."""
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            return sig_entries(3)          # FUNDER is NOT high-activity here
        if method == "getTransaction":
            return tx_with_payer(FUNDER)   # funder IS the other top holder
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    service = BundleService(
        FakeHelius([FakeHolding(FUNDER, 50.0), FakeHolding(WALLET, 12.5)]),
        client, SETTINGS)
    stakes, origins, notes = await service.gather("Mint111111111111111111111111111111111111111")

    assert origins[WALLET].funder == FUNDER
    # Explicitly checked and found NOT infrastructure -> a real bundle edge.
    assert origins[WALLET].funder_is_infrastructure is False


@pytest.mark.asyncio
async def test_a_high_activity_holder_funder_is_flagged_infrastructure(monkeypatch):
    """The case the check exists for: the funder-holder saturates its page (a
    CEX), so its edge must be marked infrastructure, not a bundle."""
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            if params[0] == FUNDER:
                return sig_entries(_SIGNATURE_PAGE_LIMIT)   # busy CEX
            return sig_entries(3)
        if method == "getTransaction":
            return tx_with_payer(FUNDER)
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    service = BundleService(
        FakeHelius([FakeHolding(FUNDER, 40.0), FakeHolding(WALLET, 9.0)]),
        client, SETTINGS)
    stakes, origins, notes = await service.gather("Mint111111111111111111111111111111111111111")
    assert origins[WALLET].funder_is_infrastructure is True


@pytest.mark.asyncio
async def test_service_reports_empty_holder_list_honestly():
    service = BundleService(FakeHelius([]), make_client(), SETTINGS)
    stakes, origins, notes = await service.gather("Mint111111111111111111111111111111111111111")
    assert stakes == [] and origins == {}
    assert any("no holders" in n for n in notes)


def test_api_key_is_redacted_from_errors():
    client = FundingClient(SETTINGS, api_key="sekret-key",
                           rate_limiter=RateLimiter(10.0, burst=5))
    assert "sekret-key" not in client._scrub("x /?api-key=sekret-key boom")


@pytest.mark.asyncio
async def test_a_busy_sniper_is_paged_back_to_its_true_origin(monkeypatch):
    """THE fatal case, verified live 2026-07-30: a sniper wallet with 2,400+
    signatures at snipe time. A single page files it ESTABLISHED and misses the
    bundle; pagination to its true first tx recovers the funder edge."""
    client = make_client()
    calls = {"sigs": 0}
    # Two full pages then a short third — a ~2,400-sig wallet.
    pages = {
        None: sig_entries(_SIGNATURE_PAGE_LIMIT, "p0"),
        "p0999": sig_entries(_SIGNATURE_PAGE_LIMIT, "p1"),
        "p1999": [{"signature": "TRUE_OLDEST", "slot": 434969782}],
    }

    def handler(method, params):
        if method == "getSignaturesForAddress":
            calls["sigs"] += 1
            before = params[1].get("before")
            return pages[before]
        if method == "getTransaction":
            assert params[0] == "TRUE_OLDEST"
            return tx_with_payer(FUNDER)
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == FRESH, "must NOT be misfiled as established"
    assert origin.funder == FUNDER
    assert origin.first_signature == "TRUE_OLDEST"
    assert calls["sigs"] == 3


@pytest.mark.asyncio
async def test_a_wallet_still_full_after_the_page_bound_is_established(monkeypatch):
    """Past the bound the wallet is genuinely high-activity — no funder is
    claimed because its true first tx was never reached."""
    client = make_client()

    def handler(method, params):
        if method == "getSignaturesForAddress":
            # Always a full page, whatever the cursor: an infinite trader.
            return sig_entries(_SIGNATURE_PAGE_LIMIT, str(params[1].get("before")))
        raise AssertionError(f"must not call {method}")

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == ESTABLISHED
    assert origin.funder is None


@pytest.mark.asyncio
async def test_pagination_stops_at_an_empty_next_page(monkeypatch):
    """A full page followed by an empty one means the full page WAS the oldest."""
    client = make_client()
    pages = {None: sig_entries(_SIGNATURE_PAGE_LIMIT, "a"), "a999": []}

    def handler(method, params):
        if method == "getSignaturesForAddress":
            return pages[params[1].get("before")]
        if method == "getTransaction":
            return tx_with_payer(FUNDER)
        raise AssertionError(method)

    patch_rpc(monkeypatch, client, handler)
    origin = await client.get_wallet_origin(WALLET)
    assert origin.kind == FRESH
    assert origin.first_signature == "a999"
    assert origin.funder == FUNDER
