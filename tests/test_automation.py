"""Tests for automation rules and alert dispatch (Spec Part 13)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.alerts.notification_engine import (
    _BUY_SIDE_ALERT_TYPES,
    _PROTECTIVE_ALERT_TYPES,
    AlertEvent,
    AutomationRules,
    NotificationEngine,
)
from meme_intelligence.config.settings import AlertEngineSettings, AlertThresholds, Settings
from meme_intelligence.core.errors import ConfigurationError
from meme_intelligence.core.enums import AlertPriority, MarketRegime
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")
SETTINGS = Settings.from_env(env={})


def make_pair(**overrides) -> DexPair:
    defaults = dict(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=90_000.0,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )
    defaults.update(overrides)
    return DexPair(**defaults)


def make_profile(honeypot=False, **overrides) -> SecurityProfile:
    import dataclasses as _dc
    base = SecurityProfile(
        token=TOKEN, source="goplus",
        is_honeypot=honeypot, cannot_buy=False, cannot_sell_all=False,
        is_open_source=True, is_proxy=False, is_mintable=False,
        ownership_renounced=True, hidden_owner=False, can_take_back_ownership=False,
        has_blacklist=False, trading_pausable=False, is_freezable=False,
        balance_mutable=False, selfdestruct=False,
        buy_tax_percent=0.0, sell_tax_percent=0.0, tax_modifiable=False,
        fake_token=False, is_airdrop_scam=False, anti_whale_modifiable=False,
        slippage_modifiable=False, personal_slippage_modifiable=False,
        trading_cooldown=False, honeypot_same_creator_count=0,
        holder_count=2500, top_holder_percent=3.0, top10_holder_percent=22.0,
        creator_percent=1.5, owner_percent=0.0, lp_locked_percent=95.0,
    )
    return _dc.replace(base, **overrides) if overrides else base


class OneShotGoPlus:
    def __init__(self, profile):
        self.profile = profile

    async def get_token_security(self, chain, address):
        return self.profile


async def pipeline_result(honeypot=False, pair=None, profile=None):
    prof = profile if profile is not None else make_profile(honeypot)
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(prof), now_func=lambda: NOW)
    return await pipeline.analyze_pair(pair or make_pair(), regime=MarketRegime.NEUTRAL)


def make_rules(now=lambda: NOW) -> AutomationRules:
    return AutomationRules(AlertThresholds(), AlertEngineSettings(), now_func=now)


async def test_strong_fresh_token_gets_high_strong_candidate_alert():
    """A fresh launch that clears the raised overall bar with every
    measurable gate passing (only community unverified) earns a HIGH
    strong_candidate alert — not capped at MEDIUM (Part 2 S4)."""
    result = await pipeline_result()  # this fixture scores ~93 overall
    assert result.master.final_score >= 88.0  # sanity: it IS a strong candidate
    events = make_rules().evaluate(result)
    types = {e.alert_type: e for e in events}
    assert "strong_candidate" in types
    assert types["strong_candidate"].priority is AlertPriority.HIGH
    assert any("community" in r or "unverified" in r.lower()
               for r in (types["strong_candidate"].title,) + types["strong_candidate"].reasons)
    # the full "every gate verified" tier still requires community data
    assert "high_priority_opportunity" not in types


async def test_shallow_liquidity_vetoes_strong_candidate():
    """A gate-passing fresh launch with pool depth below the floor is
    downgraded to MEDIUM with the depth named — the liquidity GATE scores
    lock safety, not depth, so this veto is what keeps $16k pools off a
    HIGH-filtered phone."""
    rules = AutomationRules(
        AlertThresholds(strong_candidate_min_liquidity_usd=100_000.0),  # fixture has 90k
        AlertEngineSettings())
    result = await pipeline_result()
    assert result.master.final_score >= 88.0  # would otherwise be a strong candidate
    events = rules.evaluate(result)
    types = {e.alert_type: e for e in events}
    assert "strong_candidate" not in types
    assert types["early_opportunity"].priority is AlertPriority.MEDIUM
    assert any("liquidity depth" in r for r in types["early_opportunity"].reasons)


async def test_low_ai_confidence_vetoes_strong_candidate():
    """A lukewarm AI verification (below the confidence floor) downgrades the
    alert instead of riding along as a footnote."""
    import dataclasses
    from types import SimpleNamespace

    result = await pipeline_result()
    judged = dataclasses.replace(result, ai_judgment=SimpleNamespace(confidence=22.0))
    events = make_rules().evaluate(judged)
    types = {e.alert_type: e for e in events}
    assert "strong_candidate" not in types
    assert any("AI verification confidence" in r
               for r in types["early_opportunity"].reasons)


async def test_deterministic_veto_suppresses_buy_side_alerts():
    """A rug/risk/copycat screen veto now SUPPRESSES the buy-side alert
    entirely — it used to downgrade to a MEDIUM 'provisional' early_opportunity
    that a MEDIUM-threshold phone still buzzed for (a demoted rug reaching the
    operator once he lowered his threshold). No buy-side alert of any tier
    survives the veto."""
    import dataclasses
    from types import SimpleNamespace

    result = await pipeline_result()
    # Give it a community score so it clears the fully-verified tier.
    verified = dataclasses.replace(
        result, community=SimpleNamespace(overall_score=85.0, is_artificial=False,
                                          findings=()))
    events = make_rules().evaluate(
        verified, deterministic_risk_veto="rug engine score 25 (deployer_blacklisted)")
    types = {e.alert_type for e in events}
    assert not (types & _BUY_SIDE_ALERT_TYPES)   # nothing buy-side reaches the phone


async def test_inconclusive_ai_verification_vetoes_strong_candidate():
    """Bug-hunt regression: a judgment DISCARDED below the confidence floor
    left ai_judgment None, so a 15/100 judgment fired HIGH while 22/100
    vetoed — inverted protection. The scanner now reports 'verification ran
    but was inconclusive' and the rules treat it as unconfirmed."""
    result = await pipeline_result()
    events = make_rules().evaluate(result, ai_verification_inconclusive=True)
    types = {e.alert_type: e for e in events}
    assert "strong_candidate" not in types
    assert any("no usable judgment" in r for r in types["early_opportunity"].reasons)
    # Default (verification never ran, e.g. no API key) is unchanged: HIGH fires.
    default_events = make_rules().evaluate(result)
    assert "strong_candidate" in {e.alert_type for e in default_events}


async def test_confident_ai_keeps_strong_candidate_high():
    import dataclasses
    from types import SimpleNamespace

    result = await pipeline_result()
    judged = dataclasses.replace(result, ai_judgment=SimpleNamespace(confidence=80.0))
    events = make_rules().evaluate(judged)
    types = {e.alert_type: e for e in events}
    assert "strong_candidate" in types
    assert types["strong_candidate"].priority is AlertPriority.HIGH


async def test_good_but_not_strong_token_stays_medium_provisional():
    """A token that passes the gates but does NOT clear the raised strong
    bar stays a MEDIUM early_opportunity (Rule 8 — unverified community).
    Uses a high strong-candidate bar so the healthy fixture (~93) passes
    'overall' but falls below it, landing deterministically in the MEDIUM
    provisional tier."""
    from meme_intelligence.config.settings import AlertThresholds
    rules = AutomationRules(AlertThresholds(strong_candidate_overall=99.0),
                            AlertEngineSettings())
    result = await pipeline_result()
    events = rules.evaluate(result)
    types = {e.alert_type: e for e in events}
    assert "early_opportunity" in types
    assert types["early_opportunity"].priority is AlertPriority.MEDIUM
    assert "strong_candidate" not in types
    assert "high_priority_opportunity" not in types


async def test_honeypot_triggers_critical_emergency():
    result = await pipeline_result(honeypot=True)
    events = make_rules().evaluate(result)
    assert any(e.alert_type == "emergency_review" and e.priority is AlertPriority.CRITICAL
               for e in events)
    assert not any("opportunity" in e.alert_type for e in events)


async def test_failed_gate_produces_no_opportunity_alert():
    weak_pair = make_pair(liquidity_usd=1_000.0)  # fails liquidity gate hard
    result = await pipeline_result(pair=weak_pair)
    events = make_rules().evaluate(result)
    assert not any("opportunity" in e.alert_type for e in events)


async def test_momentum_alert_fires_through_gate():
    """Part 15 Section 5: momentum alert when growth signals align."""
    hot_pair = make_pair(volume_1h=20_000.0, buys_1h=60, sells_1h=10,
                         price_change_24h=25.0, price_change_6h=12.0, price_change_1h=5.0)
    result = await pipeline_result(pair=hot_pair)
    assert result.momentum.overall_score >= 70  # sanity: gate actually reachable
    events = make_rules().evaluate(result)
    momentum_events = [e for e in events if e.alert_type == "momentum"]
    assert momentum_events
    assert momentum_events[0].priority is AlertPriority.MEDIUM
    assert any("coverage" in r for r in momentum_events[0].reasons)


async def test_no_momentum_alert_in_late_zone():
    """Accelerating into a blow-off is not an opportunity signal."""
    extended = make_pair(price_change_24h=250.0, volume_1h=20_000.0,
                         buys_1h=60, sells_1h=10,
                         pair_created_at=NOW - timedelta(days=3))
    result = await pipeline_result(pair=extended)
    events = make_rules().evaluate(result)
    assert not any(e.alert_type == "momentum" for e in events)


async def test_score_drop_rule():
    result = await pipeline_result()
    events = make_rules().evaluate(result, previous_score=result.master.final_score + 20)
    drop = [e for e in events if e.alert_type == "score_drop_review"]
    assert drop and drop[0].priority is AlertPriority.HIGH

    events_small = make_rules().evaluate(result, previous_score=result.master.final_score + 5)
    assert not any(e.alert_type == "score_drop_review" for e in events_small)


# ---- Declining-score suppression for buy-side alerts (bug-hunt finding) ----

async def test_declining_score_suppresses_the_weak_opportunity_tier():
    """A coin whose score just collapsed is not a fresh early opportunity,
    whatever its current absolute score still clears — the exact re-pitch a
    real coin hit: a day-old coin, liquidity roughly halved, score 90 -> 65,
    still fired a MEDIUM early_opportunity in the same cycle its own
    score_drop_review flagged it as declining."""
    rules = AutomationRules(
        # Forces the WEAK "early_opportunity" fallback instead of the strict
        # strong_candidate tier (fixture liquidity is 90k) — matching the
        # real coin, which never cleared the strict bar either.
        AlertThresholds(strong_candidate_min_liquidity_usd=100_000.0),
        AlertEngineSettings())
    result = await pipeline_result()
    events = rules.evaluate(result, previous_score=result.master.final_score + 25)
    types = {e.alert_type for e in events}
    assert "early_opportunity" not in types        # the weak re-pitch is stopped
    assert "score_drop_review" in types             # the protective alert still fires


async def test_declining_score_does_not_hide_a_genuinely_strong_candidate():
    """A coin that STILL clears the strict strong-candidate bar despite a
    decline is a rare enough signal that both alerts should reach the
    operator at full priority, not be hidden — matches the existing
    same-batch-interest contract for score_drop_review."""
    result = await pipeline_result()   # fires HIGH strong_candidate
    events = make_rules().evaluate(result, previous_score=result.master.final_score + 25)
    types = {e.alert_type for e in events}
    assert "strong_candidate" in types
    assert "score_drop_review" in types


async def test_minor_score_dip_does_not_suppress_buy_side():
    """A small re-scoring wobble below the review threshold is not a
    decline — buy-side alerts are unaffected."""
    result = await pipeline_result()
    events = make_rules().evaluate(
        result, previous_score=result.master.final_score + 5)  # below the 15pt threshold
    types = {e.alert_type for e in events}
    assert types & _BUY_SIDE_ALERT_TYPES
    assert "score_drop_review" not in types


async def test_fresh_discovery_never_suppressed_as_declining():
    """A coin's first-ever look has no previous_score — it is never
    penalized for being new."""
    result = await pipeline_result()
    events = make_rules().evaluate(result)  # previous_score defaults to None
    types = {e.alert_type for e in events}
    assert types & _BUY_SIDE_ALERT_TYPES


async def test_score_drop_review_unaffected_by_its_own_suppression():
    """The protective alert is unaffected by the new rule — only the
    buy-side re-pitch is stopped, not the warning that triggered it."""
    result = await pipeline_result()
    events = make_rules().evaluate(result, previous_score=result.master.final_score + 25)
    drop = [e for e in events if e.alert_type == "score_drop_review"]
    assert drop and drop[0].priority is AlertPriority.HIGH


# ---- Peak-decline suppression (operator complaint 2026-07-14) ----

async def test_slow_creep_below_peak_suppresses_weak_tier():
    """The escape the one-step check missed: a collapsed coin creeping back
    +2-3 points per recheck reads as 'improving' on every single look, yet
    is still far below its own peak days later — it must not re-pitch as a
    fresh opportunity. previous_score is BELOW current here (the creep-up
    step), so only the peak comparison can catch it."""
    rules = AutomationRules(
        AlertThresholds(strong_candidate_min_liquidity_usd=100_000.0),  # force weak tier
        AlertEngineSettings())
    result = await pipeline_result()
    events = rules.evaluate(result,
                            previous_score=result.master.final_score - 3,  # creeping up
                            peak_score=result.master.final_score + 25)     # far below peak
    types = {e.alert_type for e in events}
    assert "early_opportunity" not in types
    assert "score_drop_review" not in types   # no one-step drop -> no drop alert


async def test_below_peak_does_not_hide_a_genuinely_strong_candidate():
    """Same exemption as the decline check: a coin that STILL clears the
    strict strong-candidate bar while below its peak reaches the operator."""
    result = await pipeline_result()
    events = make_rules().evaluate(result,
                                   previous_score=result.master.final_score - 3,
                                   peak_score=result.master.final_score + 25)
    assert "strong_candidate" in {e.alert_type for e in events}


async def test_near_peak_recovery_is_not_suppressed():
    """A coin back within the threshold of its own best self is a genuinely
    renewed signal, not a stale re-pitch."""
    result = await pipeline_result()
    events = make_rules().evaluate(result,
                                   previous_score=result.master.final_score - 3,
                                   peak_score=result.master.final_score + 10)  # within 15
    assert {e.alert_type for e in events} & _BUY_SIDE_ALERT_TYPES


async def test_no_peak_history_keeps_previous_behavior():
    """peak_score=None (first look, or a caller that doesn't track history)
    must never suppress — exact pre-fix behavior (Rule 18)."""
    result = await pipeline_result()
    events = make_rules().evaluate(result, previous_score=None, peak_score=None)
    assert {e.alert_type for e in events} & _BUY_SIDE_ALERT_TYPES


# ---- Dead-token post-mortem (Part 29 Section 1) ----

async def test_dead_token_gets_single_postmortem_not_warning_spam():
    """A collapsed pool is a completed failure: one MEDIUM post-mortem, not
    a HIGH risk-warning + HIGH score-drop pair on a token nobody holds."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=30.0))
    events = make_rules().evaluate(result, previous_score=90.0)
    assert [e.alert_type for e in events] == ["token_death"]
    death = events[0]
    assert death.priority is AlertPriority.MEDIUM
    assert any("collapsed" in r for r in death.reasons)
    assert any("90" in r for r in death.reasons)  # score history preserved


async def test_dead_honeypot_still_raises_critical_emergency():
    """Anyone already holding still needs the destructive finding."""
    result = await pipeline_result(honeypot=True, pair=make_pair(liquidity_usd=10.0))
    events = make_rules().evaluate(result)
    types = {e.alert_type for e in events}
    assert "emergency_review" in types
    assert "token_death" in types
    assert "risk_warning" not in types
    assert not any("opportunity" in t or t == "momentum" for t in types)


async def test_nan_liquidity_is_not_death():
    """Bug-hunt: `nan >= dead_floor` is always False (same hazard as
    `nan <= 0` elsewhere), so the bail-out check let NaN liquidity fall
    through to 'dead' instead of being excluded like unknown liquidity —
    misclassifying a token with simply-unmeasurable liquidity as dead and
    suppressing every real alert for it."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=float("nan")))
    events = make_rules().evaluate(result)
    assert not any(e.alert_type == "token_death" for e in events)


async def test_unknown_liquidity_is_not_death():
    """Absence of data never becomes a conclusion (Rule 8)."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=None))
    events = make_rules().evaluate(result)
    assert not any(e.alert_type == "token_death" for e in events)


async def test_sinking_but_alive_token_keeps_high_risk_warning():
    """Below the $5k minimum but above the dead floor is still a live,
    decision-relevant deterioration — the HIGH warning stays (for a token
    the operator was pointed at; see the interest-gate tests below)."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=3_000.0))
    events = make_rules().evaluate(result)
    warnings = [e for e in events if e.alert_type == "risk_warning"]
    assert warnings and warnings[0].priority is AlertPriority.HIGH
    assert not any(e.alert_type == "token_death" for e in events)


# ---- Interest gate (Part 29 Section 1: alerts protect decisions) ----
# The operator only learns about tokens through HIGH opportunity alerts, so
# protective alerts on a token that never earned one guard no possible
# decision — they demote to LOW (logged, recorded, but below every external
# sink's minimum priority). This is what stops dying pump.fun garbage from
# re-warning the phone every recheck.


async def test_interest_gate_demotes_risk_alerts_on_unrecommended_tokens():
    """A junk token the operator was never pointed at: HIGH risk_warning and
    HIGH score_drop_review both demote to LOW with the reason named."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=3_000.0))
    events = make_rules().evaluate(result, previous_score=result.master.final_score + 20,
                                   operator_interest=False)
    by_type = {e.alert_type: e for e in events}
    assert by_type["risk_warning"].priority is AlertPriority.LOW
    assert by_type["score_drop_review"].priority is AlertPriority.LOW
    assert any("interest gate" in r for r in by_type["risk_warning"].reasons)


async def test_interest_keeps_protective_alerts_at_full_priority():
    """The same junk token WITH prior operator interest keeps the HIGH pair —
    a token the operator may be holding still gets its warnings."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=3_000.0))
    events = make_rules().evaluate(result, previous_score=result.master.final_score + 20,
                                   operator_interest=True)
    by_type = {e.alert_type: e for e in events}
    assert by_type["risk_warning"].priority is AlertPriority.HIGH
    assert by_type["score_drop_review"].priority is AlertPriority.HIGH


async def test_interest_gate_demotes_critical_emergency_on_unrecommended_tokens():
    """Even a CRITICAL honeypot finding is informational on a token the
    operator was never told about — he cannot be holding it."""
    result = await pipeline_result(honeypot=True)
    events = make_rules().evaluate(result, operator_interest=False)
    emergency = next(e for e in events if e.alert_type == "emergency_review")
    assert emergency.priority is AlertPriority.LOW


async def test_interest_gate_demotes_death_postmortem_on_unrecommended_tokens():
    result = await pipeline_result(pair=make_pair(liquidity_usd=30.0))
    events = make_rules().evaluate(result, previous_score=90.0, operator_interest=False)
    death = next(e for e in events if e.alert_type == "token_death")
    assert death.priority is AlertPriority.LOW


async def test_same_batch_opportunity_grants_interest():
    """A HIGH opportunity firing in the SAME batch counts as interest:
    contradictory signals on a just-recommended token must both arrive at
    full priority."""
    result = await pipeline_result()  # fires HIGH strong_candidate
    events = make_rules().evaluate(result, previous_score=result.master.final_score + 20,
                                   operator_interest=False)
    by_type = {e.alert_type: e for e in events}
    assert by_type["strong_candidate"].priority is AlertPriority.HIGH
    assert by_type["score_drop_review"].priority is AlertPriority.HIGH  # not demoted


async def test_interest_gate_never_touches_opportunity_or_momentum_alerts():
    """Only protective alert types demote — opportunity/momentum signals on a
    new token ARE the operator's introduction to it."""
    hot_pair = make_pair(volume_1h=20_000.0, buys_1h=60, sells_1h=10,
                         price_change_24h=25.0, price_change_6h=12.0, price_change_1h=5.0)
    result = await pipeline_result(pair=hot_pair)
    events = make_rules().evaluate(result, operator_interest=False)
    momentum = next(e for e in events if e.alert_type == "momentum")
    assert momentum.priority is AlertPriority.MEDIUM  # unchanged


async def test_interest_gate_configurable_off():
    """Rule 17: the gate is a setting, not a hardcode — disabling it restores
    full-priority protective alerts on every token."""
    rules = AutomationRules(AlertThresholds(),
                            AlertEngineSettings(risk_alerts_require_interest=False))
    result = await pipeline_result(pair=make_pair(liquidity_usd=3_000.0))
    events = rules.evaluate(result, operator_interest=False)
    warnings = [e for e in events if e.alert_type == "risk_warning"]
    assert warnings and warnings[0].priority is AlertPriority.HIGH


def test_gate_events_by_interest_covers_security_changes_and_is_idempotent():
    from meme_intelligence.alerts.notification_engine import gate_events_by_interest

    change = AlertEvent(AlertPriority.CRITICAL, "security_change", TOKEN,
                        "became honeypot", ("honeypot: False -> True",))
    gated = gate_events_by_interest([change], operator_interest=False)
    assert gated[0].priority is AlertPriority.LOW
    # Idempotent: a second pass adds no duplicate note.
    again = gate_events_by_interest(gated, operator_interest=False)
    assert again[0].reasons == gated[0].reasons


class RecordingSink:
    def __init__(self):
        self.sent: list[AlertEvent] = []

    async def send(self, event):
        self.sent.append(event)


async def test_notification_cooldown_suppresses_repeats():
    clock = {"now": 0.0}
    sink = RecordingSink()
    engine = NotificationEngine([sink], AlertEngineSettings(cooldown_seconds=900),
                                time_func=lambda: clock["now"])
    event = AlertEvent(AlertPriority.HIGH, "test_alert", TOKEN, "title", ("reason",))

    delivered = await engine.dispatch([event])
    assert len(delivered) == 1
    delivered = await engine.dispatch([event])  # within cooldown
    assert delivered == []
    clock["now"] = 901.0
    delivered = await engine.dispatch([event])  # cooldown elapsed
    assert len(delivered) == 1
    assert len(sink.sent) == 2


async def test_different_alert_types_have_independent_cooldowns():
    sink = RecordingSink()
    engine = NotificationEngine([sink], AlertEngineSettings(), time_func=lambda: 0.0)
    a = AlertEvent(AlertPriority.HIGH, "type_a", TOKEN, "t", ())
    b = AlertEvent(AlertPriority.HIGH, "type_b", TOKEN, "t", ())
    delivered = await engine.dispatch([a, b])
    assert len(delivered) == 2


def test_engine_requires_a_sink():
    with pytest.raises(ValueError):
        NotificationEngine([], AlertEngineSettings())


def test_event_renders():
    event = AlertEvent(AlertPriority.CRITICAL, "emergency_review", TOKEN,
                       "Destructive risk", ("honeypot confirmed",),
                       scores={"security": 0.0, "community": None})
    text = event.render()
    assert "CRITICAL" in text and "honeypot confirmed" in text and "community=?" in text


# ---- Operator liquidity / market-cap floor for buy-side alerts (2026-07-11) ----

async def test_liquidity_floor_annotates_but_no_longer_suppresses():
    """Operator rule 2026-07-12: a thin pool no longer DROPS the buy-side alert.
    The alert still sends and the safety checklist carries a ⚠ liquidity line so
    the operator sees the shortfall and decides ("if just one thing misses the
    checklist, send it through and let me know")."""
    rules = AutomationRules(
        AlertThresholds(opportunity_min_liquidity_usd=200_000.0),  # fixture has 90k
        AlertEngineSettings())
    result = await pipeline_result()
    assert result.master.final_score >= 88.0   # a strong candidate
    events = rules.evaluate(result)
    buy_side = [e for e in events if e.alert_type in _BUY_SIDE_ALERT_TYPES]
    assert buy_side                                      # NOT suppressed anymore
    checklist = "\n".join(buy_side[0].checklist)
    assert "⚠️" in checklist and "comfort floor" in checklist  # the miss is shown
    assert "Liquidity $90,000" in checklist


async def test_market_cap_floor_annotates_but_no_longer_suppresses():
    rules = AutomationRules(
        AlertThresholds(opportunity_min_market_cap_usd=5_000_000.0),  # fixture has 400k
        AlertEngineSettings())
    result = await pipeline_result()
    buy_side = [e for e in rules.evaluate(result) if e.alert_type in _BUY_SIDE_ALERT_TYPES]
    assert buy_side
    checklist = "\n".join(buy_side[0].checklist)
    assert "Market cap" in checklist and "below your" in checklist


# ---- Operator liquidity / market-cap CEILING for buy-side alerts ----

async def test_liquidity_ceiling_suppresses_buy_side():
    """A coin whose pool has already grown past the ceiling is no longer an
    early opportunity — its buy-side alert is suppressed (fixture's $90k pool
    is above a $50k ceiling). This is the '$2.8M coin dressed as early
    opportunity' fix."""
    rules = AutomationRules(
        AlertThresholds(opportunity_max_liquidity_usd=50_000.0),  # fixture has 90k
        AlertEngineSettings())
    result = await pipeline_result()
    types = {e.alert_type for e in rules.evaluate(result)}
    assert not (types & _BUY_SIDE_ALERT_TYPES)


async def test_market_cap_ceiling_suppresses_buy_side():
    rules = AutomationRules(
        AlertThresholds(opportunity_max_market_cap_usd=100_000.0),  # fixture has 400k
        AlertEngineSettings())
    result = await pipeline_result()
    types = {e.alert_type for e in rules.evaluate(result)}
    assert not (types & _BUY_SIDE_ALERT_TYPES)


async def test_ceiling_off_by_default_lets_large_coin_through():
    """Rule 18: with the ceiling unset (0), a large coin still alerts. Turning
    the ceiling on for the same coin suppresses it — proving the ceiling, not
    the fixture, is what changed."""
    result = await pipeline_result()
    off = {e.alert_type for e in make_rules().evaluate(result)}  # both ceilings 0
    assert off & _BUY_SIDE_ALERT_TYPES                           # baseline: it alerts
    on = {e.alert_type for e in AutomationRules(
        AlertThresholds(opportunity_max_liquidity_usd=50_000.0),
        AlertEngineSettings()).evaluate(result)}
    assert not (on & _BUY_SIDE_ALERT_TYPES)


async def test_coin_under_ceiling_still_sends():
    """A coin below both ceilings is unaffected (no over-suppression)."""
    rules = AutomationRules(
        AlertThresholds(opportunity_max_liquidity_usd=500_000.0,
                        opportunity_max_market_cap_usd=1_000_000.0),
        AlertEngineSettings())
    result = await pipeline_result()   # fixture 90k liq / 400k mcap — under both
    types = {e.alert_type for e in rules.evaluate(result)}
    assert types & _BUY_SIDE_ALERT_TYPES


def test_negative_ceiling_rejected():
    with pytest.raises(ConfigurationError, match="opportunity_max_liquidity_usd"):
        AlertThresholds(opportunity_max_liquidity_usd=-1.0)


def test_ceiling_below_floor_rejected():
    with pytest.raises(ConfigurationError, match="must be >="):
        AlertThresholds(opportunity_min_liquidity_usd=200_000.0,
                        opportunity_max_liquidity_usd=100_000.0)


async def test_unknown_liquidity_is_blocked_as_untradeable():
    """Missing liquidity is now a HARD block, not an annotation — a coin you
    cannot even size is not a real opportunity (operator rule 2026-07-12,
    superseding the earlier annotate-thin-pools behavior for the 0/None case)."""
    rules = AutomationRules(
        AlertThresholds(opportunity_min_liquidity_usd=10_000.0),
        AlertEngineSettings())
    result = await pipeline_result(pair=make_pair(liquidity_usd=None))
    types = {e.alert_type for e in rules.evaluate(result)}
    assert not (types & _BUY_SIDE_ALERT_TYPES)


async def test_floor_no_longer_touches_protective_alerts():
    """A destructive coin still produces its protective warning and never a
    buy-side alert — that path is governed by the destructive/rug logic, not
    the (now advisory) liquidity floor."""
    rules = AutomationRules(
        AlertThresholds(opportunity_min_liquidity_usd=1_000_000.0),
        AlertEngineSettings())
    # honeypot at $800 liquidity: destructive -> protective emergency, no buy-side.
    result = await pipeline_result(honeypot=True, pair=make_pair(liquidity_usd=800.0))
    types = {e.alert_type for e in rules.evaluate(result)}
    assert types & _PROTECTIVE_ALERT_TYPES     # the warning still fires
    assert not (types & _BUY_SIDE_ALERT_TYPES)  # destructive -> never a buy pitch


async def test_floor_off_by_default_leaves_alerts_unchanged():
    """Default floors are 0.0 (off): a strong candidate still fires (Rule 18)."""
    result = await pipeline_result()
    types = {e.alert_type for e in make_rules().evaluate(result)}
    assert "strong_candidate" in types         # unchanged from pre-floor behavior


def test_opportunity_floor_validates_and_loads_from_env():
    with pytest.raises(ConfigurationError, match="opportunity_min_liquidity_usd"):
        AlertThresholds(opportunity_min_liquidity_usd=-1.0)
    with pytest.raises(ConfigurationError, match="opportunity_min_market_cap_usd"):
        AlertThresholds(opportunity_min_market_cap_usd=-5.0)
    s = Settings.from_env(env={
        "MEMEINTEL_ALERTS_OPPORTUNITY_MIN_LIQUIDITY_USD": "15000",
        "MEMEINTEL_ALERTS_OPPORTUNITY_MIN_MARKET_CAP_USD": "50000",
    })
    assert s.alerts.opportunity_min_liquidity_usd == 15000.0
    assert s.alerts.opportunity_min_market_cap_usd == 50000.0
    # default stays off
    assert Settings.from_env(env={}).alerts.opportunity_min_liquidity_usd == 0.0


# ---- Hard "must be a real, tradeable coin" floor (operator rule 2026-07-12) --

async def test_zero_liquidity_coin_is_never_sent():
    """A 0-liquidity coin is not an opportunity you could buy — it is suppressed
    outright, NOT sent with a checklist note (operator rule 2026-07-12)."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=0.0))
    types = {e.alert_type for e in make_rules().evaluate(result)}
    assert not (types & _BUY_SIDE_ALERT_TYPES)


async def test_zero_market_cap_coin_is_never_sent():
    result = await pipeline_result(pair=make_pair(market_cap=0.0))
    types = {e.alert_type for e in make_rules().evaluate(result)}
    assert not (types & _BUY_SIDE_ALERT_TYPES)


async def test_missing_liquidity_or_market_cap_is_never_sent():
    """Missing (None) liquidity or market cap counts as untradeable, never as a
    green light (Rule 8) — even with the comfort floors off."""
    no_liq = await pipeline_result(pair=make_pair(liquidity_usd=None))
    assert not ({e.alert_type for e in make_rules().evaluate(no_liq)} & _BUY_SIDE_ALERT_TYPES)
    no_mcap = await pipeline_result(pair=make_pair(market_cap=None))
    assert not ({e.alert_type for e in make_rules().evaluate(no_mcap)} & _BUY_SIDE_ALERT_TYPES)


async def test_thin_but_real_coin_still_sends_with_a_note():
    """The hard floor blocks only 0/missing — a small BUT real pool still sends
    (with the ⚠ comfort-floor note), preserving the operator's "send it and tell
    me" rule for thin-but-tradeable coins."""
    rules = AutomationRules(
        AlertThresholds(opportunity_min_liquidity_usd=10_000.0),  # comfort floor
        AlertEngineSettings(), now_func=lambda: NOW)
    result = await pipeline_result(pair=make_pair(liquidity_usd=4_000.0, market_cap=60_000.0))
    buy_side = [e for e in rules.evaluate(result) if e.alert_type in _BUY_SIDE_ALERT_TYPES]
    assert buy_side                                              # real pool -> still sent
    assert any("comfort floor" in line for line in buy_side[0].checklist)


# ---- Safety checklist rides on buy-side alerts (operator rule 2026-07-12) ----

async def test_clean_buy_side_alert_carries_a_passing_checklist():
    """A healthy fixture's buy-side alert carries a checklist with a "passed
    X/Y" header, all-clear lines, and the concentration note. The checklist is
    also visible in the rendered message the operator sees."""
    from meme_intelligence.alerts.sinks import format_alert
    result = await pipeline_result()
    buy_side = [e for e in make_rules().evaluate(result)
                if e.alert_type in _BUY_SIDE_ALERT_TYPES]
    assert buy_side
    event = buy_side[0]
    assert event.checklist                                   # attached
    assert event.checklist[0].startswith("Safety checklist — passed ")
    body = "\n".join(event.checklist)
    assert "✅ Sellable" in body
    assert "✅ Mint authority renounced" in body
    assert "✅ Freeze authority renounced" in body
    # visible to the operator in BOTH renderers
    assert "Safety checklist" in event.render()
    assert "Safety checklist" in format_alert(event)


async def test_checklist_flags_soft_risks_without_suppressing():
    """Mint/freeze authority still live, a high sell tax, and a deployer tied to
    past honeypots each produce a ⚠ line — but the alert is NOT suppressed
    (only a rug veto does that). Verified on the checklist directly."""
    dirty = make_profile(is_mintable=True, is_freezable=True,
                         sell_tax_percent=30.0, honeypot_same_creator_count=2)
    result = await pipeline_result(profile=dirty)
    checks = make_rules()._safety_checklist(result)
    by_status = {c.detail: c.status for c in checks}
    warns = "\n".join(c.detail for c in checks if c.status == "warn")
    assert "Mint authority still active" in warns
    assert "Freeze authority still active" in warns
    assert "Sell tax 30%" in warns
    assert "past honeypot" in warns
    # every one of those is a soft ⚠ (annotate), none is a hard stop
    assert all(s in ("pass", "warn", "note", "unknown") for s in by_status.values())


async def test_checklist_concentration_note_is_never_a_fail():
    """A brand-new coin with 85% in the top wallet: shown as an informational
    note framed 'normal for a new launch', NEVER a warn — a fresh coin must not
    look like a scam purely for being young (the operator's exact concern)."""
    young = make_pair(pair_created_at=NOW - timedelta(minutes=3))
    concentrated = make_profile(top_holder_percent=85.0)
    result = await pipeline_result(pair=young, profile=concentrated)
    checks = make_rules(now=lambda: NOW)._safety_checklist(result)
    conc = [c for c in checks if "top wallet" in c.detail]
    assert conc and conc[0].status == "note"                 # note, not warn
    assert "normal for a new launch" in conc[0].detail


async def test_checklist_surfaces_unknown_data_not_assumed_safe():
    """Missing contract facts are shown as ❔ (Rule 8), never silently passed."""
    blank = make_profile(is_mintable=None, is_freezable=None,
                         sell_tax_percent=None, honeypot_same_creator_count=None)
    result = await pipeline_result(profile=blank)
    statuses = {c.detail: c.status for c in make_rules()._safety_checklist(result)}
    assert statuses["Mint authority: not verified"] == "unknown"
    assert statuses["Freeze authority: not verified"] == "unknown"
    assert statuses["Sell tax: not verified"] == "unknown"


def test_render_checklist_counts_only_scored_lines():
    """The "passed X/Y" header counts pass/warn only — notes and unknowns are
    shown but not scored."""
    from meme_intelligence.alerts.notification_engine import (
        _SafetyCheck,
        _render_checklist,
    )
    lines = _render_checklist([
        _SafetyCheck("pass", "A"), _SafetyCheck("pass", "B"),
        _SafetyCheck("warn", "C"), _SafetyCheck("note", "D"),
        _SafetyCheck("unknown", "E"),
    ])
    assert lines[0] == "Safety checklist — passed 2/3"       # 2 pass of 3 scored
    assert lines[1] == "✅ A" and "⚠️ C" in lines and "ℹ️ D" in lines and "❔ E" in lines


def test_checklist_config_validates_and_loads_from_env():
    with pytest.raises(ConfigurationError, match="checklist_sell_tax_max_percent"):
        AlertThresholds(checklist_sell_tax_max_percent=150.0)
    with pytest.raises(ConfigurationError, match="checklist_new_launch_minutes"):
        AlertThresholds(checklist_new_launch_minutes=-1.0)
    s = Settings.from_env(env={
        "MEMEINTEL_ALERTS_CHECKLIST_SELL_TAX_MAX_PERCENT": "10",
        "MEMEINTEL_ALERTS_CHECKLIST_NEW_LAUNCH_MINUTES": "30",
    })
    assert s.alerts.checklist_sell_tax_max_percent == 10.0
    assert s.alerts.checklist_new_launch_minutes == 30.0


async def test_deterministic_veto_keeps_protective_alerts():
    """The rug/risk veto suppresses buy-side signals but never a protective
    warning — a holder on a flagged coin still gets the alert."""
    # Honeypot at healthy liquidity -> a protective emergency_review on the
    # normal path; passing a deterministic veto must not silence it.
    result = await pipeline_result(honeypot=True, pair=make_pair(liquidity_usd=90_000.0))
    events = make_rules().evaluate(
        result, deterministic_risk_veto="rug engine score 30 (deployer_blacklisted)")
    types = {e.alert_type for e in events}
    assert types & _PROTECTIVE_ALERT_TYPES       # the warning survives the veto
    assert not (types & _BUY_SIDE_ALERT_TYPES)   # no buy-side leaks through
