"""Tests for automation rules and alert dispatch (Spec Part 13)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.alerts.notification_engine import (
    AlertEvent,
    AutomationRules,
    ConsoleSink,
    NotificationEngine,
)
from meme_intelligence.config.settings import AlertEngineSettings, AlertThresholds, Settings
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


def make_profile(honeypot=False) -> SecurityProfile:
    return SecurityProfile(
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


class OneShotGoPlus:
    def __init__(self, profile):
        self.profile = profile

    async def get_token_security(self, chain, address):
        return self.profile


async def pipeline_result(honeypot=False, pair=None):
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile(honeypot)),
                                now_func=lambda: NOW)
    return await pipeline.analyze_pair(pair or make_pair(), regime=MarketRegime.NEUTRAL)


def make_rules() -> AutomationRules:
    return AutomationRules(AlertThresholds(), AlertEngineSettings())


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
    decision-relevant deterioration — the HIGH warning stays."""
    result = await pipeline_result(pair=make_pair(liquidity_usd=3_000.0))
    events = make_rules().evaluate(result)
    warnings = [e for e in events if e.alert_type == "risk_warning"]
    assert warnings and warnings[0].priority is AlertPriority.HIGH
    assert not any(e.alert_type == "token_death" for e in events)


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
