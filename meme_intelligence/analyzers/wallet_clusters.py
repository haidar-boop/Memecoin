"""Wallet funding-cluster analysis: is this coin's "distribution" one actor?

Why this exists (operator request 2026-07-30, after reading a Bubblemap)
-----------------------------------------------------------------------
A bundled launch spreads one actor's supply across many fresh wallets so the
coin *looks* distributed. Per-wallet concentration checks are blind to it by
construction: thirty wallets holding 2% each read as thirty independent small
holders. Live investigation the same day found the pattern repeatedly — three
coins where the deployer kept exactly 50% and sent 12.5% to each of four fresh
sibling wallets, and one coin where six wallets took ~79% of supply inside a
single slot. The balances looked spread; the *funding* said one person.

So this module clusters top holders by ORIGIN rather than by balance:

* two holders whose first-ever transaction is the SAME transaction are the same
  actor (co-created wallets — essentially certain);
* two FRESH holders funded by the same non-infrastructure wallet are very
  likely the same actor;
* a holder who FUNDED another top holder is joined to it (the deployer keeping
  50% and funding four siblings becomes one 100% cluster).

What it refuses to conclude (Rule 8)
------------------------------------
* An **unknown origin never clusters.** A wallet whose history could not be
  read goes into its own singleton and is counted, loudly, as unknown — it is
  never assumed independent-and-fine, and never assumed guilty.
* An **infrastructure funder never clusters.** Two strangers who both withdrew
  from the same exchange hot wallet share a "funder"; clustering on it would
  invent a bundle out of ordinary CEX withdrawals. A funder whose own history
  saturates a full signature page is high-traffic infrastructure and is
  reported, not used as an edge.
* An **established wallet is not a sybil.** A wallet with a long history
  predating the coin is not a fresh bundle wallet. It can still anchor a
  cluster if it *funded* other top holders (that is the deployer case), but it
  is never clustered merely for sharing a funder with someone.

This module is pure: no I/O, no clock, no RPC. The collector feeds it
observations; it returns a report. It feeds NO score, NO gate and NO veto — it
answers ``/bundle`` when the operator asks, and nothing else. (The previous
holder-concentration layer was wired toward alerting and was scrapped for it;
that decision is recorded in the DECISIONS_LOG and is not revisited here.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from meme_intelligence.config.settings import WalletClusterSettings

# --- Solana constants (protocol facts, not tunables) ---

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_P = 2**255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P


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

    A Solana wallet address IS an ed25519 public key, so it must decompress to
    a valid curve point; PDAs are derived off-curve precisely to prove no
    private key exists. Verified live 2026-07-30 against the Raydium v4 /
    Raydium CPMM / Meteora DAMM v2 / Meteora DBC vault authorities (all
    off-curve) and three known real holder wallets (all on-curve). Used here to
    drop custody accounts (pool vaults, bonding curves) from the holder list
    before any origin lookup is paid for.

    Unparseable input returns False: "could not check" must never read as
    "provably not a holder" (Rule 8).
    """
    raw = decode_base58(address)
    if raw is None:
        return False
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _ED25519_P:
        return True
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
            return True
    return False


# --- Observations the collector supplies ---

#: Origin kinds. FRESH = short history, origin read; ESTABLISHED = history
#: saturated a full signature page (predates any fresh-coin bundle);
#: UNKNOWN = the read failed — never treated as either of the others.
FRESH = "fresh"
ESTABLISHED = "established"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class HolderStake:
    """One top holder: owner wallet and its share of total supply (0-100)."""

    wallet: str
    percent: float


@dataclass(frozen=True)
class WalletOrigin:
    """What one origin lookup established about a wallet. Fields may be None —
    None always means "not established", never a value (Rule 8)."""

    wallet: str
    kind: str = UNKNOWN                # FRESH / ESTABLISHED / UNKNOWN
    funder: str | None = None          # fee payer of the wallet's first tx
    funder_is_infrastructure: bool | None = None  # None = not checked
    first_signature: str | None = None
    first_slot: int | None = None
    note: str | None = None            # why UNKNOWN, when it is


@dataclass(frozen=True)
class Cluster:
    """One group of top holders judged to be a single actor."""

    members: tuple[str, ...]           # wallets, largest stake first
    combined_percent: float
    evidence: tuple[str, ...]          # human-readable reasons for each join

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class ClusterReport:
    """Everything ``cluster_holders`` established about one coin's top holders.

    ``clusters`` contains only multi-wallet groups; singletons are summarized in
    the counts. This report is display material for /bundle — it deliberately
    has no score and no verdict field, so nothing downstream can treat it as a
    gate input by accident.
    """

    clusters: tuple[Cluster, ...] = ()          # sorted by combined percent, desc
    holders_examined: int = 0
    independent_holders: int = 0                # fresh singletons, real funder, no match
    established_holders: int = 0                # long-history singletons (busy traders)
    unknown_origins: int = 0                    # could not be read — NOT independent
    infrastructure_funders: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def top_cluster_percent(self) -> float | None:
        return self.clusters[0].combined_percent if self.clusters else None


class _UnionFind:
    def __init__(self, items):
        self._parent = {item: item for item in items}

    def find(self, item):
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:      # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def cluster_holders(
    stakes: list[HolderStake],
    origins: dict[str, WalletOrigin],
    settings: WalletClusterSettings,
) -> ClusterReport:
    """Group top holders into same-actor clusters by funding evidence.

    ``stakes`` must already exclude custody (off-curve owners); the collector
    does that before paying for origin lookups. ``origins`` may be missing
    entries or carry UNKNOWN kinds — both are handled as unknowns, never as
    evidence in either direction.
    """
    if not stakes:
        return ClusterReport(notes=("no holders to examine",))

    percent_of = {stake.wallet: stake.percent for stake in stakes}
    wallets = [stake.wallet for stake in stakes]
    uf = _UnionFind(wallets)
    evidence: dict[str, list[str]] = {wallet: [] for wallet in wallets}

    def note_join(a: str, b: str, why: str) -> None:
        uf.union(a, b)
        evidence[uf.find(a)] = evidence.get(uf.find(a), [])
        # keep the reason on both original wallets' trails for rendering
        evidence[a].append(why)

    unknown = 0
    infra: set[str] = set()

    # Pass 1 — same first transaction: co-created wallets are one actor.
    by_first_sig: dict[str, list[str]] = {}
    for wallet in wallets:
        origin = origins.get(wallet)
        if origin is None or origin.kind == UNKNOWN:
            unknown += 1
            continue
        if origin.kind == FRESH and origin.first_signature:
            by_first_sig.setdefault(origin.first_signature, []).append(wallet)
    for signature, group in by_first_sig.items():
        for other in group[1:]:
            note_join(group[0], other,
                      f"first transaction is the same as {group[0][:6]}… "
                      f"(co-created, sig {signature[:10]}…)")

    # Pass 2 — same funder, both fresh, funder not infrastructure.
    by_funder: dict[str, list[str]] = {}
    for wallet in wallets:
        origin = origins.get(wallet)
        if origin is None or origin.kind != FRESH or not origin.funder:
            continue
        if origin.funder_is_infrastructure:
            infra.add(origin.funder)
            continue
        if origin.funder_is_infrastructure is None:
            # Unchecked funder activity: clustering on it could invent a bundle
            # out of shared CEX withdrawals, so it is NOT an edge (Rule 8) —
            # but it is still recorded for pass 3 (funder-is-a-holder needs no
            # activity check: a top holder funding another top holder is
            # evidence regardless of how busy the funder is).
            if origin.funder in percent_of:
                by_funder.setdefault(origin.funder, []).append(wallet)
            continue
        by_funder.setdefault(origin.funder, []).append(wallet)
    for funder, group in by_funder.items():
        for other in group[1:]:
            note_join(group[0], other, f"funded by the same wallet {funder[:6]}…")

    # Pass 3 — the funder is itself a top holder: deployer + siblings join.
    for wallet in wallets:
        origin = origins.get(wallet)
        if origin is None or origin.kind != FRESH or not origin.funder:
            continue
        if origin.funder in percent_of and origin.funder != wallet:
            note_join(origin.funder, wallet,
                      f"funded by top holder {origin.funder[:6]}… "
                      f"({percent_of[origin.funder]:.1f}%)")

    # Collect groups.
    groups: dict[str, list[str]] = {}
    for wallet in wallets:
        groups.setdefault(uf.find(wallet), []).append(wallet)

    clusters: list[Cluster] = []
    independent = 0
    established = 0
    for members in groups.values():
        if len(members) == 1:
            origin = origins.get(members[0])
            if origin is None or origin.kind == UNKNOWN:
                continue                      # counted under unknown, not here
            if origin.kind == ESTABLISHED:
                established += 1               # busy trader, reported separately
            else:
                independent += 1               # fresh, real funder, matched no one
            continue
        members.sort(key=lambda w: percent_of[w], reverse=True)
        combined = sum(percent_of[w] for w in members)
        if not math.isfinite(combined):
            continue
        reasons = tuple(dict.fromkeys(
            reason for wallet in members for reason in evidence[wallet]))
        clusters.append(Cluster(members=tuple(members),
                                combined_percent=min(100.0, combined),
                                evidence=reasons))
    clusters.sort(key=lambda c: c.combined_percent, reverse=True)

    notes: list[str] = []
    if unknown:
        notes.append(f"{unknown} of {len(wallets)} top holders have UNREADABLE "
                     f"origins — they are counted separately, not assumed "
                     f"independent")
    if infra:
        notes.append(f"{len(infra)} shared funder(s) are high-traffic "
                     f"infrastructure (exchange/router) — NOT treated as "
                     f"bundle evidence")

    return ClusterReport(
        clusters=tuple(clusters),
        holders_examined=len(wallets),
        independent_holders=independent,
        established_holders=established,
        unknown_origins=unknown,
        infrastructure_funders=tuple(sorted(infra)),
        notes=tuple(notes),
    )
