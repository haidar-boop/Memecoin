"""Wallet-origin lookups for the funding-cluster check (/bundle).

Reads, for each top holder of a coin, where that wallet CAME from — its first
transaction, who paid for it, and whether the wallet (or its funder) has the
long history of a real participant rather than a fresh sybil. The pure
clustering logic lives in :mod:`meme_intelligence.analyzers.wallet_clusters`;
this module only fetches observations and never draws conclusions.

Cost model (Rule 11 — the operator watches his Helius spend)
------------------------------------------------------------
Per coin: one top-holders census (3 RPC calls via the existing HeliusClient)
plus, per non-custody holder, ONE ``getSignaturesForAddress`` page and — only
for fresh wallets — ONE ``getTransaction``. Each *distinct* funder costs one
more signature page to classify it as infrastructure or not. Top-20 worst case
is ~60 bounded calls, on-demand only: this runs when the operator sends
``/bundle``, never inside the scan loop.

Honesty rules (Rule 8)
----------------------
* A failed lookup yields ``WalletOrigin(kind=UNKNOWN)`` with the reason in
  ``note`` — never a guessed funder and never silence.
* A signature page that saturates (a full page returned) proves LONG history,
  not its details: the wallet is ESTABLISHED and no funder is claimed for it.
* The fee payer of a wallet's oldest transaction is only called its funder
  when it is a DIFFERENT address; a wallet whose first act was self-signed has
  no observable funder here, and none is invented.
"""

from __future__ import annotations

from typing import Any

from meme_intelligence.analyzers.wallet_clusters import (
    ESTABLISHED,
    FRESH,
    UNKNOWN,
    HolderStake,
    WalletOrigin,
    is_off_curve,
)
from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.config.settings import WalletClusterSettings
from meme_intelligence.core.errors import CollectorError

# One full getSignaturesForAddress page. A wallet or funder whose history fills
# an entire page has months-to-years of activity — not a wallet minted for this
# launch. This is the RPC page maximum, not a tunable: making it configurable
# would invite a value the "established" semantics silently stop matching.
_SIGNATURE_PAGE_LIMIT = 1000


class FundingClient(BaseCollector):
    """Origin lookups over Solana RPC (Helius when keyed, public otherwise)."""

    def __init__(self, settings: WalletClusterSettings, *, api_key: str = "",
                 rpc_url: str = "https://mainnet.helius-rpc.com",
                 fallback_rpc_url: str = "https://api.mainnet-beta.solana.com",
                 **kwargs: Any) -> None:
        url = rpc_url if api_key else fallback_rpc_url
        kwargs.setdefault("name", "funding_data")
        kwargs.setdefault("base_url", url)
        kwargs.setdefault("timeout_seconds", settings.timeout_seconds)
        if api_key:
            # Key rides in the URL query (Helius has no header auth) — scrub it
            # from every raised message (Rule 16).
            kwargs.setdefault("redact", (api_key,))
        super().__init__(**kwargs)
        self._s = settings
        self._path = f"/?api-key={api_key}" if api_key else "/"

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

    # ---- signature history ----

    async def _signature_page(self, address: str,
                              before: str | None = None) -> list[dict] | None:
        """One full page of signatures (newest first), or None on failure.
        ``before`` walks further back in history."""
        params: dict = {"limit": _SIGNATURE_PAGE_LIMIT}
        if before:
            params["before"] = before
        key = f"funding:sigs:{address}:{before or 'head'}"
        try:
            result = await self._rpc(
                "getSignaturesForAddress", [address, params],
                cache_key=key, cache_ttl=300.0)
        except CollectorError:
            return None
        if not isinstance(result, list):
            return None
        return [entry for entry in result if isinstance(entry, dict)]

    async def is_high_activity(self, address: str) -> bool | None:
        """True when ``address`` fills a full first page — an exchange, router,
        or spam bot that touches strangers' wallets. Verified live 2026-07-30:
        this single check removed every false cluster on the organic controls
        (a CEX hot wallet at ~1053 tx/hr and two dust-spam bots at ~730 tx/hr).
        None when the read failed (never guessed)."""
        page = await self._signature_page(address)
        if page is None:
            return None
        return len(page) >= _SIGNATURE_PAGE_LIMIT

    def _oldest_of(self, page: list[dict]) -> tuple[str | None, int | None]:
        oldest = page[-1]
        signature = oldest.get("signature")
        slot = oldest.get("slot") if isinstance(oldest.get("slot"), int) else None
        return (signature if isinstance(signature, str) and signature else None), slot

    # ---- origins ----

    async def get_wallet_origin(self, wallet: str) -> WalletOrigin:
        """Where this wallet came from, by walking back to its true first
        transaction. Bounded to ``max_signature_pages`` pages: a wallet still
        not exhausted past that bound is genuinely high-activity (ESTABLISHED),
        NOT a fresh bundle wallet.

        The bound is the fix for the fatal case verified live: a sniper wallet
        with 2,400 signatures at the moment it sniped would, under a single
        page, be misfiled as an established trader and its bundle missed. Paging
        to its real oldest transaction recovers the funder edge."""
        if not wallet:
            raise ValueError("wallet must be non-empty")

        page = await self._signature_page(wallet)
        if page is None:
            return WalletOrigin(wallet=wallet, kind=UNKNOWN,
                                note="signature history unreadable")
        if not page:
            return WalletOrigin(wallet=wallet, kind=UNKNOWN,
                                note="no transaction history returned")

        # Walk back while each page is full, up to the bound.
        signature, slot = self._oldest_of(page)
        pages_walked = 1
        reached_oldest = len(page) < _SIGNATURE_PAGE_LIMIT
        while not reached_oldest and pages_walked < self._s.max_signature_pages:
            if signature is None:
                return WalletOrigin(wallet=wallet, kind=UNKNOWN,
                                    note="signature page entry malformed")
            nxt = await self._signature_page(wallet, before=signature)
            pages_walked += 1
            if nxt is None:
                return WalletOrigin(wallet=wallet, kind=UNKNOWN,
                                    note="pagination read failed mid-history")
            if not nxt:
                reached_oldest = True         # current page's oldest IS the first tx
                break
            page = nxt
            signature, slot = self._oldest_of(page)
            reached_oldest = len(page) < _SIGNATURE_PAGE_LIMIT

        if not reached_oldest:
            # Still full at the bound: genuinely high-activity, not a fresh
            # sybil. No funder is claimed — we never reached its first tx.
            return WalletOrigin(wallet=wallet, kind=ESTABLISHED)
        if signature is None:
            return WalletOrigin(wallet=wallet, kind=UNKNOWN,
                                note="oldest signature entry malformed")

        funder = await self._fee_payer(signature, wallet)
        return WalletOrigin(wallet=wallet, kind=FRESH, funder=funder,
                            first_signature=signature, first_slot=slot)

    async def _fee_payer(self, signature: str, wallet: str) -> str | None:
        """Fee payer of ``signature`` when it is not ``wallet`` itself, else
        None. The fee payer of a wallet's FIRST transaction is whoever brought
        it into existence — the funder."""
        try:
            result = await self._rpc(
                "getTransaction",
                [signature, {"encoding": "jsonParsed",
                             "maxSupportedTransactionVersion": 0}],
                cache_key=f"funding:tx:{signature}", cache_ttl=3600.0)
        except CollectorError:
            return None
        message = (((result or {}).get("transaction") or {}).get("message") or {})
        keys = message.get("accountKeys") or []
        for entry in keys:
            # jsonParsed: [{pubkey, signer, writable, source}, ...] — the fee
            # payer is the first signer.
            if isinstance(entry, dict) and entry.get("signer"):
                payer = entry.get("pubkey")
                if isinstance(payer, str) and payer and payer != wallet:
                    return payer
                return None    # self-paid first tx: no observable funder
        return None


class BundleService:
    """Orchestrates one /bundle analysis: census -> origins -> observations.

    Returns raw material for :func:`cluster_holders`; draws no conclusions.
    ``helius`` is the existing wallet-data client (its ``get_top_holders``
    needs a keyed endpoint — ``getTokenLargestAccounts`` is disabled on public
    RPC, verified live 2026-07-30 with x-ratelimit-method-limit: 0).
    """

    def __init__(self, helius, funding: FundingClient,
                 settings: WalletClusterSettings) -> None:
        self._helius = helius
        self._funding = funding
        self._s = settings

    async def close(self) -> None:
        await self._funding.close()

    async def gather(self, mint: str) -> tuple[list[HolderStake],
                                               dict[str, WalletOrigin],
                                               list[str]]:
        """(stakes, origins, notes) for one mint. Custody excluded up front so
        no origin lookup is spent on a pool vault."""
        notes: list[str] = []
        holdings = await self._helius.get_top_holders(
            mint, limit=self._s.top_holders_limit)
        if not holdings:
            return [], {}, ["no holders visible — coin may be unlaunched or dead"]

        stakes: list[HolderStake] = []
        custody = 0
        for holding in holdings:
            if is_off_curve(holding.owner):
                custody += 1        # pool vault / curve authority — not a holder
                continue
            stakes.append(HolderStake(wallet=holding.owner,
                                      percent=holding.percent))
        if custody:
            notes.append(f"{custody} custody account(s) (pool/curve) excluded")
        if not stakes:
            return [], {}, notes + ["every visible holder is custody"]

        # Origins, sequentially: BaseCollector's rate limiter paces the calls,
        # and one coin's lookup must not burst the shared Helius budget.
        origins: dict[str, WalletOrigin] = {}
        for stake in stakes:
            origins[stake.wallet] = await self._funding.get_wallet_origin(stake.wallet)

        # Classify each distinct funder's activity once. A funder that is
        # itself a top holder needs no check (that edge stands on its own);
        # skipping it saves calls on exactly the bundled case.
        holder_set = {stake.wallet for stake in stakes}
        funders = sorted({origin.funder for origin in origins.values()
                          if origin.funder and origin.funder not in holder_set})
        activity: dict[str, bool | None] = {}
        for funder in funders:
            activity[funder] = await self._funding.is_high_activity(funder)

        for wallet, origin in list(origins.items()):
            if origin.funder and origin.funder in activity:
                origins[wallet] = WalletOrigin(
                    wallet=origin.wallet, kind=origin.kind, funder=origin.funder,
                    funder_is_infrastructure=activity[origin.funder],
                    first_signature=origin.first_signature,
                    first_slot=origin.first_slot, note=origin.note)
            elif origin.funder and origin.funder in holder_set:
                origins[wallet] = WalletOrigin(
                    wallet=origin.wallet, kind=origin.kind, funder=origin.funder,
                    funder_is_infrastructure=False,
                    first_signature=origin.first_signature,
                    first_slot=origin.first_slot, note=origin.note)
        return stakes, origins, notes
