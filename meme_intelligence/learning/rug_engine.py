"""Hard-signal rug-pull detection engine (Section 5a).

Complements the *learned* rug detection (Section 5b — confirmed rugs live in
the analog index, so a coin whose fingerprint sits near past rugs is flagged
by resemblance even when its on-chain signals look clean). This module is the
rules half: it reads concrete on-chain / market facts and sums weighted signal
points into an explainable 0-100 rug-risk score.

Reuse over reinvention (Rule 18): the contract-level facts — honeypot /
un-sellable, mint & freeze authority, holder concentration, LP lock, sell tax,
same-creator honeypot count — are already normalized into
:class:`~meme_intelligence.core.models.SecurityProfile` by the existing GoPlus
collector. The engine consumes that profile rather than re-collecting. The
trajectory snapshots supply the *dynamic* signals (liquidity removal, dev
dumping, wash-traded volume), and the deployer blacklist (grown from confirmed
rugs) supplies reputation.

Every signal fires only on positive evidence: missing/unknown data never
fires a rug signal (unknown is not guilt — Rule 8). Each fired signal is
returned with the points it added and a human-readable reason, so an alert can
explain *why* a coin was flagged (Section 5a — explainable).

The *active* un-sellable check the spec describes (Solana Jupiter round-trip,
EVM ``eth_call`` sell simulation) requires live RPC and is a separate collector;
this engine exposes an ``unsellable_override`` seam so that simulator's result
plugs in when available, falling back to the GoPlus honeypot flags otherwise.
"""

from __future__ import annotations

from typing import Sequence

from meme_intelligence.config.settings import RugSignalWeights, RugThresholds
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import SecurityProfile
from meme_intelligence.learning.models import CoinSnapshot, RugAssessment, RugSignal

_logger = get_logger("learning.rug_engine")

_MAX_SCORE = 100.0


def _latest(snapshots: Sequence[CoinSnapshot], attr: str):
    """Most recent non-None value of ``attr`` across the trajectory."""
    for snap in reversed(sorted(snapshots, key=lambda s: s.age_seconds)):
        value = getattr(snap, attr)
        if value is not None:
            return value
    return None


def _peak(snapshots: Sequence[CoinSnapshot], attr: str):
    values = [getattr(s, attr) for s in snapshots if getattr(s, attr) is not None]
    return max(values) if values else None


class RugEngine:
    """Scores rug-pull risk from hard on-chain + trajectory + reputation signals."""

    def __init__(self, weights: RugSignalWeights, thresholds: RugThresholds) -> None:
        self._w = weights
        self._t = thresholds

    def assess(
        self,
        *,
        security: SecurityProfile | None = None,
        snapshots: Sequence[CoinSnapshot] = (),
        deployer_rug_count: int = 0,
        unsellable_override: bool | None = None,
    ) -> RugAssessment:
        """Compute the 0-100 rug-risk score and the signals that fired.

        ``unsellable_override`` — when a live sell-simulation result is
        available (True = cannot sell), it wins over the GoPlus honeypot flags.
        ``None`` means "no active check ran", so the profile flags are used.
        """
        snapshots = tuple(snapshots)
        signals: list[RugSignal] = []

        self._check_unsellable(signals, security, unsellable_override)
        self._check_authorities(signals, security)
        self._check_concentration(signals, security)
        self._check_lp_lock(signals, security)
        self._check_sell_tax(signals, security)
        self._check_liquidity_removal(signals, snapshots)
        self._check_dev_dumping(signals, snapshots)
        self._check_fake_volume(signals, security, snapshots)
        self._check_deployer(signals, security, deployer_rug_count)

        score = min(_MAX_SCORE, sum(s.points for s in signals))
        if signals:
            _logger.info("rug assessment: score=%.0f signals=%s",
                         score, [s.name for s in signals])
        return RugAssessment(score=score, signals=tuple(signals))

    # ---- Individual signals (each fires only on positive evidence) ----

    def _check_unsellable(self, signals, security, override) -> None:
        cannot_sell = override
        detail = "active sell simulation failed" if override else None
        if cannot_sell is None and security is not None:
            if security.is_honeypot is True or security.cannot_sell_all is True:
                cannot_sell = True
                detail = "honeypot / sell disabled (contract analysis)"
        if cannot_sell:
            signals.append(RugSignal("unsellable", self._w.unsellable, detail))

    def _check_authorities(self, signals, security) -> None:
        if security is None:
            return
        if security.is_mintable is True:
            signals.append(RugSignal("mint_authority_active", self._w.mint_authority_active,
                                     "owner can mint new supply"))
        if security.is_freezable is True:
            signals.append(RugSignal("freeze_authority_active", self._w.freeze_authority_active,
                                     "owner can freeze holder balances"))

    def _check_concentration(self, signals, security) -> None:
        if security is None:
            return
        top1 = security.top_holder_percent
        top10 = security.top10_holder_percent
        fired_top1 = top1 is not None and top1 > self._t.top_holder_percent_max
        fired_top10 = top10 is not None and top10 > self._t.top10_holder_percent_max
        if fired_top1 or fired_top10:
            parts = []
            if fired_top1:
                parts.append(f"top holder {top1:.0f}%")
            if fired_top10:
                parts.append(f"top 10 hold {top10:.0f}%")
            signals.append(RugSignal("top_holder_concentration",
                                     self._w.top_holder_concentration, ", ".join(parts)))

    def _check_lp_lock(self, signals, security) -> None:
        if security is None or security.lp_locked_percent is None:
            return  # unknown lock status never fires (Rule 8)
        if security.lp_locked_percent < self._t.min_lp_locked_percent:
            signals.append(RugSignal("liquidity_unlocked", self._w.liquidity_unlocked,
                                     f"only {security.lp_locked_percent:.0f}% of LP locked"))

    def _check_sell_tax(self, signals, security) -> None:
        if security is None or security.sell_tax_percent is None:
            return
        if security.sell_tax_percent >= self._t.sell_tax_max_percent:
            signals.append(RugSignal("high_sell_tax", self._w.high_sell_tax,
                                     f"sell tax {security.sell_tax_percent:.0f}%"))

    def _check_liquidity_removal(self, signals, snapshots) -> None:
        if not snapshots:
            return
        # A single large LP-removal event, or a sharp fall from the trajectory
        # peak, indicates liquidity being pulled in real time (Section 5a).
        removal_event = min(
            (s.liquidity_event_usd for s in snapshots if s.liquidity_event_usd is not None),
            default=None,
        )
        peak = _peak(snapshots, "liquidity_usd")
        latest = _latest(snapshots, "liquidity_usd")
        fired = False
        detail = None
        if removal_event is not None and removal_event <= -self._t.liquidity_removal_usd:
            fired = True
            detail = f"LP remove event ${abs(removal_event):,.0f}"
        elif peak is not None and latest is not None and peak > 0:
            drop_pct = (peak - latest) / peak * 100.0
            if drop_pct >= self._t.liquidity_drop_percent:
                fired = True
                detail = f"liquidity down {drop_pct:.0f}% from peak"
        if fired:
            signals.append(RugSignal("liquidity_removed", self._w.liquidity_removed, detail))

    def _check_dev_dumping(self, signals, snapshots) -> None:
        outflow = _peak(snapshots, "dev_outflow_usd")
        if outflow is not None and outflow >= self._t.dev_dump_usd:
            signals.append(RugSignal("dev_wallet_dumping", self._w.dev_wallet_dumping,
                                     f"creator outflow ${outflow:,.0f}"))

    def _check_fake_volume(self, signals, security, snapshots) -> None:
        holders = _latest(snapshots, "holder_count")
        if holders is None and security is not None:
            holders = security.holder_count
        volume = _latest(snapshots, "volume_1h_usd")
        if holders is None or volume is None or holders <= 0:
            return
        if volume < self._t.fake_volume_min_volume_usd:
            return  # too little volume to call wash trading
        per_holder = volume / holders
        if per_holder >= self._t.fake_volume_per_holder_usd:
            signals.append(RugSignal("fake_volume", self._w.fake_volume,
                                     f"${per_holder:,.0f} volume per holder ({holders} holders)"))

    def _check_deployer(self, signals, security, deployer_rug_count) -> None:
        same_creator = security.honeypot_same_creator_count if security is not None else None
        linked = deployer_rug_count > 0 or (same_creator is not None and same_creator > 0)
        if linked:
            parts = []
            if deployer_rug_count > 0:
                parts.append(f"{deployer_rug_count} prior rug(s) by this deployer")
            if same_creator:
                parts.append(f"{same_creator} same-creator honeypot(s)")
            signals.append(RugSignal("deployer_blacklisted", self._w.deployer_blacklisted,
                                     "; ".join(parts)))
