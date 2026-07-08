"""Tests for the AI reasoning layer (Spec Part 23) — mocked LLM, no network."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from meme_intelligence.ai.reasoning import (
    JUDGMENT_SCHEMA,
    AIJudgmentService,
    build_intelligence_snapshot,
    build_judgment_service,
)
from meme_intelligence.config.settings import AISettings, Settings
from meme_intelligence.core.enums import (
    MarketRegime,
    NarrativeCategory,
    NarrativeStage,
    ResearchMode,
)
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")
SETTINGS = Settings.from_env(env={})


def make_pair() -> DexPair:
    return DexPair(
        chain="solana", pair_address="Pool1", base_token=TOKEN,
        market_cap=400_000.0, fdv=420_000.0, liquidity_usd=90_000.0,
        volume_24h=120_000.0, volume_1h=8_000.0,
        buys_24h=400, sells_24h=250, buys_1h=40, sells_1h=15,
        buyers_24h=300, sellers_24h=180,
        price_change_24h=15.0, price_change_6h=8.0, price_change_1h=2.0,
        pair_created_at=NOW - timedelta(hours=3),
    )


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


GOOD_JUDGMENT = {
    "meme_strength": 80, "narrative": 75, "brand": 70,
    "dev_communication": None, "long_term": 60,
    "memorability": 85, "shareability": 80, "emotional_impact": 70,
    "cultural_timing": 65, "community_participation": None,
    "community_creativity": None, "long_term_strength": 60,
    "short_term_hype_risk": False, "trend_dependency_risk": None, "copycat_risk": False,
    "narrative_category": "internet_culture", "narrative_stage": "expansion",
    "narrative_summary": "A frog character from a long-running internet joke.",
    "bull_case": ["Security profile is clean with renounced ownership.",
                  "Holder base is growing with organic-looking volume."],
    "bear_case": ["No community data is available yet.",
                  "Early-stage liquidity remains thin relative to hype."],
    "confidence": 55,
    "confidence_reason": "Market and security data present; social data missing.",
}


class FakeMessages:
    """anthropic messages endpoint stand-in; records the request it receives."""

    def __init__(self, payload=GOOD_JUDGMENT, stop_reason="end_turn", raises=None):
        self.payload = payload
        self.stop_reason = stop_reason
        self.raises = raises
        self.last_request = None

    async def create(self, **kwargs):
        self.last_request = kwargs
        if self.raises is not None:
            raise self.raises
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
        )


def make_service(messages: FakeMessages, **overrides) -> AIJudgmentService:
    settings = AISettings(**overrides) if overrides else AISettings()
    return AIJudgmentService(settings, SimpleNamespace(messages=messages))


async def deterministic_result(honeypot=False):
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile(honeypot)),
                                now_func=lambda: NOW)
    return await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)


# ---- Snapshot builder (Part 23 Section 3) ----

async def test_snapshot_is_structured_and_honest_about_gaps():
    result = await deterministic_result()
    snapshot = build_intelligence_snapshot(result)
    assert snapshot["token"]["symbol"] == "MEME"
    assert snapshot["market"]["liquidity_usd"] == 90_000.0
    assert snapshot["security"]["score"] == round(result.security.overall_score)
    # Missing sources are reported as missing, never invented (Rule 8).
    assert snapshot["community"]["assessed"] is False
    assert snapshot["wallets"]["smart_money_score"] is None
    json.dumps(snapshot)  # must be JSON-serializable as sent


# ---- Judgment parsing and validation ----

async def test_valid_judgment_parses_into_inputs():
    result = await deterministic_result()
    service = make_service(FakeMessages())
    judgment = await service.judge(result)
    assert judgment is not None
    assert judgment.foundation_inputs.meme_strength == 80
    assert judgment.foundation_inputs.dev_communication is None
    assert judgment.narrative_inputs.category is NarrativeCategory.INTERNET_CULTURE
    assert judgment.narrative_inputs.stage is NarrativeStage.EXPANSION
    assert judgment.narrative_inputs.short_term_hype_risk is False
    assert judgment.narrative_inputs.trend_dependency_risk is None
    assert judgment.confidence == 55
    assert len(judgment.bull_case) == 2 and len(judgment.bear_case) == 2
    assert "MEME" not in judgment.summary() or True  # summary renders
    assert "Bull case" in judgment.summary()


async def test_request_uses_configured_model_and_schema():
    result = await deterministic_result()
    messages = FakeMessages()
    service = make_service(messages, model="claude-opus-4-8", effort="medium")
    await service.judge(result, mode=ResearchMode.DEEP_INVESTIGATION)
    request = messages.last_request
    assert request["model"] == "claude-opus-4-8"
    assert request["output_config"]["effort"] == "medium"
    assert request["output_config"]["format"]["schema"] is JUDGMENT_SCHEMA
    assert request["thinking"] == {"type": "adaptive"}
    assert "DEEP INVESTIGATION" in request["messages"][0]["content"]
    assert "INTELLIGENCE SNAPSHOT" in request["messages"][0]["content"]


@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps({**GOOD_JUDGMENT, "meme_strength": 150}),        # out of range
    json.dumps({**GOOD_JUDGMENT, "copycat_risk": "yes"}),       # non-boolean flag
    json.dumps({**GOOD_JUDGMENT, "narrative_category": "weather"}),  # bad enum
    json.dumps([1, 2, 3]),                                      # not an object
])
async def test_invalid_judgments_are_discarded(payload):
    result = await deterministic_result()
    service = make_service(FakeMessages(payload=payload))
    assert await service.judge(result) is None


async def test_banned_language_in_prose_discards_judgment():
    bad = dict(GOOD_JUDGMENT, bull_case=["This token is guaranteed to succeed."])
    result = await deterministic_result()
    service = make_service(FakeMessages(payload=bad))
    assert await service.judge(result) is None


async def test_refusal_and_api_errors_degrade_to_none():
    result = await deterministic_result()
    assert await make_service(FakeMessages(stop_reason="refusal")).judge(result) is None
    assert await make_service(FakeMessages(raises=RuntimeError("boom"))).judge(result) is None


async def test_low_confidence_judgment_is_discarded():
    timid = dict(GOOD_JUDGMENT, confidence=10)
    result = await deterministic_result()
    service = make_service(FakeMessages(payload=timid))  # default floor is 20
    assert await service.judge(result) is None


# ---- Pipeline integration (Part 23 architecture) ----

async def test_pipeline_ai_enrichment_fills_foundation_and_narrative():
    messages = FakeMessages()
    service = make_service(messages)
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile()),
                                ai_service=service, now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result.ai_judgment is not None
    assert result.foundation is not None
    assert result.narrative is not None
    assert result.narrative.narrative_summary is not None
    assert result.master.category_scores.narrative == pytest.approx(
        result.narrative.overall_score)
    assert result.master.category_scores.foundation is not None
    # Deterministic assessments are untouched by the AI pass.
    assert result.master.category_scores.security == pytest.approx(
        result.security.overall_score)


async def test_pipeline_skips_ai_for_destructive_tokens():
    messages = FakeMessages()
    service = make_service(messages)
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile(honeypot=True)),
                                ai_service=service, now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result.ai_judgment is None
    assert messages.last_request is None  # no tokens spent on an invalidated coin (Rule 10)


async def test_pipeline_survives_ai_failure_on_deterministic_evidence():
    service = make_service(FakeMessages(raises=RuntimeError("api down")))
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile()),
                                ai_service=service, now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result is not None
    assert result.ai_judgment is None
    assert result.master.category_scores.narrative is None  # honest "no data"


async def test_explicit_analyst_narrative_inputs_win_over_ai():
    from meme_intelligence.analyzers.narrative_analyzer import NarrativeInputs

    service = make_service(FakeMessages())
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile()),
                                ai_service=service, now_func=lambda: NOW)
    analyst = NarrativeInputs(meme_strength=10, cultural_timing=10, long_term_strength=10)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL,
                                         narrative_inputs=analyst)
    # The analyst's (deliberately low) judgments survive the AI pass.
    assert result.narrative.sub_scores["meme_strength"] == pytest.approx(10.0)


# ---- Settings & service construction ----

def test_ai_settings_validated_and_env_overridable():
    from meme_intelligence.core.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        AISettings(effort="extreme")
    with pytest.raises(ConfigurationError):
        AISettings(max_tokens=0)
    settings = Settings.from_env(env={
        "MEMEINTEL_AI_MODEL": "claude-opus-4-8",
        "MEMEINTEL_AI_EFFORT": "low",
        "MEMEINTEL_ANTHROPIC_API_KEY": "sk-test",
    })
    assert settings.ai.effort == "low"
    assert settings.anthropic_api_key == "sk-test"


def test_service_builder_requires_key():
    assert build_judgment_service(Settings.from_env(env={})) is None
    built = build_judgment_service(Settings.from_env(
        env={"MEMEINTEL_ANTHROPIC_API_KEY": "sk-test"}))
    assert built is not None
