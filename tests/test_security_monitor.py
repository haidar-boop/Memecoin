"""Tests for continuous security-change monitoring (Spec Part 18, Sections 10/13)."""

from meme_intelligence.alerts.notification_engine import events_from_security_changes
from meme_intelligence.analyzers.security_monitor import (
    detect_security_changes,
    extract_facts,
    merge_facts,
)
from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.core.models import SecurityProfile, TokenIdentity
from meme_intelligence.database.storage import Storage

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def profile(**overrides) -> SecurityProfile:
    defaults = dict(
        token=TOKEN, source="goplus",
        is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
        is_mintable=False, ownership_renounced=True, hidden_owner=False,
        can_take_back_ownership=False, has_blacklist=False, trading_pausable=False,
        is_freezable=False, balance_mutable=False, selfdestruct=False, is_proxy=False,
        buy_tax_percent=0.0, sell_tax_percent=0.0, tax_modifiable=False,
        fake_token=False, is_airdrop_scam=False,
        holder_count=2000, top_holder_percent=3.0, top10_holder_percent=22.0,
        creator_percent=1.5, lp_locked_percent=95.0,
    )
    defaults.update(overrides)
    return SecurityProfile(**defaults)


def baseline() -> dict:
    return extract_facts(profile())


def test_first_sighting_produces_no_changes():
    assert detect_security_changes(None, profile()) == []
    assert detect_security_changes({}, profile()) == []


def test_no_change_produces_no_changes():
    assert detect_security_changes(baseline(), profile()) == []


def test_honeypot_appearing_is_critical():
    changes = detect_security_changes(baseline(), profile(is_honeypot=True))
    assert changes[0].severity is AlertPriority.CRITICAL
    assert "honeypot" in changes[0].message


def test_ownership_unrenounced_is_critical():
    changes = detect_security_changes(baseline(), profile(ownership_renounced=False))
    assert changes[0].severity is AlertPriority.CRITICAL
    assert "no longer renounced" in changes[0].message


def test_mint_authority_appearing_is_critical():
    changes = detect_security_changes(baseline(), profile(is_mintable=True))
    assert changes[0].severity is AlertPriority.CRITICAL


def test_lp_unlock_is_critical():
    changes = detect_security_changes(baseline(), profile(lp_locked_percent=40.0))
    assert changes[0].severity is AlertPriority.CRITICAL
    assert "pullable" in changes[0].message


def test_tax_hike_is_high():
    changes = detect_security_changes(baseline(), profile(sell_tax_percent=12.0))
    assert changes[0].severity is AlertPriority.HIGH
    assert "12%" in changes[0].message


def test_blacklist_appearing_is_high():
    changes = detect_security_changes(baseline(), profile(has_blacklist=True))
    assert changes[0].severity is AlertPriority.HIGH


def test_concentration_creep_severities():
    medium = detect_security_changes(baseline(), profile(top10_holder_percent=28.0))
    assert medium[0].severity is AlertPriority.MEDIUM
    high = detect_security_changes(baseline(), profile(top10_holder_percent=35.0))
    assert high[0].severity is AlertPriority.HIGH


def test_holder_drain_is_medium():
    changes = detect_security_changes(baseline(), profile(holder_count=1400))
    assert changes[0].severity is AlertPriority.MEDIUM
    assert "fell 30%" in changes[0].message


def test_unknown_transitions_never_alarm():
    """known->unknown and unknown->known are not changes (Rule 8)."""
    prev = baseline()
    now_unknown = detect_security_changes(prev, profile(lp_locked_percent=None))
    assert now_unknown == []

    prev_unknown = dict(prev, is_honeypot=None)
    newly_known = detect_security_changes(prev_unknown, profile(is_honeypot=True))
    assert all(c.field != "is_honeypot" for c in newly_known)


def test_merge_facts_keeps_last_known_values():
    merged = merge_facts(baseline(), extract_facts(profile(lp_locked_percent=None,
                                                           sell_tax_percent=3.0)))
    assert merged["lp_locked_percent"] == 95.0  # facts don't un-happen
    assert merged["sell_tax_percent"] == 3.0


def test_changes_sorted_worst_first():
    changes = detect_security_changes(
        baseline(),
        profile(top10_holder_percent=28.0, sell_tax_percent=12.0, is_honeypot=True),
    )
    severities = [c.severity for c in changes]
    assert severities[0] is AlertPriority.CRITICAL
    assert severities.index(AlertPriority.HIGH) < severities.index(AlertPriority.MEDIUM)


def test_events_grouped_per_severity():
    changes = detect_security_changes(
        baseline(),
        profile(is_honeypot=True, sell_tax_percent=12.0, top10_holder_percent=28.0),
    )
    events = events_from_security_changes(TOKEN, changes)
    priorities = {e.priority for e in events}
    assert priorities == {AlertPriority.CRITICAL, AlertPriority.HIGH, AlertPriority.MEDIUM}
    critical = next(e for e in events if e.priority is AlertPriority.CRITICAL)
    assert critical.alert_type == "security_change"
    assert critical.monitoring  # what to do next is always included


def test_live_sell_route_lost_is_critical():
    prev = dict(baseline(), live_buy_route_found=True, live_sell_route_found=True,
                live_round_trip_loss_percent=2.0)
    changes = detect_security_changes(
        prev, profile(live_buy_route_found=True, live_sell_route_found=False,
                      live_round_trip_loss_percent=None),
    )
    assert changes[0].severity is AlertPriority.CRITICAL
    assert changes[0].field == "live_sell_route_found"
    assert "no sell route" in changes[0].message


def test_round_trip_loss_hike_is_high():
    prev = dict(baseline(), live_buy_route_found=True, live_sell_route_found=True,
                live_round_trip_loss_percent=5.0)
    changes = detect_security_changes(
        prev, profile(live_buy_route_found=True, live_sell_route_found=True,
                      live_round_trip_loss_percent=30.0),
    )
    assert changes[0].severity is AlertPriority.HIGH
    assert changes[0].field == "live_round_trip_loss_percent"
    assert "5% -> 30%" in changes[0].message


def test_live_buy_route_flip_alone_produces_no_change():
    """buy-route disappearing is persisted for reference but not wired into any danger check."""
    prev = dict(baseline(), live_buy_route_found=True)
    changes = detect_security_changes(prev, profile(live_buy_route_found=False))
    assert changes == []


def test_storage_roundtrip():
    with Storage(":memory:") as storage:
        assert storage.latest_security_facts(TOKEN) is None
        storage.record_security_facts(TOKEN, baseline())
        facts = storage.latest_security_facts(TOKEN)
        assert facts["lp_locked_percent"] == 95.0
        storage.record_security_facts(TOKEN, dict(facts, sell_tax_percent=9.0))
        assert storage.latest_security_facts(TOKEN)["sell_tax_percent"] == 9.0
