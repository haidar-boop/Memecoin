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
  risk in this work. Exclusion is therefore structural: an owner whose account
  is owned by a *program* (a PDA — vault, curve, staking pool) is custody.
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
consistent cross-checks, which is what makes this trustworthy. Every v4 pool
vault is owned by the single constant authority below, so excluding that one
address excludes every Raydium v4 vault without having to know the pool.

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

# Custody authorities excluded by ADDRESS. **This list is load-bearing, not a
# shortcut — do not delete it in favour of the structural rule below.**
#
# Verified live 2026-07-30: the Raydium v4 vault authority
# 5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1 is owned by the SYSTEM PROGRAM
# and carries zero-length data, so it is indistinguishable from an ordinary
# wallet by the structural test. It is a signer-only PDA: it never stores state,
# it only signs. On mint 6Gni19FJsmz8X26fNpWWHydYCTuj59WPqREMbNdzpump its vault
# held 94.6% of supply — so dropping this constant would report a healthy
# graduated coin as 94.6% concentrated and block it. That is the
# silence-the-operator failure this module exists to avoid.
#
# The structural rule catches the state-holding custody accounts (pump.fun
# bonding curves, PumpSwap pool PDAs); this list catches the signer-only ones.
# Both are required.
_CUSTODY_OWNERS = frozenset({_RAYDIUM_AMM_V4_AUTHORITY}) | _BURN_OWNERS

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


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


@dataclass(frozen=True)
class OnChainSecurityFacts:
    """What one census actually established. Every field may be ``None``.

    ``None`` means "not established" — never "zero" and never "safe". The
    ``notes`` carry why, so a probe or a log line can explain a withheld number
    instead of leaving it silently absent (Rule 13).
    """

    top_holder_percent: float | None = None
    top10_holder_percent: float | None = None
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

        owners_result = await self._rpc(
            "getMultipleAccounts",
            [[address for address, _ in accounts], {"encoding": "jsonParsed"}],
            cache_key=f"onchain:owners:{mint}:{len(accounts)}:{accounts[0][0]}",
            cache_ttl=120.0)
        owner_infos = (owners_result or {}).get("value") or []

        by_owner: dict[str, int] = {}
        unresolved = 0
        for (address, amount), info in zip(accounts, owner_infos, strict=False):
            owner = None
            if isinstance(info, dict):
                owner = (((info.get("data") or {}).get("parsed") or {})
                         .get("info") or {}).get("owner")
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
        custody_by_address = {owner for owner in by_owner if owner in _CUSTODY_OWNERS}
        for owner in custody_by_address:
            excluded.append(f"{owner} (known custody/burn address)")

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

        amounts = sorted(held.values(), reverse=True)
        top_percent = 100.0 * amounts[0] / supply
        top10_percent = 100.0 * sum(amounts[:10]) / supply
        notes: list[str] = []
        if len(amounts) < 10:
            # top10 over fewer than 10 real holders is still the correct share
            # of supply held by the top ten (there just are not ten), but say so.
            notes.append(f"only {len(amounts)} non-custody owners in the top "
                         f"{len(accounts)} accounts")
        return OnChainSecurityFacts(
            top_holder_percent=min(100.0, top_percent),
            top10_holder_percent=min(100.0, top10_percent),
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
            cache_key=f"onchain:ownerkind:{len(owners)}:{owners[0]}", cache_ttl=600.0)
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
            lp_burned_percent=lp.lp_burned_percent,
            census_owner_count=concentration.census_owner_count,
            excluded_owners=concentration.excluded_owners,
            notes=tuple(notes) + concentration.notes + lp.notes,
        )
