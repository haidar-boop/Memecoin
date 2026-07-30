"""Holder concentration and LP burn status read straight off the Solana chain.

Why this module exists
---------------------
GoPlus — the security collector that fills :class:`SecurityProfile` — returns no
holder distribution and no LP data for a fresh pump.fun mint. Measured on the
operator's live database: **499 of 500 coins he was alerted about** had only the
``contract`` sub-score resolved, so the security score was a constant ~100 that
passed 100% of buy-side alerts, and two written-and-weighted ``RugEngine``
signals (``top_holder_concentration``, ``liquidity_unlocked``) had never once
received an input. The facts were missing, not the scoring: with the real
numbers present the existing analyzer separates a known-bad coin (73.8, blocked)
from a known-good one (100.0) by 26 points on its own.

This module supplies those facts. Nothing downstream changes shape.

What it will and will not claim (Rule 8)
---------------------------------------
* **Concentration excludes custody, not just burns.** Raw largest-account
  balances are NOT holder concentration. An AMM's pool vault, the incinerator,
  and a pump.fun bonding curve routinely hold most of a young coin's supply and
  are not "a holder". Counting them makes every healthy coin look ~90%
  concentrated, which would silence the operator's alerts — the single highest
  risk in this work, and a review measured the first version of this module
  doing exactly that on 11 of 11 live coins. Exclusion therefore runs three
  independent tests, and all three are needed:
  1. **off-curve** (:func:`is_off_curve`) — an address that is not a valid
     ed25519 point provably has no private key, so nobody holds it. This is what
     catches AMM vault authorities, which are *System-owned with zero-length
     data* and so invisible to test 3.
  2. **known address** (``_CUSTODY_OWNERS``) — covers the System Program
     address, which is on-curve and so invisible to test 1.
  3. **program-owned account** — catches state-holding custody: pump.fun bonding
     curves and PumpSwap pools, whose authorities are per-pool PDAs that no
     address list could enumerate.
* **When custody dominates supply, no concentration is reported.** See
  ``max_custody_share_for_concentration``: a share-of-total-supply figure on a
  coin whose curve holds 85% understates real concentration several-fold and
  would read as healthy.
* **A partial census reports nothing.** If any of the largest accounts cannot be
  resolved to an owner, the concentration figures are withheld rather than
  understated: an unresolved account could itself be the top holder.
* **``holder_count`` is never filled from here.** ``getTokenLargestAccounts``
  returns at most 20 accounts and cannot see the total holder population.
  Reporting the census size as a holder count would be a fabricated number.
* **The LP read is Raydium AMM v4 ONLY.** The verified ``lpReserve`` offset is
  meaningless in CPMM / CLMM / Whirlpool / DLMM, where it would decode an
  unrelated integer and print it as a percentage. Every other pool program, and
  every pre-graduation pump.fun coin (no pool at all), returns ``None``.
* **What it measures is BURN, which is a subset of "locked".** Burned LP needs
  no trust and is arithmetic; verifying a third-party *lock* needs that locker's
  account layout, which this does not read. See ``report_zero_burn_as_unlocked``.

Byte layout — verified live, not taken from documentation
--------------------------------------------------------
Decoded from a live mainnet Raydium AMM v4 pool (SOL/USDC
``58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2``) and cross-checked so that a
wrong offset could not pass:

===========================  ======  ==================================================
field                        offset  independent check that it is right
===========================  ======  ==================================================
account length                  752  program owner is the AMM v4 program id
base_vault                      336  the vault's own ``mint`` equals base_mint@400
quote_vault                     368  the vault's own ``mint`` equals quote_mint@432
base_mint                       400  decodes to wSOL on a known SOL/USDC pool
quote_mint                      432  decodes to USDC on that same pool
lp_mint                         464  ``getTokenSupply`` resolves it (garbage would not)
lp_reserve (u64 LE)             720  55465149717186 vs supply 55459414131915
===========================  ======  ==================================================

A wrong offset yields wildly different garbage rather than a set of mutually
consistent cross-checks, which is what makes this trustworthy.

Independently re-verified 2026-07-30 across five live v4 pools (SOL/USDC, WIF,
POPCAT, RAY, stSOL) — all 752 bytes under that program id. A survey of 25 other
Solana AMM/DEX programs found no 752-byte accounts (CPMM 637, Whirlpool 653,
Meteora DLMM 904, Meteora DAMM v2 1112, CLMM 1544, PumpSwap 300), but four
programs could not be scanned, so length is treated as a SECOND check behind the
program-id check rather than as proof of uniqueness.

Measured scope limits — what this layer can and cannot see today
--------------------------------------------------------------
Verified live rather than assumed. These are honest gaps, not TODOs hidden in a
docstring; ``deploy/onchain_facts_probe.py`` reports them per coin.

* **The census needs a Helius key.** ``getTokenLargestAccounts`` is *disabled*
  on the public endpoint — ``api.mainnet-beta.solana.com`` answers HTTP 429 with
  ``x-ratelimit-method-limit: 0`` deterministically, while cheap calls on the
  same connection succeed. That surfaces as "concentration unknown", never as a
  number and never as zero. Without a key, only the LP read is usable.
* **LP burn covers Raydium AMM v4 only, and modern pump.fun coins graduate to
  PumpSwap** (program ``pAMMBay6…``, a 300-301 byte account), so their LP status
  reports ``None``. The pAMM layout appears to keep an equivalent ``lp_supply``
  at offset 203 — arithmetically consistent on one pool, and explicitly NOT
  validated as the same *semantic* field. It is deliberately not implemented:
  an unvalidated formula is how a wrong number ships.
* **A burned LP is not liquidity safety.** Measured on the operator's own
  screenshot coin: LP genuinely 100% burned, and the pool still lost 90.97% of
  its SOL to holders dumping. Burning stops a rug-by-withdrawal; it does nothing
  about a rug-by-dump, which the holdings guard is what actually covers.
* **Most current pump.fun mints are Token-2022** (70 of 82 sampled). The census
  reads mint-indexed RPC methods, which are token-program agnostic, so this is
  expected to work — but it could not be exercised end-to-end here, because the
  one method needed is the one the public endpoint blocks.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.config.settings import OnChainSecuritySettings
from meme_intelligence.core.errors import CollectorError

# --- Solana / protocol constants (protocol facts, not tunables — kept here
# rather than in settings for the same reason `_TOKEN_PROGRAM` lives in
# trading/solana_rpc.py: a configurable byte offset invites someone to set a
# wrong one, and a wrong offset prints confident garbage.) ---

_SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Addresses that hold supply but are provably not a holder.
_BURN_OWNERS = frozenset({
    "1nc1nerator11111111111111111111111111111111",  # the community incinerator
    _SYSTEM_PROGRAM,
})

# Raydium AMM v4. The authority is a single program-wide constant that owns
# EVERY v4 pool's base and quote vaults (verified live on SOL/USDC).
_RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
_RAYDIUM_AMM_V4_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
_RAYDIUM_V4_ACCOUNT_LEN = 752

_OFF_BASE_VAULT = 336
_OFF_QUOTE_VAULT = 368
_OFF_BASE_MINT = 400
_OFF_QUOTE_MINT = 432
_OFF_LP_MINT = 464
_OFF_LP_RESERVE = 720

# Custody authorities excluded by ADDRESS.
#
# This list is now a belt-and-braces layer, NOT the primary defence — the
# off-curve test (:func:`is_off_curve`) catches every one of these vault
# authorities structurally, plus the ones nobody thought to list. The history is
# worth keeping because it shows why one rule was not enough:
#
# The first version of this module relied only on "the owner's account is owned
# by a program". Every AMM vault authority defeats that test — they are
# System-owned with ZERO-LENGTH data, structurally identical to a person,
# because they are signer-only PDAs that never store state. A review measured
# the consequence live: 11 of 11 fresh tradeable coins lost every buy-side
# alert, because vault authorities for Raydium v4 (5Q544fKr…), Raydium CPMM
# (GpMZbSM2…), Meteora DAMM v2 (HLnpSz9h…, which owns 390k token accounts) and
# Meteora DBC (FhVo3mqL…) were each counted as a whale holding 50-94% of supply.
# That is the silence-the-operator failure, reproduced.
#
# All four are OFF-CURVE, as is the pump.fun-era infrastructure address
# BwWK17cb… (46,732 SOL), while three known real holder wallets are on-curve —
# verified live 2026-07-30. So the off-curve test covers them all without a list
# to maintain. The entries below are kept for the one case it cannot cover: the
# System Program address is a valid curve point.
_CUSTODY_OWNERS = frozenset({
    _RAYDIUM_AMM_V4_AUTHORITY,
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",  # Meteora DAMM v2 authority
    "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM",  # Meteora DBC authority
    "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",  # pump.fun-era infrastructure
}) | _BURN_OWNERS

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# ed25519 curve parameters, for the off-curve custody test below.
_ED25519_P = 2**255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P


def encode_base58(raw: bytes) -> str:
    """Base58-encode 32 raw bytes into a Solana pubkey string.

    Local implementation rather than a dependency: the runtime already refuses
    to grow its footprint for the 1 GB droplet, and this is 12 lines.
    """
    number = int.from_bytes(raw, "big")
    out: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        out.append(_B58_ALPHABET[remainder])
    for byte in raw:
        if byte != 0:
            break
        out.append("1")
    return "".join(reversed(out))


def decode_base58(address: str) -> bytes | None:
    """32 raw bytes from a Solana address string, or ``None`` if it isn't one."""
    number = 0
    for char in address:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            return None
        number = number * 58 + index
    try:
        return number.to_bytes(32, "big")
    except OverflowError:
        return None


def is_off_curve(address: str) -> bool:
    """True when ``address`` is provably NOT anyone's wallet.

    A Solana wallet address IS an ed25519 public key, so it must decompress to a
    valid curve point. Program-derived addresses are, by construction, chosen so
    that they do *not* — that is precisely how a program proves an address has no
    private key behind it. So "off the curve" is not a heuristic: it is proof
    that no keypair exists and therefore that no person holds this.

    This is the load-bearing custody test, and it needs no RPC call and no
    maintained address list. Verified live 2026-07-30 against the vault
    authorities that were silently breaking the previous design — Raydium AMM v4
    ``5Q544fKr…``, Raydium CPMM ``GpMZbSM2…``, Meteora DAMM v2 ``HLnpSz9h…``,
    Meteora DBC ``FhVo3mqL…``, and the pump.fun-era infrastructure address
    ``BwWK17cb…`` (46,732 SOL) — all five are off-curve, while three known real
    holder wallets are on-curve. Every one of those five is System-owned with
    zero-length data, so the program-owned test below cannot see any of them.

    Unparseable input returns False: "I could not check" must not read as
    "provably not a holder", which would silently delete supply from the census.
    """
    raw = decode_base58(address)
    if raw is None:
        return False
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _ED25519_P:
        return True           # not a canonical field element: no such point
    numerator = (y * y - 1) % _ED25519_P
    denominator = (_ED25519_D * y * y + 1) % _ED25519_P
    if denominator == 0:
        return True
    x2 = numerator * pow(denominator, _ED25519_P - 2, _ED25519_P) % _ED25519_P
    if x2 == 0:
        return sign != 0
    x = pow(x2, (_ED25519_P + 3) // 8, _ED25519_P)
    if x * x % _ED25519_P != x2:
        x = x * pow(2, (_ED25519_P - 1) // 4, _ED25519_P) % _ED25519_P
        if x * x % _ED25519_P != x2:
            return True       # no square root: the point does not exist
    return False


@dataclass(frozen=True)
class OnChainSecurityFacts:
    """What one census actually established. Every field may be ``None``.

    ``None`` means "not established" — never "zero" and never "safe". The
    ``notes`` carry why, so a probe or a log line can explain a withheld number
    instead of leaving it silently absent (Rule 13).
    """

    top_holder_percent: float | None = None
    top10_holder_percent: float | None = None
    # The owner address behind ``top_holder_percent``, so a human can check
    # whether it is a person or an undetected vault. The probe's "STOP, check
    # whether an AMM vault is being counted as a holder" message was unusable
    # without this: it named the risk and withheld the one fact needed to settle
    # it (found in the field, 2026-07-30).
    top_holder_owner: str | None = None
    # Named for what it IS. The pipeline maps this onto SecurityProfile's
    # `lp_locked_percent`, whose analyzer semantics are "locked OR burned".
    lp_burned_percent: float | None = None
    # How many distinct non-custody owners the census saw. Diagnostic only —
    # explicitly NOT a holder count, and never written to SecurityProfile
    # (getTokenLargestAccounts sees at most 20 accounts).
    census_owner_count: int | None = None
    excluded_owners: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_any_fact(self) -> bool:
        return (self.top_holder_percent is not None
                or self.top10_holder_percent is not None
                or self.lp_burned_percent is not None)


def _as_int(value: Any) -> int | None:
    """Parse a raw token amount, which the RPC sends as a decimal STRING."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class OnChainSecurityCollector(BaseCollector):
    """Reads holder concentration and LP burn state from Solana RPC."""

    def __init__(
        self,
        settings: OnChainSecuritySettings,
        *,
        api_key: str = "",
        rpc_url: str = "https://mainnet.helius-rpc.com",
        **kwargs: Any,
    ) -> None:
        """``api_key`` empty falls back to ``settings.fallback_rpc_url`` (public
        RPC) so the layer degrades instead of going dark (Rule 9)."""
        url = rpc_url if api_key else settings.fallback_rpc_url
        if not url:
            raise ValueError(
                "OnChainSecurityCollector needs either a Helius api_key or a "
                "fallback_rpc_url")
        kwargs.setdefault("name", "onchain_security")
        kwargs.setdefault("base_url", url)
        kwargs.setdefault("timeout_seconds", settings.timeout_seconds)
        if api_key:
            # The key rides in the URL query (Helius has no header auth), so it
            # must be scrubbed from every raised/logged message (Rule 16).
            kwargs.setdefault("redact", (api_key,))
        super().__init__(**kwargs)
        self._s = settings
        self._api_key = api_key
        self._path = f"/?api-key={api_key}" if api_key else "/"

    # ---- helpers ----

    @staticmethod
    def _digest(mint: str, addresses: list[str]) -> str:
        """Stable fingerprint of the exact request a cached response answers.

        sha256 is used as a content fingerprint, not for security — it just keeps
        the key bounded and collision-free so a cached response can never be
        zipped against a different account list.
        """
        payload = f"{mint}|{'|'.join(addresses)}".encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _parsed_owner(info: Any) -> str | None:
        """Owner wallet from a jsonParsed token account, or ``None``.

        ``data`` is a dict when the node parsed the account and the documented
        ``[base64, "base64"]`` LIST when it could not — a list has no ``.get``,
        so the obvious chained access raises AttributeError. That escaped
        ``collect``'s CollectorError handler, propagated through the pipeline and
        aborted the whole scan cycle at that coin (review finding). Reachable
        with a Token-2022 extension a validator's parser does not know, and most
        current pump.fun mints are Token-2022.
        """
        if not isinstance(info, dict):
            return None
        data = info.get("data")
        if not isinstance(data, dict):
            return None
        parsed = data.get("parsed")
        if not isinstance(parsed, dict):
            return None
        fields = parsed.get("info")
        if not isinstance(fields, dict):
            return None
        owner = fields.get("owner")
        return owner if isinstance(owner, str) and owner else None

    # ---- RPC plumbing ----

    async def _rpc(self, method: str, params: list, *, cache_key: str | None = None,
                   cache_ttl: float | None = None) -> Any:
        payload = await self._get_json(
            self._path, cache_key=cache_key, cache_ttl=cache_ttl,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected a JSON object from RPC")
        if "error" in payload:
            error = payload["error"]
            message = (str(error.get("message", ""))[:200]
                       if isinstance(error, dict) else str(error)[:200])
            raise CollectorError(f"{self.name}: RPC error on {method}: {message}")
        return payload.get("result")

    # ---- Holder concentration ----

    async def get_holder_concentration(self, mint: str) -> OnChainSecurityFacts:
        """Top-1 and top-10 holder share of supply, custody excluded.

        Returns facts with ``None`` percentages (and a note saying why) whenever
        the census cannot be trusted. Four RPC calls at most, all cacheable.
        """
        if not mint:
            raise ValueError("mint must be non-empty")

        if not self._api_key:
            # getTokenLargestAccounts is DISABLED on the public endpoint, not
            # merely throttled: it answers 429 with x-ratelimit-method-limit: 0
            # every time, while cheap calls on the same connection succeed
            # (verified live 2026-07-30, and again by running
            # deploy/onchain_facts_probe.py). Attempting it anyway spends the
            # full retry ladder — 4 attempts, ~30s of backoff — on a guaranteed
            # failure, per coin, inside the scan cycle's gather. On a 1 vCPU
            # droplet that stalls the scanner and the Telegram poller together.
            # Refusing up front is the same honest "unknown" for zero cost.
            return OnChainSecurityFacts(
                notes=("holder concentration needs a keyed RPC endpoint — "
                       "getTokenLargestAccounts is disabled on public RPC "
                       "(set MEMEINTEL_HELIUS_API_KEY)",))

        supply_result = await self._rpc(
            "getTokenSupply", [mint],
            cache_key=f"onchain:supply:{mint}", cache_ttl=300.0)
        supply = _as_int(((supply_result or {}).get("value") or {}).get("amount"))
        if supply is None or supply <= 0:
            return OnChainSecurityFacts(
                notes=("total supply unreadable or zero — concentration undefined",))

        largest = await self._rpc(
            "getTokenLargestAccounts", [mint],
            cache_key=f"onchain:largest:{mint}", cache_ttl=120.0)
        raw_accounts = (largest or {}).get("value") or []
        if not raw_accounts:
            return OnChainSecurityFacts(
                notes=("no token accounts returned — nothing to measure",))

        # Keep the account objects whose address AND amount both parsed, so the
        # owner response (which corresponds positionally to the addresses we
        # send) is zipped against exactly the accounts that produced it. Zipping
        # a filtered response against an unfiltered list misattributes a balance
        # to the wrong wallet — the fabricated-whale bug already fixed once in
        # wallet_data.get_top_holders.
        accounts: list[tuple[str, int]] = []
        malformed = 0
        for entry in raw_accounts[: self._s.top_accounts_limit]:
            address = entry.get("address") if isinstance(entry, dict) else None
            amount = _as_int(entry.get("amount")) if isinstance(entry, dict) else None
            if not address or amount is None:
                malformed += 1
                continue
            accounts.append((address, amount))
        if not accounts:
            return OnChainSecurityFacts(
                notes=("every largest-account entry was malformed",))

        # Cache keys carry a digest of the EXACT request they answer. Keying by
        # length plus first element let a stale response be zipped positionally
        # against a different, fresher account list — the fabricated-whale bug
        # already fixed twice in wallet_data.get_top_holders, and a review caught
        # this module repeating it.
        accounts_key = self._digest(mint, [address for address, _ in accounts])
        owners_result = await self._rpc(
            "getMultipleAccounts",
            [[address for address, _ in accounts], {"encoding": "jsonParsed"}],
            cache_key=f"onchain:owners:{accounts_key}",
            cache_ttl=120.0)
        owner_infos = (owners_result or {}).get("value") or []

        by_owner: dict[str, int] = {}
        unresolved = 0
        for (address, amount), info in zip(accounts, owner_infos, strict=False):
            owner = self._parsed_owner(info)
            if not owner:
                unresolved += 1
                continue
            by_owner[owner] = by_owner.get(owner, 0) + amount
        # A response shorter than the request leaves trailing accounts unseen.
        unresolved += max(0, len(accounts) - len(owner_infos))

        if unresolved or malformed:
            # An unresolved account could itself be the top holder, so any hole
            # makes both figures a potential UNDER-count. Under-reporting
            # concentration is exactly the failure that lets a rug through, so
            # withhold rather than understate (Rule 8).
            return OnChainSecurityFacts(
                notes=(f"{unresolved + malformed} of {len(accounts)} largest accounts "
                       f"could not be resolved to an owner — concentration withheld "
                       f"rather than under-reported",))

        excluded: list[str] = []
        custody_by_address: set[str] = set()
        for owner in by_owner:
            if owner in _CUSTODY_OWNERS:
                custody_by_address.add(owner)
                excluded.append(f"{owner} (known custody/burn address)")
            elif is_off_curve(owner):
                # Provably keyless: no private key can exist for an off-curve
                # address, so nobody holds this. Catches every AMM vault
                # authority without an address list to maintain.
                custody_by_address.add(owner)
                excluded.append(f"{owner} (off-curve: provably not a wallet)")

        remaining = [owner for owner in by_owner if owner not in custody_by_address]
        program_owned: set[str] = set()
        if remaining and self._s.exclude_program_owned_accounts:
            program_owned, classify_failed = await self._program_owned(remaining)
            if classify_failed:
                # Same reasoning as an unresolved account: an unclassified owner
                # might be a bonding curve holding 90% of supply. Guessing
                # "real holder" would block healthy coins; guessing "custody"
                # would hide a rug. Report neither.
                return OnChainSecurityFacts(
                    notes=(f"{len(classify_failed)} top owner(s) could not be "
                           f"classified as wallet vs program-owned custody — "
                           f"concentration withheld",))
            for owner in program_owned:
                excluded.append(f"{owner} (program-owned: custody, not a holder)")

        held = {owner: amount for owner, amount in by_owner.items()
                if owner not in custody_by_address and owner not in program_owned}
        if not held:
            return OnChainSecurityFacts(
                census_owner_count=0,
                excluded_owners=tuple(sorted(excluded)),
                notes=("every top account is custody (pool vault, burn, or bonding "
                       "curve) — no real holder is visible in the top accounts",))

        # THE DENOMINATOR PROBLEM (review finding, 2026-07-30). Custody is
        # removed from the numerator but `supply` is the total, so on a coin
        # whose curve or pool holds most of the supply the reported figure is
        # deflated several-fold: a dev holding 67% of the tradeable float can
        # read as 10% "of circulating supply", which is how the analyzer prints
        # it. That turns an honest "distribution unverified" into an affirmative
        # "distribution healthy" — the one direction Rule 8 forbids, and it also
        # puts the top-10 thresholds arithmetically out of reach.
        #
        # Reporting against the float instead would inflate every pre-graduation
        # coin and risks the silencing failure, so neither number is published
        # when custody dominates: no number is the honest answer, and it is the
        # answer the probe can act on.
        custody_amount = sum(amount for owner, amount in by_owner.items()
                             if owner not in held)
        custody_share = 100.0 * custody_amount / supply
        if custody_share > self._s.max_custody_share_for_concentration:
            return OnChainSecurityFacts(
                census_owner_count=len(held),
                excluded_owners=tuple(sorted(excluded)),
                notes=(f"{custody_share:.1f}% of supply sits in custody (pool vault "
                       f"or bonding curve), above the "
                       f"{self._s.max_custody_share_for_concentration:.0f}% limit — a "
                       f"share-of-total-supply figure would understate real "
                       f"concentration several-fold, so none is reported",))

        ranked = sorted(held.items(), key=lambda item: item[1], reverse=True)
        amounts = [amount for _, amount in ranked]
        top_percent = 100.0 * amounts[0] / supply
        notes: list[str] = []
        if custody_share > 0.0:
            notes.append(f"measured against total supply; {custody_share:.1f}% is in "
                         f"custody and excluded from the numerator")
        if len(amounts) < 10:
            # top10 over fewer than 10 real holders is still the correct share
            # of supply held by the top ten (there just are not ten), but say so.
            notes.append(f"only {len(amounts)} non-custody owners in the top "
                         f"{len(accounts)} accounts")
        # top10_holder_percent is deliberately NOT emitted. It is arithmetically
        # degenerate on the coins this bot actually sees, measured live rather
        # than argued (2026-07-30, full censuses verified against getTokenSupply):
        #
        #   * 6 of 8 sampled coins had <= 12 real non-custody holders; median 7
        #     for the fresh cluster. With fewer than ten holders the "top ten"
        #     IS every holder, so top10-of-float came out at exactly 100.0000%
        #     on all six — zero variance, no distributional content.
        #   * Measured against total supply it equalled (100 - custody share) to
        #     four decimal places on those same six coins. It does not measure
        #     concentration, it measures how far through graduation a coin is.
        #   * It is worse than uninformative on a thin coin. One coin reported
        #     8.33% (distribution 100/100, PASS) and thirty minutes later its
        #     three largest token accounts were CLOSED and 99.9% of supply sat
        #     in the pool. The figure certified healthy distribution immediately
        #     before every holder left.
        #
        # A number with no information that can still cross a threshold is a
        # fabricated fact (Rule 8), so the field stays None and the analyzer's
        # top10 thresholds get no input from this layer. The honest replacement
        # is a real holder_count from a full census — see the module docstring.
        return OnChainSecurityFacts(
            top_holder_percent=min(100.0, top_percent),
            top_holder_owner=ranked[0][0],
            census_owner_count=len(held),
            excluded_owners=tuple(sorted(excluded)),
            notes=tuple(notes),
        )

    async def _program_owned(self, owners: list[str]) -> tuple[set[str], list[str]]:
        """Split owner addresses into program-owned (custody) and plain wallets.

        A state-holding PDA — a pump.fun bonding curve, a PumpSwap pool — is
        owned by the program that created it, which is what makes it detectable
        here (verified live on three separate curves and a PumpSwap pool).

        Two live counterexamples bound what this test can do, and both were
        measured rather than reasoned about:

        * **A signer-only PDA looks exactly like a wallet.** Raydium's v4 vault
          authority is System-owned with zero-length data. Nothing structural
          separates it from a person. That is why ``_CUSTODY_OWNERS`` exists and
          must not be removed.
        * **A real wallet may have no account at all.** A holder whose lamports
          went to zero returns ``null`` here while still holding tokens
          (observed at 1.30% and 0.35% of two different supplies). Absence is
          therefore treated as a wallet: calling it custody would silently
          delete genuine holders and UNDER-state concentration, which is the
          direction that lets a rug through.
        """
        result = await self._rpc(
            "getMultipleAccounts", [owners, {"encoding": "base64"}],
            cache_key=f"onchain:ownerkind:{self._digest('', owners)}", cache_ttl=120.0)
        infos = (result or {}).get("value") or []
        program_owned: set[str] = set()
        failed: list[str] = []
        for owner, info in zip(owners, infos, strict=False):
            if info is None:
                continue  # no account = a wallet that never held SOL
            if not isinstance(info, dict):
                failed.append(owner)
                continue
            program = info.get("owner")
            if not program:
                failed.append(owner)
                continue
            if program != _SYSTEM_PROGRAM:
                program_owned.add(owner)
        failed.extend(owners[len(infos):])
        return program_owned, failed

    # ---- LP burn status ----

    async def get_lp_burned_percent(self, pool_address: str) -> OnChainSecurityFacts:
        """Percentage of a Raydium AMM v4 pool's LP tokens that were burned.

        ``burned% = (lpReserve - currentLpMintSupply) / lpReserve * 100``.
        Returns ``None`` — never a number — for any pool this formula does not
        describe.
        """
        if not pool_address:
            raise ValueError("pool_address must be non-empty")

        result = await self._rpc(
            "getAccountInfo", [pool_address, {"encoding": "base64"}],
            cache_key=f"onchain:pool:{pool_address}", cache_ttl=120.0)
        value = (result or {}).get("value")
        if not isinstance(value, dict):
            return OnChainSecurityFacts(
                notes=(f"pool account {pool_address[:8]}… not found — no LP claim",))

        program = value.get("owner")
        if program != _RAYDIUM_AMM_V4_PROGRAM:
            # The decisive guard. Applying the v4 offset to a CPMM / CLMM /
            # Whirlpool / DLMM account reads an unrelated integer; a wrong
            # number here is worse than no number.
            return OnChainSecurityFacts(
                notes=(f"pool is not Raydium AMM v4 (owned by {str(program)[:12]}…) — "
                       f"the verified lpReserve layout does not apply, no LP claim",))

        data = self._decode_account_data(value.get("data"))
        if data is None:
            return OnChainSecurityFacts(notes=("pool account data unreadable",))
        if len(data) != _RAYDIUM_V4_ACCOUNT_LEN:
            return OnChainSecurityFacts(
                notes=(f"pool account is {len(data)} bytes, not the "
                       f"{_RAYDIUM_V4_ACCOUNT_LEN}-byte v4 layout — no LP claim",))

        lp_mint = encode_base58(data[_OFF_LP_MINT:_OFF_LP_MINT + 32])
        try:
            lp_reserve = struct.unpack_from("<Q", data, _OFF_LP_RESERVE)[0]
        except struct.error:
            return OnChainSecurityFacts(notes=("could not decode lpReserve",))
        if lp_reserve <= 0:
            return OnChainSecurityFacts(
                notes=("lpReserve is zero — the burned fraction is undefined",))

        supply_result = await self._rpc(
            "getTokenSupply", [lp_mint],
            cache_key=f"onchain:lpsupply:{lp_mint}", cache_ttl=120.0)
        lp_supply = _as_int(((supply_result or {}).get("value") or {}).get("amount"))
        if lp_supply is None:
            return OnChainSecurityFacts(
                notes=(f"LP mint {lp_mint[:8]}… supply unreadable — no LP claim",))

        if lp_supply > lp_reserve:
            # Current supply above the recorded reserve means the two numbers do
            # not describe the same thing right now (liquidity added after the
            # snapshot, or a layout assumption that does not hold for this pool).
            # A negative burn is not a small burn — refuse it (Rule 8).
            return OnChainSecurityFacts(
                notes=(f"LP supply {lp_supply} exceeds lpReserve {lp_reserve} — "
                       f"the burn arithmetic does not describe this pool, no LP claim",))

        burned = 100.0 * (lp_reserve - lp_supply) / lp_reserve
        if burned <= 0.0 and not self._s.treat_zero_burn_as_unlocked:
            # Nothing was burned. Whether it is instead LOCKED in a locker
            # program is not something this layer can see, so under this setting
            # it says nothing rather than implying "pullable" (see the setting's
            # comment: the alternative can block a coin for being safely locked).
            return OnChainSecurityFacts(
                notes=("no LP burned; a third-party lock cannot be verified from "
                       "chain, so no LP claim is made",))
        return OnChainSecurityFacts(lp_burned_percent=min(100.0, max(0.0, burned)))

    @staticmethod
    def _decode_account_data(payload: Any) -> bytes | None:
        """``[base64, "base64"]`` from getAccountInfo, or ``None``."""
        if isinstance(payload, list) and payload and isinstance(payload[0], str):
            try:
                return base64.b64decode(payload[0], validate=True)
            except (ValueError, TypeError):
                return None
        return None

    # ---- Combined ----

    async def collect(self, mint: str, pool_address: str | None = None
                      ) -> OnChainSecurityFacts:
        """Both facts for one coin. A failure in either half never voids the other.

        Bounded at six RPC calls per coin (four for the census, two for LP), all
        cached, so a watchlist recheck inside the TTL costs nothing (Rule 11).
        """
        notes: list[str] = []
        concentration = OnChainSecurityFacts()
        try:
            concentration = await self.get_holder_concentration(mint)
        except (CollectorError, ValueError) as exc:
            notes.append(f"holder census unavailable: {self._scrub(str(exc))}")

        lp = OnChainSecurityFacts()
        if pool_address:
            try:
                lp = await self.get_lp_burned_percent(pool_address)
            except (CollectorError, ValueError) as exc:
                notes.append(f"LP status unavailable: {self._scrub(str(exc))}")
        else:
            notes.append("no pool address supplied — LP status not attempted")

        return OnChainSecurityFacts(
            top_holder_percent=concentration.top_holder_percent,
            top10_holder_percent=concentration.top10_holder_percent,
            top_holder_owner=concentration.top_holder_owner,
            lp_burned_percent=lp.lp_burned_percent,
            census_owner_count=concentration.census_owner_count,
            excluded_owners=concentration.excluded_owners,
            notes=tuple(notes) + concentration.notes + lp.notes,
        )
