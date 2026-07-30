"""Tests for the on-chain holder-concentration and LP-burn collector.

The premise these guard (DECISIONS_LOG 2026-07-29, late night): GoPlus returns
no holder or LP data for a fresh mint, so the two facts that decide a rug were
`None` on 499 of the operator's 500 alerted coins. This collector supplies them
from chain — and the thing that must never happen is that it supplies a WRONG
one. Nearly every test below pins a refusal rather than a value.
"""

import base64
import struct

import pytest

from meme_intelligence.collectors.onchain_security import (
    OnChainSecurityCollector,
    encode_base58,
    is_off_curve,
)
from meme_intelligence.config.settings import OnChainSecuritySettings
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.rate_limiter import RateLimiter

MINT = "MintAddr1111111111111111111111111111111111"
POOL = "PoolAddr1111111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
RAYDIUM_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
RAYDIUM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Off-curve, and deliberately NOT in _CUSTODY_OWNERS: stands in for the vault
# authority of an AMM nobody has enumerated yet.
OFFCURVE_UNLISTED = "DYk2ooHQGWpS5J3MtycUs1PNKuSQhx1wAwkxaNk3zt9j"

# Synthetic owner addresses. These MUST be genuine ON-CURVE ed25519 points:
# the collector now treats an off-curve address as provably-not-a-wallet
# custody, so a made-up string like W_WHALE would be silently excluded and
# the test would pass for the wrong reason.
W_WHALE = "DGirC9hJVDMfidznPm3aoGMNQv9i5vJoX5CivyHeXwB1"
W_SMALL = "CpcfeMHQPg57epWopPGhBBw1P8t168XrN1XH7TBL7LHk"
W_BUYER1 = "5qH2nkxSZnhMG5KaBxpfVEfXFikRWBCHQAjxYTEgtwQr"
W_BUYER2 = "3eYnMWpNveAK1tA6ywhP2uZfoigW8NL3HycGrHwednXX"
W_CURVEPDA = "CgPkVvVLxiD45DjM4A3Q5eJozoemb1du5NdhLxNSn1eP"
W_DEV = "GepdsZTKRG56mt1qnrEXhNRRafTDNQpGMkns8EgXfwPk"
W_OTHER = "3SbCcQgjWXDQ4raLzEaE4d33oZ8kST9Txn3mHDqP9qJx"
W_REALHOLDER = "4yszLiJUZihSLP8pmAGBgXCBar2mB93YPDC1ToT93eAC"
W_REALWHALE = "2YyYjJYsuESE3jrirLugWhamd14mRstmcWxNNmqL5uKe"
W_PERPOOLPDA = "9DQZuoUxLaC7AkkCt5K1RqTp99pR9tgqCGuY8K8DVw8Z"
W_FRESHWALLET = "2nzTPL7EBhmE8XhBW8dRNzNTM5omACrG5oLQrNUgvX7R"
W_OWNER2 = "4EtCmgs4WCVGFThTH4vxR6uLe6nRvPXXjKo457u56FwT"
W_OWNER1 = "F4hQEnp74zoxiBrfV3YcuL7j9PkDobBCMapkw9CbZkZS"
W_MYSTERY = "rZ2MLKNeZALTZQCRT3NXDUAQP9sUBVHgcHWDfgQRL9T"
W_A = "DgHxBc1mGMQy72bivv9DUQ82ysJuvXsknsuc38LP2LE1"
W_B = "49XgaBak2nz64k4BFcgwV1y6QkEzMqePqiCfSXBN7jGt"
W_HOLDER = "2BnBAQbkw7Ym1s27MsYixUFAQxTzhEq64wAeCWU5DteV"

SETTINGS = OnChainSecuritySettings()


def make_collector(settings: OnChainSecuritySettings = SETTINGS, *,
                   api_key: str = "test-key"):
    """Defaults to a keyed collector: the holder census is only attempted
    against a keyed endpoint (see the keyless test below for why)."""
    return OnChainSecurityCollector(
        settings, api_key=api_key,
        rate_limiter=RateLimiter(1000.0, burst=100), cache=TTLCache())


def patch_rpc(monkeypatch, collector, handler):
    """Route the collector's RPC through ``handler(method, params)``."""
    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None, error_status_as_json=frozenset()):
        result = handler(json_body["method"], json_body["params"])
        if isinstance(result, Exception):
            raise result
        return {"jsonrpc": "2.0", "id": 1, "result": result}
    monkeypatch.setattr(collector, "_get_json", fake_get_json)


def supply(amount, decimals=6):
    return {"value": {"amount": str(amount), "decimals": decimals}}


def largest(*pairs):
    return {"value": [{"address": addr, "amount": str(amt)} for addr, amt in pairs]}


def token_accounts(*owners):
    return {"value": [{"data": {"parsed": {"info": {"owner": o}}}} if o else None
                      for o in owners]}


def owner_kinds(*programs):
    """getMultipleAccounts(base64) over OWNER addresses: which program owns each."""
    return {"value": [None if p is None else {"owner": p, "data": ["", "base64"]}
                      for p in programs]}


def raydium_pool_data(lp_mint_raw: bytes, lp_reserve: int, *, length: int = 752) -> str:
    """A synthetic pool account. ``length`` under 752 deliberately leaves the
    lpReserve field off the end — that IS the truncated-account case."""
    data = bytearray(length)
    if length >= 496:
        data[464:496] = lp_mint_raw
    if length >= 728:
        struct.pack_into("<Q", data, 720, lp_reserve)
    return base64.b64encode(bytes(data)).decode()


LP_MINT_RAW = bytes(range(32))
LP_MINT = encode_base58(LP_MINT_RAW)


# --------------------------------------------------------------------------
# base58 — a wrong encoder would silently produce the wrong mint address
# --------------------------------------------------------------------------

def test_base58_encoder_matches_known_solana_pubkeys():
    assert encode_base58(bytes(32)) == SYSTEM
    wsol = bytes.fromhex(
        "069b8857feab8184fb687f634618c035dac439dc1aeb3b5598a0f00000000001")
    assert encode_base58(wsol) == "So11111111111111111111111111111111111111112"
    usdc = bytes.fromhex(
        "c6fa7af3bedbad3a3d65f36aabc97431b1bbe4c2d2f6e0e47ca60203452f5d61")
    assert encode_base58(usdc) == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# --------------------------------------------------------------------------
# Holder concentration
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concentration_excludes_custody_and_aggregates_by_owner(monkeypatch):
    """The core computation: a whale split across two token accounts counts once,
    and the pool vault / incinerator / bonding curve are not holders."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_vault", 200_000), ("acc_whale_a", 60_000),
                           ("acc_whale_b", 40_000), ("acc_burn", 100_000),
                           ("acc_small", 10_000))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(RAYDIUM_AUTHORITY, W_WHALE, W_WHALE,
                                      INCINERATOR, W_SMALL)
            return owner_kinds(SYSTEM, SYSTEM)  # Whale, Small — both real wallets
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)

    # Whale = 60k + 40k = 100k of 1M = 10%, NOT two separate 6%/4% holders.
    assert facts.top_holder_percent == pytest.approx(10.0)
    assert facts.top10_holder_percent is None  # deliberately not emitted  # + Small 1%
    assert facts.census_owner_count == 2
    assert any(RAYDIUM_AUTHORITY in e for e in facts.excluded_owners)
    assert any(INCINERATOR in e for e in facts.excluded_owners)


@pytest.mark.asyncio
async def test_a_pumpfun_bonding_curve_is_excluded_by_being_program_owned(monkeypatch):
    """THE trap this module exists to avoid: the curve is not a holder. Counting
    it makes every healthy launch look 92% concentrated, which would block
    essentially every alert — silencing the operator."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000_000_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_curve", 400_000_000), ("acc_buyer1", 30_000_000),
                           ("acc_buyer2", 20_000_000))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_CURVEPDA, W_BUYER1, W_BUYER2)
            return owner_kinds(PUMP_PROGRAM, SYSTEM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)

    # 3% (the biggest REAL buyer), not 40%.
    assert facts.top_holder_percent == pytest.approx(3.0)
    assert facts.top10_holder_percent is None  # deliberately not emitted
    assert any("program-owned" in e for e in facts.excluded_owners)


@pytest.mark.asyncio
async def test_custody_dominating_supply_withholds_rather_than_deflating(monkeypatch):
    """The denominator trap. Custody leaves the numerator but not the
    denominator, so on a coin whose curve holds 85% a dev with 67% of the
    tradeable FLOAT reads as 10% "of circulating supply" — which the analyzer
    treats as healthy, converting an honest unknown into a false all-clear.
    Above the custody limit, neither figure is published."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000_000_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_curve", 850_000_000), ("acc_dev", 100_000_000),
                           ("acc_other", 50_000_000))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_CURVEPDA, W_DEV, W_OTHER)
            return owner_kinds(PUMP_PROGRAM, SYSTEM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)

    assert facts.top_holder_percent is None, (
        "10% of supply would read as healthy while being 67% of the float")
    assert facts.top10_holder_percent is None
    assert any("sits in custody" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_a_reported_figure_says_how_much_custody_was_excluded(monkeypatch):
    """Below the limit the number is published, but the deflation factor has to
    travel with it so the probe can show it."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_curve", 300), ("acc_dev", 200))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_CURVEPDA, W_DEV)
            return owner_kinds(PUMP_PROGRAM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent == pytest.approx(20.0)
    assert any("30.0% is in custody" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_an_owner_with_no_account_is_a_real_wallet_not_custody(monkeypatch):
    """A wallet that has never held SOL has no account. A PDA always has data —
    that is why it exists — so absence is not ambiguity, and treating it as
    custody would hide a genuine whale."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc1", 400))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_FRESHWALLET)
            return owner_kinds(None)   # account does not exist
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent == pytest.approx(40.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("authority,label", [
    ("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "Raydium AMM v4"),
    ("GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL", "Raydium CPMM"),
    ("HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC", "Meteora DAMM v2"),
    ("FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM", "Meteora DBC"),
    ("BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s", "pump.fun-era infra"),
])
def test_every_known_vault_authority_is_provably_not_a_wallet(authority, label):
    """The measured failure of the first design, turned into a guard.

    Each of these is System-owned with ZERO-LENGTH data, so the program-owned
    test cannot see any of them, and each was measured holding 50-94% of a live
    coin's supply. Counting them as whales cost 11 of 11 live coins every
    buy-side alert. All five are off-curve — an address that is not a valid
    ed25519 point cannot have a private key, so nobody holds it.
    """
    assert is_off_curve(authority), f"{label} authority must be provably keyless"


@pytest.mark.parametrize("wallet", [
    "ArB1hBWhRKxpqEykMN25cFrZdwUfg71FNUfh921PFapW",
    "6uRXCp3v13N4FKfMiU1oS7ztnnbNXPLWc9HSp6X8a5oq",
    "DvR7f6MMpTzRsmYHyBapogbBxSXt4g8sjskENsqamfro",
])
def test_real_holder_wallets_are_not_mistaken_for_custody(wallet):
    """The other direction, and the more dangerous one: excluding a real whale
    hides a rug. These three were observed holding real balances on chain."""
    assert not is_off_curve(wallet)


def test_an_unparseable_address_is_not_treated_as_custody():
    """"I could not check" must never read as "provably not a holder" — that
    would silently delete supply from the census (Rule 8)."""
    assert not is_off_curve("not-a-base58-address!!!")
    assert not is_off_curve("")


@pytest.mark.asyncio
async def test_an_off_curve_vault_authority_is_excluded_without_any_rpc_hint(
        monkeypatch):
    """End to end: the vault authority looks exactly like a plain wallet to every
    RPC-based test, and is still excluded."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_vault", 400_000), ("acc_holder", 20_000))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                # Deliberately NOT one of the listed authorities: this proves the
                # off-curve test stands on its own, so a vault belonging to some
                # AMM nobody has enumerated yet is still excluded.
                return token_accounts(OFFCURVE_UNLISTED, W_REALHOLDER)
            # System-owned with no data — indistinguishable from a person here.
            return owner_kinds(SYSTEM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)

    assert facts.top_holder_percent == pytest.approx(2.0), (
        "an unlisted AMM vault must not be reported as a 40% whale")
    assert any("off-curve" in e for e in facts.excluded_owners)


@pytest.mark.asyncio
async def test_a_rate_limited_census_reports_unknown_not_zero(monkeypatch):
    """getTokenLargestAccounts is disabled (deterministic 429) on the public
    endpoint. Treating that as 'no holder data, therefore fine' is precisely the
    bug that produced the 100/100 score — it must surface as unknown."""
    from meme_intelligence.core.errors import RateLimitedError
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000_000)
        return RateLimitedError("429: Too many requests for a specific RPC call")

    patch_rpc(monkeypatch, c, handler)
    facts = await c.collect(MINT, None)
    assert facts.top_holder_percent is None
    assert facts.top10_holder_percent is None
    assert not facts.has_any_fact
    assert any("census unavailable" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_a_pumpswap_pool_is_excluded_by_the_structural_rule(monkeypatch):
    """Modern pump.fun coins graduate to PumpSwap, whose pool authority is a
    PER-POOL PDA — no address list can enumerate them, so the program-owned rule
    is what catches these. Uses the real measured numbers from that live coin:
    the pool held 79.04% and the largest real wallet 3.49%.

    The pool IS correctly excluded — but because custody then exceeds the 50%
    limit, the figure is withheld rather than published as 3.49%. That is the
    intended outcome: 3.49% of total supply is 16.7% of the tradeable float, and
    publishing the smaller number would read as healthier than the coin is."""
    c = make_collector()
    pumpswap = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_pool", 790_427), ("acc_whale", 34_933))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_PERPOOLPDA, W_REALWHALE)
            return owner_kinds(pumpswap, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert any("program-owned" in e for e in facts.excluded_owners), (
        "the per-pool PDA must be recognised as custody")
    assert facts.top_holder_percent is None
    assert any("79.0% of supply sits in custody" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_an_unresolved_largest_account_withholds_both_figures(monkeypatch):
    """An account we cannot attribute could BE the top holder, so reporting the
    rest would under-state concentration — exactly the failure that lets a rug
    through. Withhold instead (Rule 8)."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc1", 800), ("acc2", 100))
        if method == "getMultipleAccounts":
            return token_accounts(None, W_OWNER2)   # first one unresolvable
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None
    assert facts.top10_holder_percent is None
    assert any("could not be resolved" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_a_short_owner_response_withholds_rather_than_truncating(monkeypatch):
    """getMultipleAccounts returning fewer entries than requested leaves trailing
    accounts unseen — the same under-count, by a quieter route."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc1", 500), ("acc2", 300), ("acc3", 100))
        if method == "getMultipleAccounts":
            return token_accounts(W_OWNER1)   # two missing
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None
    assert any("could not be resolved" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_an_unclassifiable_owner_withholds(monkeypatch):
    """If we cannot tell a wallet from a vault, guessing either way is wrong:
    'wallet' blocks healthy coins, 'custody' hides a rug."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc1", 900))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_MYSTERY)
            return {"value": [{"data": ["", "base64"]}]}   # no 'owner' field
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None
    assert any("could not be classified" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_all_custody_reports_no_holder_rather_than_zero(monkeypatch):
    """A pre-graduation coin whose whole supply sits in the curve has no visible
    holder. That is 'unknown', not '0% concentrated' (which would read as safe)."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_curve", 1_000))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_CURVEPDA)
            return owner_kinds(PUMP_PROGRAM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None
    assert facts.census_owner_count == 0
    assert any("every top account is custody" in n for n in facts.notes)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_supply", [supply(0), {"value": {}}, {}, None])
async def test_unreadable_supply_yields_no_concentration(monkeypatch, bad_supply):
    c = make_collector()
    patch_rpc(monkeypatch, c, lambda m, p: bad_supply if m == "getTokenSupply"
              else largest(("a", 1)))
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None
    assert any("supply" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_no_token_accounts_yields_nothing(monkeypatch):
    c = make_collector()
    patch_rpc(monkeypatch, c, lambda m, p: supply(1_000) if m == "getTokenSupply"
              else {"value": []})
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None
    assert not facts.has_any_fact


@pytest.mark.asyncio
async def test_malformed_amount_entries_withhold(monkeypatch):
    """A non-numeric amount is a hole in the census, not a zero balance."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "acc1", "amount": "not-a-number"},
                              {"address": "acc2", "amount": "300"}]}
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_OWNER2)
            return owner_kinds(SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent is None


@pytest.mark.asyncio
async def test_fewer_than_ten_real_holders_still_reports_and_says_so(monkeypatch):
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("a", 200), ("b", 100))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_A, W_B)
            return owner_kinds(SYSTEM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent == pytest.approx(20.0)
    assert facts.top10_holder_percent is None  # deliberately not emitted
    assert any("non-custody owners" in n for n in facts.notes)


# --------------------------------------------------------------------------
# LP burn
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lp_burn_reproduces_the_live_verified_numbers(monkeypatch):
    """The exact integers decoded live from Raydium SOL/USDC on 2026-07-30."""
    c = make_collector()
    lp_reserve, lp_supply = 55_465_149_717_186, 55_459_414_131_915

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, lp_reserve),
                                       "base64"]}}
        if method == "getTokenSupply":
            assert params[0] == LP_MINT, "must read the LP mint decoded at offset 464"
            return supply(lp_supply, decimals=9)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent == pytest.approx(0.0103, abs=0.0005)


@pytest.mark.asyncio
async def test_a_fully_burned_lp_reads_one_hundred(monkeypatch):
    """What a graduated pump.fun coin looks like: the LP mint supply is gone."""
    c = make_collector()

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 1_000_000),
                                       "base64"]}}
        if method == "getTokenSupply":
            return supply(0)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    assert (await c.get_lp_burned_percent(POOL)).lp_burned_percent == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_a_non_raydium_v4_pool_makes_no_lp_claim_at_all(monkeypatch):
    """THE scope trap. The verified offset is v4-only; on a CPMM/CLMM/Whirlpool/
    DLMM account it would decode an unrelated integer and print it as a
    percentage. A wrong number is worse than None."""
    c = make_collector()
    other = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"

    def handler(method, params):
        if method == "getAccountInfo":
            # Same 752 bytes, different program: length alone must not decide.
            return {"value": {"owner": other,
                              "data": [raydium_pool_data(LP_MINT_RAW, 12345), "base64"]}}
        raise AssertionError(f"must not call {method} on a non-v4 pool")

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("not Raydium AMM v4" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_a_v4_program_account_of_the_wrong_length_is_refused(monkeypatch):
    """Belt and braces behind the program check: only the 752-byte
    LiquidityStateV4 layout has lpReserve at 720."""
    c = make_collector()

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 999, length=624),
                                       "base64"]}}
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("752-byte" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_zero_lp_reserve_is_undefined_not_zero_burn(monkeypatch):
    """0/0 is not 0% burned. Reporting 0 would deduct 30 points for 'liquidity
    can be pulled' on a pool we know nothing about."""
    c = make_collector()

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 0), "base64"]}}
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("lpReserve is zero" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_supply_above_reserve_is_refused_not_clamped(monkeypatch):
    """A negative burn is not a small burn — it means the two numbers do not
    describe the same thing, so neither can be published."""
    c = make_collector()

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 1_000), "base64"]}}
        if method == "getTokenSupply":
            return supply(1_500)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("exceeds lpReserve" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_a_missing_pool_account_makes_no_claim(monkeypatch):
    c = make_collector()
    patch_rpc(monkeypatch, c, lambda m, p: {"value": None})
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("not found" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_unreadable_lp_mint_supply_makes_no_claim(monkeypatch):
    c = make_collector()

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 1_000), "base64"]}}
        if method == "getTokenSupply":
            return {"value": {}}
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("unreadable" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_undecodable_account_data_makes_no_claim(monkeypatch):
    c = make_collector()
    patch_rpc(monkeypatch, c, lambda m, p: {
        "value": {"owner": RAYDIUM_V4_PROGRAM, "data": ["!!!not base64!!!", "base64"]}})
    facts = await c.get_lp_burned_percent(POOL)
    assert facts.lp_burned_percent is None
    assert any("unreadable" in n for n in facts.notes)


# --------------------------------------------------------------------------
# collect() — the combined call the pipeline uses
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_returns_both_facts(monkeypatch):
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            if params[0] == LP_MINT:
                return supply(0)
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc1", 250))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_HOLDER)
            return owner_kinds(SYSTEM)
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 500), "base64"]}}
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.collect(MINT, POOL)
    assert facts.top_holder_percent == pytest.approx(25.0)
    assert facts.lp_burned_percent == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_a_failing_census_does_not_void_the_lp_read(monkeypatch):
    """Two independent facts; one provider hiccup must not erase the other."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            if params[0] == LP_MINT:
                return supply(0)
            return CollectorError("supply call exploded")
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 500), "base64"]}}
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.collect(MINT, POOL)
    assert facts.top_holder_percent is None
    assert facts.lp_burned_percent == pytest.approx(100.0)
    assert any("census unavailable" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_no_pool_address_still_runs_the_census(monkeypatch):
    """A pre-graduation pump.fun coin has no LP pool at all. That is a `None` LP
    fact, not a reason to skip holder concentration."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc1", 120))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_HOLDER)
            return owner_kinds(SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.collect(MINT, None)
    assert facts.top_holder_percent == pytest.approx(12.0)
    assert facts.lp_burned_percent is None
    assert any("no pool address" in n for n in facts.notes)


def test_the_collector_has_no_holder_count_field_to_leak():
    """getTokenLargestAccounts sees at most 20 accounts and cannot know the total
    holder population. `census_owner_count` is diagnostic only; there is
    deliberately no `holder_count` for the pipeline to copy onto
    SecurityProfile, because that number would be fabricated (Rule 8)."""
    from meme_intelligence.collectors.onchain_security import OnChainSecurityFacts
    assert not hasattr(OnChainSecurityFacts(), "holder_count")
    assert "census_owner_count" in OnChainSecurityFacts.__dataclass_fields__


# --------------------------------------------------------------------------
# Secrets and construction
# --------------------------------------------------------------------------

def test_an_api_key_is_redacted_from_errors():
    c = make_collector(api_key="super-secret-key")
    assert "super-secret-key" not in c._scrub(
        "boom on https://x/?api-key=super-secret-key")


def test_without_a_key_it_falls_back_to_public_rpc():
    c = make_collector(api_key="")
    assert c._base_url == SETTINGS.fallback_rpc_url
    assert c._path == "/"


def test_with_a_key_it_uses_helius_and_carries_the_key_in_the_path():
    c = make_collector(api_key="k")
    assert "helius" in c._base_url
    assert c._path == "/?api-key=k"


def test_no_key_and_no_fallback_is_a_configuration_error():
    with pytest.raises(ValueError):
        make_collector(OnChainSecuritySettings(fallback_rpc_url=""), api_key="")


@pytest.mark.asyncio
async def test_a_keyless_census_refuses_up_front_without_spending_retries(monkeypatch):
    """getTokenLargestAccounts is DISABLED on public RPC (429 with
    x-ratelimit-method-limit: 0), not throttled. Attempting it burns the whole
    retry ladder — ~30s per coin — for a guaranteed failure, inside the scan
    cycle. On a 1 vCPU droplet that stalls the scanner and Telegram together, so
    it must not even be tried."""
    c = make_collector(api_key="")
    calls: list[str] = []

    def handler(method, params):
        calls.append(method)
        raise AssertionError(f"no RPC call should be made, got {method}")

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert calls == []
    assert facts.top_holder_percent is None
    assert any("keyed RPC endpoint" in n for n in facts.notes)


@pytest.mark.asyncio
async def test_a_keyless_collector_can_still_read_lp_burn(monkeypatch):
    """Only the census method is blocked; getAccountInfo and getTokenSupply work
    fine on public RPC, so the LP half stays usable without a key."""
    c = make_collector(api_key="")

    def handler(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": RAYDIUM_V4_PROGRAM,
                              "data": [raydium_pool_data(LP_MINT_RAW, 1_000), "base64"]}}
        if method == "getTokenSupply":
            return supply(0)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.collect(MINT, POOL)
    assert facts.lp_burned_percent == pytest.approx(100.0)
    assert facts.top_holder_percent is None


@pytest.mark.asyncio
async def test_the_top_holder_owner_is_reported_so_a_human_can_check_it(monkeypatch):
    """The probe's STOP verdict asks "is an AMM vault being counted as a holder?"
    and that question is unanswerable without the address. Field failure
    2026-07-30: the operator got the warning and no way to act on it."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            return largest(("acc_big", 400), ("acc_small", 100))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_WHALE, W_SMALL)
            return owner_kinds(SYSTEM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent == pytest.approx(40.0)
    assert facts.top_holder_owner == W_WHALE, (
        "the address behind the percentage must travel with it")


@pytest.mark.asyncio
async def test_the_reported_owner_is_the_largest_one_not_merely_the_first(monkeypatch):
    """Guards the ranking: dict order is insertion order, which is
    largest-token-account order, NOT largest-OWNER order once balances are
    aggregated across accounts."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            # W_SMALL leads on a single account, but W_WHALE totals more.
            return largest(("acc_a", 300), ("acc_b", 200), ("acc_c", 200))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_SMALL, W_WHALE, W_WHALE)
            return owner_kinds(SYSTEM, SYSTEM)
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)
    assert facts.top_holder_percent == pytest.approx(40.0)
    assert facts.top_holder_owner == W_WHALE


@pytest.mark.asyncio
async def test_top10_is_never_emitted_because_it_is_degenerate_here(monkeypatch):
    """Measured live 2026-07-30, not reasoned about: 6 of 8 sampled coins had <=12
    real holders (median 7 on fresh ones). With fewer than ten holders the "top
    ten" is every holder, so top10-of-float was exactly 100.0000% on all six —
    zero variance. Against total supply it equalled (100 - custody share) to four
    decimals, i.e. it measures graduation progress, not distribution. And it is
    anti-informative: one coin reported 8.33% and PASSED, and thirty minutes
    later its three largest accounts were closed with 99.9% of supply in the
    pool. A value with no information that can still cross a threshold is a
    fabricated fact."""
    c = make_collector()

    def handler(method, params):
        if method == "getTokenSupply":
            return supply(1_000)
        if method == "getTokenLargestAccounts":
            # Six real holders — exactly the shape where top10 saturates.
            return largest(("a", 300), ("b", 200), ("c", 200),
                           ("d", 150), ("e", 100), ("f", 50))
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return token_accounts(W_A, W_B, W_WHALE, W_SMALL, W_DEV, W_OTHER)
            return owner_kinds(*([SYSTEM] * 6))
        raise AssertionError(method)

    patch_rpc(monkeypatch, c, handler)
    facts = await c.get_holder_concentration(MINT)

    assert facts.top_holder_percent == pytest.approx(30.0), "top-1 is still real"
    assert facts.top10_holder_percent is None, (
        "top10 would be 100% of every coin with under ten holders — a threshold "
        "crossing carrying no information")
