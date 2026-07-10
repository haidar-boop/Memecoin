"""Continuous security-change monitoring (Spec Part 18, Sections 10 and 13).

A token that was safe yesterday is not safe today just because yesterday's
scan said so. This module persists the *security facts* observed at each
analysis and diffs every new analysis against the last known facts:

* CRITICAL changes (honeypot appearing, ownership un-renounced, mint
  authority appearing, LP unlock, the live Jupiter sell route disappearing)
  demand an immediate high-priority security review — they are exactly how
  rugs begin.
* HIGH changes (new blacklist/pause powers, tax hikes, freeze authority,
  a rising live round-trip sell loss) demand prompt review.
* MEDIUM changes (concentration creeping up, holders draining) are the
  slow-motion warnings of Section 13.

A fact moving from *unknown to known* is recorded as information, not a
change-alarm; a fact moving from known to unknown keeps the last known
value on record (sources flicker; facts don't un-happen — Rule 8).
"""

from __future__ import annotations

from dataclasses import dataclass

from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.core.models import SecurityProfile

# Boolean facts where flipping to True is dangerous, with severity.
_BOOL_DANGER_FIELDS: dict[str, tuple[AlertPriority, str]] = {
    "is_honeypot": (AlertPriority.CRITICAL, "token became a honeypot"),
    "cannot_buy": (AlertPriority.CRITICAL, "buying became blocked"),
    "cannot_sell_all": (AlertPriority.CRITICAL, "holders can no longer sell their full balance"),
    "is_mintable": (AlertPriority.CRITICAL, "mint authority appeared: supply can now be inflated"),
    "fake_token": (AlertPriority.CRITICAL, "token was flagged as counterfeit"),
    "is_airdrop_scam": (AlertPriority.CRITICAL, "token was flagged as an airdrop scam"),
    "balance_mutable": (AlertPriority.CRITICAL, "an authority can now modify wallet balances"),
    "hidden_owner": (AlertPriority.HIGH, "a hidden owner appeared"),
    "can_take_back_ownership": (AlertPriority.HIGH, "ownership became reclaimable"),
    "has_blacklist": (AlertPriority.HIGH, "a blacklist function appeared"),
    "trading_pausable": (AlertPriority.HIGH, "trading became pausable"),
    "is_freezable": (AlertPriority.HIGH, "freeze authority appeared"),
    "is_proxy": (AlertPriority.HIGH, "contract became upgradeable (proxy)"),
    "selfdestruct": (AlertPriority.HIGH, "self-destruct capability appeared"),
    "tax_modifiable": (AlertPriority.HIGH, "trading tax became modifiable"),
}

# Numeric facts: (worsens_when, threshold_delta, severity, message template).
_LP_UNLOCK_CRITICAL_DROP = 30.0   # percentage points of LP lock disappearing
_TAX_HIKE_HIGH_POINTS = 5.0
_CONCENTRATION_HIGH_POINTS = 10.0
_CONCENTRATION_MEDIUM_POINTS = 5.0
_HOLDER_DROP_MEDIUM_FRACTION = 0.20
_ROUND_TRIP_LOSS_HIKE_HIGH_POINTS = 20.0

# The facts worth persisting for diffs (superset of the fields above).
FACT_FIELDS = tuple(_BOOL_DANGER_FIELDS) + (
    "ownership_renounced",
    "buy_tax_percent",
    "sell_tax_percent",
    "lp_locked_percent",
    "top_holder_percent",
    "top10_holder_percent",
    "creator_percent",
    "holder_count",
    "live_buy_route_found",
    "live_sell_route_found",
    "live_round_trip_loss_percent",
)


@dataclass(frozen=True)
class SecurityChange:
    """One detected deterioration in a token's security facts."""

    field: str
    severity: AlertPriority
    message: str
    previous: object
    current: object


def extract_facts(profile: SecurityProfile) -> dict:
    """The persistable security facts from one analysis (unknowns included as None)."""
    return {name: getattr(profile, name) for name in FACT_FIELDS}


def merge_facts(previous: dict | None, current: dict) -> dict:
    """Facts to persist: current values, keeping last-known where now unknown."""
    if not previous:
        return current
    return {
        name: current.get(name) if current.get(name) is not None else previous.get(name)
        for name in FACT_FIELDS
    }


def detect_security_changes(previous: dict | None, profile: SecurityProfile) -> list[SecurityChange]:
    """Diff current security facts against the last known facts (Section 10).

    Only *known -> known* transitions count as changes; a first sighting or
    a newly-measured fact produces no alarm (there is nothing it changed
    from). Returns changes worst-first.
    """
    if not previous:
        return []
    current = extract_facts(profile)
    changes: list[SecurityChange] = []

    def known(name: str) -> bool:
        return previous.get(name) is not None and current.get(name) is not None

    # Dangerous booleans flipping on.
    for name, (severity, message) in _BOOL_DANGER_FIELDS.items():
        if known(name) and not previous[name] and current[name]:
            changes.append(SecurityChange(name, severity, message,
                                          previous[name], current[name]))

    # Ownership renounced -> un-renounced (reverse-polarity boolean).
    if known("ownership_renounced") and previous["ownership_renounced"] \
            and not current["ownership_renounced"]:
        changes.append(SecurityChange(
            "ownership_renounced", AlertPriority.CRITICAL,
            "ownership is no longer renounced: owner control returned",
            True, False,
        ))

    # Live sell test losing its sell route (Project 1) -- a token that could
    # be sold yesterday and can't today is exactly how a rug begins.
    if known("live_sell_route_found") and previous["live_sell_route_found"] \
            and not current["live_sell_route_found"]:
        changes.append(SecurityChange(
            "live_sell_route_found", AlertPriority.CRITICAL,
            "live Jupiter test: token could be sold, now has no sell route",
            True, False,
        ))

    # LP lock evaporating.
    if known("lp_locked_percent"):
        drop = previous["lp_locked_percent"] - current["lp_locked_percent"]
        if drop >= _LP_UNLOCK_CRITICAL_DROP:
            changes.append(SecurityChange(
                "lp_locked_percent", AlertPriority.CRITICAL,
                f"LP lock fell {drop:.0f} points "
                f"({previous['lp_locked_percent']:.0f}% -> {current['lp_locked_percent']:.0f}%): "
                "liquidity became pullable",
                previous["lp_locked_percent"], current["lp_locked_percent"],
            ))

    # Tax hikes.
    for name in ("buy_tax_percent", "sell_tax_percent"):
        if known(name) and current[name] - previous[name] >= _TAX_HIKE_HIGH_POINTS:
            changes.append(SecurityChange(
                name, AlertPriority.HIGH,
                f"{name.replace('_percent', '').replace('_', ' ')} rose "
                f"{previous[name]:.0f}% -> {current[name]:.0f}%",
                previous[name], current[name],
            ))

    # Live round-trip sell loss rising (Project 1).
    if known("live_round_trip_loss_percent") and \
            current["live_round_trip_loss_percent"] - previous["live_round_trip_loss_percent"] \
            >= _ROUND_TRIP_LOSS_HIKE_HIGH_POINTS:
        changes.append(SecurityChange(
            "live_round_trip_loss_percent", AlertPriority.HIGH,
            f"live round-trip sell loss rose {previous['live_round_trip_loss_percent']:.0f}% -> "
            f"{current['live_round_trip_loss_percent']:.0f}%",
            previous["live_round_trip_loss_percent"], current["live_round_trip_loss_percent"],
        ))

    # Concentration creep (Section 13 medium/high warnings).
    for name in ("top_holder_percent", "top10_holder_percent"):
        if known(name):
            rise = current[name] - previous[name]
            if rise >= _CONCENTRATION_HIGH_POINTS:
                changes.append(SecurityChange(
                    name, AlertPriority.HIGH,
                    f"{name.replace('_', ' ')} rose {rise:.1f} points "
                    f"({previous[name]:.1f}% -> {current[name]:.1f}%)",
                    previous[name], current[name],
                ))
            elif rise >= _CONCENTRATION_MEDIUM_POINTS:
                changes.append(SecurityChange(
                    name, AlertPriority.MEDIUM,
                    f"{name.replace('_', ' ')} creeping up "
                    f"({previous[name]:.1f}% -> {current[name]:.1f}%)",
                    previous[name], current[name],
                ))

    # Holder base draining.
    if known("holder_count") and previous["holder_count"] > 0:
        drop_fraction = (previous["holder_count"] - current["holder_count"]) / previous["holder_count"]
        if drop_fraction >= _HOLDER_DROP_MEDIUM_FRACTION:
            changes.append(SecurityChange(
                "holder_count", AlertPriority.MEDIUM,
                f"holder count fell {drop_fraction:.0%} "
                f"({previous['holder_count']} -> {current['holder_count']})",
                previous["holder_count"], current["holder_count"],
            ))

    order = [AlertPriority.CRITICAL, AlertPriority.HIGH, AlertPriority.MEDIUM, AlertPriority.LOW]
    changes.sort(key=lambda c: order.index(c.severity))
    return changes
