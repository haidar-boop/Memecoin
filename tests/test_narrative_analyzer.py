"""Tests for the narrative intelligence & viral potential engine (Spec Part 19)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.ai.prompts import check_language
from meme_intelligence.analyzers.community_analyzer import CommunityAssessment
from meme_intelligence.analyzers.narrative_analyzer import (
    NarrativeAnalyzer,
    NarrativeInputs,
    ViralCatalyst,
)
from meme_intelligence.config.settings import (
    NarrativeSubWeights,
    NarrativeThresholds,
    Settings,
    ViralSubWeights,
)
from meme_intelligence.core.enums import (
    CatalystLevel,
    CommunityRating,
    ConfidenceLevel,
    MarketRegime,
    NarrativeCategory,
    NarrativeRating,
    NarrativeRisk,
    NarrativeStage,
    RiskTier,
    SentimentLabel,
)
from meme_intelligence.core.errors import ConfigurationError, InsufficientDataError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")
SETTINGS = Settings.from_env(env={})


def make_analyzer() -> NarrativeAnalyzer:
    return NarrativeAnalyzer(NarrativeThresholds(), NarrativeSubWeights(), ViralSubWeights())


def make_community(creativity=80.0, overall=75.0, artificial=False) -> CommunityAssessment:
    rating = CommunityRating.ARTIFICIAL if artificial else CommunityRating.STRONG
    return CommunityAssessment(
        token=TOKEN, source="test",
        sub_scores={"engagement": 70.0, "growth": 60.0, "loyalty": None,
                    "creativity": creativity, "dev_relationship": None},
        overall_score=0.0 if artificial else overall,
        rating=rating, confidence=ConfidenceLevel.MEDIUM,
        findings=(), unknown_fields=(), coverage=0.6,
    )


FULL_INPUTS = NarrativeInputs(
    memorability=80, shareability=90, emotional_impact=70, cultural_timing=60,
    community_participation=50, meme_strength=85, community_creativity=75,
    long_term_strength=65,
    category=NarrativeCategory.ANIMAL, stage=NarrativeStage.EXPANSION,
    short_term_hype_risk=False, trend_dependency_risk=False, copycat_risk=False,
)


# ---- Input validation ----

def test_out_of_range_inputs_rejected():
    with pytest.raises(ValueError):
        NarrativeInputs(memorability=150)
    with pytest.raises(ValueError):
        NarrativeInputs(long_term_strength=-1)
    with pytest.raises(ValueError):
        make_analyzer().assess(TOKEN, NarrativeInputs(meme_strength=50),
                               positive_sentiment_percent=101)


def test_catalyst_requires_description():
    with pytest.raises(ValueError):
        ViralCatalyst("   ", CatalystLevel.HIGH, CatalystLevel.LOW)


# ---- Scoring per the Part 19 rubrics ----

def test_full_inputs_score_per_spec():
    assessment = make_analyzer().assess(TOKEN, FULL_INPUTS)
    # Cultural timing blends the judgment (60) with the expansion-stage signal (90).
    assert assessment.sub_scores["cultural_timing"] == pytest.approx(75.0)
    # Viral score (Section 3): five components at 20% each.
    assert assessment.viral_score == pytest.approx((80 + 90 + 70 + 75 + 50) / 5)
    # Narrative intelligence score (Section 11): viral score feeds viral_potential.
    assert assessment.sub_scores["viral_potential"] == pytest.approx(assessment.viral_score)
    expected = (85 + 75 + 73 + 75 + 65) / 5
    assert assessment.overall_score == pytest.approx(expected)
    assert assessment.coverage == pytest.approx(1.0)
    assert assessment.rating is NarrativeRating.STRONG
    assert assessment.narrative_risk is NarrativeRisk.LOW
    assert assessment.category is NarrativeCategory.ANIMAL
    assert assessment.stage is NarrativeStage.EXPANSION


def test_partial_inputs_renormalize_and_report_missing():
    assessment = make_analyzer().assess(TOKEN, NarrativeInputs(meme_strength=80))
    assert assessment.overall_score == pytest.approx(80.0)
    assert assessment.coverage == pytest.approx(0.20)
    assert assessment.viral_score is None
    assert "memorability" in assessment.unknown_fields
    assert "category" in assessment.unknown_fields
    assert assessment.confidence is ConfidenceLevel.LOW


def test_no_inputs_raises():
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(TOKEN, NarrativeInputs())


def test_viral_only_inputs_still_score_the_narrative():
    inputs = NarrativeInputs(memorability=80, shareability=80, emotional_impact=80,
                             cultural_timing=80, community_participation=80)
    assessment = make_analyzer().assess(TOKEN, inputs)
    assert assessment.viral_score == pytest.approx(80.0)
    # NIS has cultural_timing + viral_potential -> renormalized to 80.
    assert assessment.overall_score == pytest.approx(80.0)
    assert assessment.coverage == pytest.approx(0.40)


# ---- Community evidence reuse (Sections 3, 5, 11) ----

def test_community_creativity_fills_participation_and_creativity():
    assessment = make_analyzer().assess(TOKEN, NarrativeInputs(),
                                        community=make_community(creativity=80.0))
    assert assessment.viral_sub_scores["community_participation"] == pytest.approx(80.0)
    assert assessment.sub_scores["community_creativity"] == pytest.approx(80.0)
    assert assessment.viral_score == pytest.approx(80.0)
    assert any("organic" in s for s in assessment.strengths)


def test_explicit_judgment_beats_community_fallback():
    inputs = NarrativeInputs(community_participation=40, community_creativity=30)
    assessment = make_analyzer().assess(TOKEN, inputs,
                                        community=make_community(creativity=90.0))
    assert assessment.viral_sub_scores["community_participation"] == pytest.approx(40.0)
    assert assessment.sub_scores["community_creativity"] == pytest.approx(30.0)


def test_artificial_community_zeroes_participation_with_finding():
    assessment = make_analyzer().assess(TOKEN, NarrativeInputs(),
                                        community=make_community(artificial=True))
    assert assessment.viral_sub_scores["community_participation"] == pytest.approx(0.0)
    # The fake community's creativity numbers must not leak in as evidence.
    assert assessment.sub_scores["community_creativity"] is None
    assert any(f.severity is RiskTier.SERIOUS_WARNING and "artificial" in f.message
               for f in assessment.findings)
    assert any("artificial" in w for w in assessment.weaknesses)


# ---- Sentiment (Section 6) ----

def test_sentiment_classification_bands():
    analyzer = make_analyzer()
    inputs = NarrativeInputs(meme_strength=50)
    assert analyzer.assess(TOKEN, inputs, positive_sentiment_percent=75).sentiment \
        is SentimentLabel.POSITIVE
    assert analyzer.assess(TOKEN, inputs, positive_sentiment_percent=50).sentiment \
        is SentimentLabel.NEUTRAL
    negative = analyzer.assess(TOKEN, inputs, positive_sentiment_percent=20)
    assert negative.sentiment is SentimentLabel.NEGATIVE
    assert any("sentiment" in f.category for f in negative.findings)
    assert analyzer.assess(TOKEN, inputs).sentiment is SentimentLabel.UNKNOWN


# ---- Life-cycle stage (Section 7) ----

def test_saturation_stage_drags_timing_and_flags_distribution_risk():
    inputs = NarrativeInputs(cultural_timing=60, stage=NarrativeStage.SATURATION)
    assessment = make_analyzer().assess(TOKEN, inputs)
    assert assessment.sub_scores["cultural_timing"] == pytest.approx(45.0)
    assert any(f.severity is RiskTier.SERIOUS_WARNING and "distribution" in f.message
               for f in assessment.findings)
    # A late stage alone is evidence of narrative risk even with no flags assessed.
    assert assessment.narrative_risk is NarrativeRisk.MEDIUM


# ---- Section 10 risk factors ----

def test_risk_flags_deduct_from_long_term_strength():
    inputs = NarrativeInputs(long_term_strength=80, short_term_hype_risk=True)
    assessment = make_analyzer().assess(TOKEN, inputs)
    assert assessment.sub_scores["long_term_strength"] == pytest.approx(50.0)
    assert any("lasting story" in f.message for f in assessment.findings)


@pytest.mark.parametrize("flags, stage, expected", [
    ((None, None, None), NarrativeStage.UNKNOWN, NarrativeRisk.UNKNOWN),
    ((None, None, None), NarrativeStage.SATURATION, NarrativeRisk.MEDIUM),
    ((False, False, False), NarrativeStage.UNKNOWN, NarrativeRisk.LOW),
    ((True, False, False), NarrativeStage.UNKNOWN, NarrativeRisk.MEDIUM),
    ((True, True, False), NarrativeStage.UNKNOWN, NarrativeRisk.HIGH),
    ((False, False, False), NarrativeStage.DECLINE, NarrativeRisk.MEDIUM),
    ((True, False, False), NarrativeStage.SATURATION, NarrativeRisk.HIGH),
])
def test_narrative_risk_ladder(flags, stage, expected):
    hype, trend, copycat = flags
    inputs = NarrativeInputs(meme_strength=50, stage=stage,
                             short_term_hype_risk=hype,
                             trend_dependency_risk=trend, copycat_risk=copycat)
    assert make_analyzer().assess(TOKEN, inputs).narrative_risk is expected


# ---- Rating bands (Section 12) ----

@pytest.mark.parametrize("score, expected", [
    (90, NarrativeRating.EXCELLENT),
    (75, NarrativeRating.STRONG),
    (55, NarrativeRating.AVERAGE),
    (30, NarrativeRating.WEAK),
])
def test_rating_bands(score, expected):
    assessment = make_analyzer().assess(TOKEN, NarrativeInputs(meme_strength=score))
    assert assessment.rating is expected


# ---- Report format (Section 12) ----

def test_summary_renders_report_format_and_language_guard():
    inputs = NarrativeInputs(
        narrative_summary="A frog character from a long-running internet joke.",
        memorability=80, shareability=90, emotional_impact=70, cultural_timing=60,
        meme_strength=85, long_term_strength=30,
        category=NarrativeCategory.INTERNET_CULTURE, stage=NarrativeStage.CREATION,
        catalysts=(ViralCatalyst("community meme campaign",
                                 CatalystLevel.HIGH, CatalystLevel.MEDIUM),),
    )
    text = make_analyzer().assess(TOKEN, inputs,
                                  community=make_community()).summary()
    for expected in (
        "Narrative assessment: MEME",
        "Summary: A frog character",
        "category=internet_culture",
        "stage=creation",
        "Viral score:",
        "Strengths:",
        "Weaknesses:",
        "Viral catalysts:",
        "community meme campaign (probability=high, impact=medium)",
    ):
        assert expected in text, f"missing from summary: {expected}"
    assert check_language(text) == []


def test_summary_reports_partial_coverage():
    text = make_analyzer().assess(TOKEN, NarrativeInputs(meme_strength=75)).summary()
    assert "incomplete picture" in text
    assert "no data" in text


# ---- Settings (Rule 17) ----

def test_narrative_settings_env_overrides():
    env = {
        "MEMEINTEL_NARRATIVE_POSITIVE_SENTIMENT_PERCENT": "70",
        "MEMEINTEL_VIRAL_WEIGHTS_MEMORABILITY": "0.40",
        "MEMEINTEL_VIRAL_WEIGHTS_SHAREABILITY": "0.15",
        "MEMEINTEL_VIRAL_WEIGHTS_EMOTIONAL_IMPACT": "0.15",
        "MEMEINTEL_VIRAL_WEIGHTS_CULTURAL_TIMING": "0.15",
        "MEMEINTEL_VIRAL_WEIGHTS_COMMUNITY_PARTICIPATION": "0.15",
    }
    settings = Settings.from_env(env=env)
    assert settings.narrative.positive_sentiment_percent == 70.0
    assert settings.viral_weights.memorability == 0.40


def test_narrative_settings_validated():
    with pytest.raises(ConfigurationError):
        NarrativeThresholds(positive_sentiment_percent=40, negative_sentiment_percent=60)
    with pytest.raises(ConfigurationError):
        NarrativeSubWeights(meme_strength=0.50)
    with pytest.raises(ConfigurationError):
        ViralSubWeights(memorability=0.50)


# ---- Pipeline integration (the master score's narrative slot) ----

class OneShotGoPlus:
    def __init__(self, profile):
        self.profile = profile

    async def get_token_security(self, chain, address):
        return self.profile


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


def make_profile() -> SecurityProfile:
    return SecurityProfile(
        token=TOKEN, source="goplus",
        is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
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


async def test_pipeline_fills_master_narrative_slot():
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile()),
                                now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL,
                                         narrative_inputs=FULL_INPUTS)
    assert result.narrative is not None
    assert result.master.category_scores.narrative == pytest.approx(
        result.narrative.overall_score)
    # Q5 of the decision tree now sees a real answer instead of "unknown".
    q5 = [s for s in result.master.decision_trace if "narrative" in s.question]
    assert q5 and q5[0].answer == "yes"


async def test_pipeline_without_inputs_keeps_narrative_unknown():
    pipeline = ResearchPipeline(SETTINGS, OneShotGoPlus(make_profile()),
                                now_func=lambda: NOW)
    result = await pipeline.analyze_pair(make_pair(), regime=MarketRegime.NEUTRAL)
    assert result.narrative is None
    assert result.master.category_scores.narrative is None
