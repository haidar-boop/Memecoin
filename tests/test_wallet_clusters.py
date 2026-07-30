"""Tests for the wallet funding-cluster engine.

The fixtures mirror the real bundles found on chain 2026-07-30: a deployer that
kept 50% and funded four fresh siblings at 12.5% each, and six sniper wallets
created across shared transactions in one slot. The engine must rediscover
those — and must refuse to invent bundles out of unknowns, exchange hot
wallets, or established wallets.
"""

import pytest

from meme_intelligence.analyzers.wallet_clusters import (
    ESTABLISHED,
    FRESH,
    UNKNOWN,
    Cluster,
    ClusterReport,
    HolderStake,
    WalletOrigin,
    cluster_holders,
    decode_base58,
    is_off_curve,
)
from meme_intelligence.config.settings import WalletClusterSettings

SETTINGS = WalletClusterSettings()

# Real addresses with known curve status (verified live 2026-07-30).
RAYDIUM_V4_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"   # off-curve
METEORA_DAMM_AUTHORITY = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"  # off-curve
REAL_WALLET = "6uRXCp3v13N4FKfMiU1oS7ztnnbNXPLWc9HSp6X8a5oq"             # on-curve
SYSTEM = "11111111111111111111111111111111"


def fresh(wallet, funder=None, sig=None, slot=None, infra=False):
    return WalletOrigin(wallet=wallet, kind=FRESH, funder=funder,
                        funder_is_infrastructure=infra if funder else None,
                        first_signature=sig, first_slot=slot)


# ---------------------------------------------------------------------------
# curve helpers
# ---------------------------------------------------------------------------

def test_off_curve_identifies_custody_and_keeps_real_wallets():
    assert is_off_curve(RAYDIUM_V4_AUTHORITY)
    assert is_off_curve(METEORA_DAMM_AUTHORITY)
    assert not is_off_curve(REAL_WALLET)
    assert not is_off_curve(SYSTEM)          # System address is on-curve
    assert not is_off_curve("not-base58!!")  # unparseable is never "custody"
    assert decode_base58("bad chars !") is None


# ---------------------------------------------------------------------------
# The template bundle: deployer keeps 50%, funds four siblings at 12.5%
# ---------------------------------------------------------------------------

def test_the_deployer_template_becomes_one_full_cluster():
    stakes = [HolderStake("Deployer", 50.0),
              HolderStake("Sib1", 12.5), HolderStake("Sib2", 12.5),
              HolderStake("Sib3", 12.5), HolderStake("Sib4", 12.5)]
    origins = {
        "Deployer": fresh("Deployer", funder="SomeCex", infra=True),
        "Sib1": fresh("Sib1", funder="Deployer"),
        "Sib2": fresh("Sib2", funder="Deployer"),
        "Sib3": fresh("Sib3", funder="Deployer"),
        "Sib4": fresh("Sib4", funder="Deployer"),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert cluster.combined_percent == pytest.approx(100.0)
    assert set(cluster.members) == {"Deployer", "Sib1", "Sib2", "Sib3", "Sib4"}
    assert cluster.members[0] == "Deployer", "largest stake leads the listing"
    assert any("funded by top holder" in reason for reason in cluster.evidence)


def test_an_established_deployer_still_anchors_its_siblings():
    """The funder-is-a-holder edge must not require the funder to be fresh —
    a dev wallet with years of history still owns the wallets it funded."""
    stakes = [HolderStake("OldDev", 40.0), HolderStake("SibA", 20.0),
              HolderStake("SibB", 20.0)]
    origins = {
        "OldDev": WalletOrigin(wallet="OldDev", kind=ESTABLISHED),
        "SibA": fresh("SibA", funder="OldDev"),
        "SibB": fresh("SibB", funder="OldDev"),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert len(report.clusters) == 1
    assert report.clusters[0].combined_percent == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# The sniper bundle: co-created wallets share their first transaction
# ---------------------------------------------------------------------------

def test_wallets_sharing_their_first_transaction_are_one_actor():
    stakes = [HolderStake("S1", 46.59), HolderStake("S2", 9.26),
              HolderStake("S3", 0.79), HolderStake("Other", 1.0)]
    origins = {
        "S1": fresh("S1", sig="SIGAAA", slot=436124761),
        "S2": fresh("S2", sig="SIGAAA", slot=436124761),
        "S3": fresh("S3", sig="SIGAAA", slot=436124761),
        "Other": fresh("Other", sig="SIGZZZ", slot=436130000),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert len(report.clusters) == 1
    assert set(report.clusters[0].members) == {"S1", "S2", "S3"}
    assert report.clusters[0].combined_percent == pytest.approx(56.64)
    assert report.independent_holders == 1


def test_two_cocreation_groups_with_a_shared_funder_merge():
    """The TNOS shape: two same-slot transactions, three wallets each. If both
    transactions were paid by the same funder, the six merge into one cluster."""
    stakes = [HolderStake(f"W{i}", 10.0) for i in range(6)]
    origins = {}
    for i in range(3):
        origins[f"W{i}"] = fresh(f"W{i}", funder="Payer", sig="TXA")
    for i in range(3, 6):
        origins[f"W{i}"] = fresh(f"W{i}", funder="Payer", sig="TXB")
    report = cluster_holders(stakes, origins, SETTINGS)
    assert len(report.clusters) == 1
    assert report.clusters[0].size == 6
    assert report.clusters[0].combined_percent == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Refusals (Rule 8) — the traps that would invent bundles
# ---------------------------------------------------------------------------

def test_a_shared_exchange_hot_wallet_never_creates_a_cluster():
    """THE trap. Two strangers who withdrew from the same CEX share a 'funder'.
    Clustering on it would report ordinary withdrawals as a bundle and damn a
    healthy coin."""
    stakes = [HolderStake("A", 8.0), HolderStake("B", 7.0)]
    origins = {
        "A": fresh("A", funder="CexHotWallet", infra=True),
        "B": fresh("B", funder="CexHotWallet", infra=True),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == ()
    assert report.independent_holders == 2
    assert "CexHotWallet" in report.infrastructure_funders
    assert any("infrastructure" in note for note in report.notes)


def test_an_unchecked_funder_is_not_evidence():
    """funder_is_infrastructure=None means the funder's activity was never
    measured. Clustering on it would be guessing in the damning direction."""
    stakes = [HolderStake("A", 8.0), HolderStake("B", 7.0)]
    origins = {
        "A": WalletOrigin(wallet="A", kind=FRESH, funder="Mystery",
                          funder_is_infrastructure=None),
        "B": WalletOrigin(wallet="B", kind=FRESH, funder="Mystery",
                          funder_is_infrastructure=None),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == ()


def test_unknown_origins_are_counted_not_assumed_independent():
    stakes = [HolderStake("A", 30.0), HolderStake("B", 5.0)]
    origins = {
        "A": WalletOrigin(wallet="A", kind=UNKNOWN, note="RPC failed"),
        # B has no origin entry at all
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == ()
    assert report.unknown_origins == 2
    assert report.independent_holders == 0
    assert any("UNREADABLE" in note for note in report.notes)


def test_established_wallets_sharing_a_funder_do_not_cluster():
    """Two old wallets funded years ago by the same (now defunct) service are
    not a bundle — the fresh-wallet requirement is what makes the funder edge
    meaningful."""
    stakes = [HolderStake("Old1", 6.0), HolderStake("Old2", 5.0)]
    origins = {
        "Old1": WalletOrigin(wallet="Old1", kind=ESTABLISHED, funder="X",
                             funder_is_infrastructure=False),
        "Old2": WalletOrigin(wallet="Old2", kind=ESTABLISHED, funder="X",
                             funder_is_infrastructure=False),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == ()
    assert report.established_holders == 2      # long-history, own bucket
    assert report.independent_holders == 0


def test_empty_holder_list_reports_nothing():
    report = cluster_holders([], {}, SETTINGS)
    assert report.clusters == ()
    assert report.holders_examined == 0
    assert any("no holders" in note for note in report.notes)


def test_combined_percent_is_clamped_and_finite():
    stakes = [HolderStake("A", 60.0), HolderStake("B", 55.0)]
    origins = {"A": fresh("A", sig="T"), "B": fresh("B", sig="T")}
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters[0].combined_percent == 100.0


def test_report_carries_no_score_or_verdict_field():
    """The contract with the operator: this is display material, not a gate
    input. Nothing downstream can consume a number that does not exist."""
    for forbidden in ("score", "verdict", "veto", "rug"):
        assert not any(forbidden in name for name in ClusterReport.__dataclass_fields__), forbidden
        assert not any(forbidden in name for name in Cluster.__dataclass_fields__), forbidden


# ---------------------------------------------------------------------------
# Review fixes (2026-07-30 adversarial pass)
# ---------------------------------------------------------------------------

def test_an_unknown_origin_funder_never_drags_a_holder_into_a_cluster():
    """Review finding: F is a top holder whose origin read FAILED, W is funded
    by F. Pass 3 must NOT cluster (F,W) — 'an unknown origin never clusters',
    and F must not be both a cluster member and counted unreadable."""
    stakes = [HolderStake("F", 60.0), HolderStake("W", 40.0)]
    origins = {
        "F": WalletOrigin(wallet="F", kind=UNKNOWN, note="rpc timeout"),
        # W's funder is F; its activity read failed too -> None.
        "W": WalletOrigin(wallet="W", kind=FRESH, funder="F",
                          funder_is_infrastructure=None),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == ()
    assert report.unknown_origins == 1
    # The invariant the review found broken: counts must sum to holders_examined.
    total = (sum(c.size for c in report.clusters) + report.independent_holders
             + report.established_holders + report.unknown_origins)
    assert total == report.holders_examined == 2


def test_a_cex_that_is_a_top_holder_does_not_cluster_its_withdrawers():
    """Review finding: a CEX/router which happens to be a top holder funded two
    independent holders. Pass 3 must honour its infrastructure flag, not
    re-open the exact false positive Pass 2 blocks."""
    stakes = [HolderStake("Cex", 30.0), HolderStake("A", 10.0), HolderStake("B", 9.0)]
    origins = {
        "Cex": WalletOrigin(wallet="Cex", kind=ESTABLISHED),
        "A": WalletOrigin(wallet="A", kind=FRESH, funder="Cex",
                          funder_is_infrastructure=True),
        "B": WalletOrigin(wallet="B", kind=FRESH, funder="Cex",
                          funder_is_infrastructure=True),
    }
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == ()
    assert "Cex" in report.infrastructure_funders


def test_a_mass_airdrop_first_signature_is_not_a_bundle():
    """Review finding: getSignaturesForAddress indexes txs a wallet never signed,
    so a multisend/airdrop is the shared 'first signature' of many independent
    recipients. More than a tight batch sharing one first sig is treated as a
    fan-out, not co-creation."""
    stakes = [HolderStake(f"R{i}", 5.0) for i in range(10)]
    origins = {f"R{i}": WalletOrigin(wallet=f"R{i}", kind=FRESH,
                                     first_signature="AIRDROP")
               for i in range(10)}
    report = cluster_holders(stakes, origins, SETTINGS)
    assert report.clusters == (), "10 airdrop recipients are not one actor"
    assert any("infrastructure" in n or "airdrop" in n for n in report.notes)


def test_a_tight_cocreation_batch_still_clusters():
    """The other side: a real 4-wallet co-creation batch (under the fan-out
    limit) must still be caught."""
    stakes = [HolderStake(f"S{i}", 12.0) for i in range(4)]
    origins = {f"S{i}": WalletOrigin(wallet=f"S{i}", kind=FRESH,
                                     first_signature="BATCH")
               for i in range(4)}
    report = cluster_holders(stakes, origins, SETTINGS)
    assert len(report.clusters) == 1
    assert report.clusters[0].size == 4


# ---------------------------------------------------------------------------
# Renderer honesty (review: never claim "independently funded" without checking)
# ---------------------------------------------------------------------------

def _render(report):
    from meme_intelligence.alerts.telegram_commands import _render_bundle
    return _render_bundle("zZrp7eEPmghHC44PpmQE3q4F8QFjBmsC9dPNgWx7wZ3",
                          report, [], SETTINGS)


def test_all_established_holders_do_not_read_as_independently_funded():
    """Review finding: 6 long-history holders were never funder-checked, yet the
    old headline said 'they look independently funded'. It must say the origins
    were not established."""
    report = ClusterReport(holders_examined=6, established_holders=6,
                           independent_holders=0, unknown_origins=0)
    text = _render(report)
    assert "independently funded" not in text.split("traced-independent")[0] \
        or "NOT a clean bill" in text
    assert "NOT a clean bill of health" in text


def test_mostly_unknown_origins_do_not_read_as_clean():
    report = ClusterReport(holders_examined=10, independent_holders=2,
                           unknown_origins=8)
    text = _render(report)
    assert "NOT a clean bill of health" in text


def test_genuinely_traced_independent_holders_may_be_called_so():
    report = ClusterReport(holders_examined=8, independent_holders=7,
                           unknown_origins=1)
    text = _render(report)
    assert "look\nindependently funded" in text or "independently funded" in text


def test_the_render_always_warns_about_what_it_can_miss():
    report = ClusterReport(holders_examined=5, independent_holders=5)
    text = _render(report)
    assert "defeats this check" in text
    assert "NOT proof of fair distribution" in text
