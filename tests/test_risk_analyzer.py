"""Tests for the risk management engine (Spec Part 9)."""


from meme_intelligence.analyzers.risk_analyzer import (
    PortfolioRiskManager,
    Position,
    RiskAnalyzer,
    emergency_flags,
)
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.config.settings import (
    RiskSettings,
    RiskSubWeights,
    SecuritySubWeights,
    SecurityThresholds,
)
from meme_intelligence.core.enums import MarketRegime, RiskCategory, RiskPosture
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME")


def make_security(destructive=False, sparse=False):
    if sparse:
        profile = SecurityProfile(token=TOKEN, source="goplus", is_mintable=False)
    else:
        profile = SecurityProfile(
            token=TOKEN, source="goplus",
            is_honeypot=destructive, cannot_buy=False, cannot_sell_all=False,
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
    pair = DexPair(chain="solana", pair_address="P", base_token=TOKEN, liquidity_usd=80000.0)
    return SecurityAnalyzer(SecurityThresholds(), SecuritySubWeights()).assess(
        profile, None if sparse else pair
    )


def make_pair(**overrides) -> DexPair:
    defaults = dict(chain="solana", pair_address="P", base_token=TOKEN,
                    liquidity_usd=80000.0, price_change_24h=8.0)
    defaults.update(overrides)
    return DexPair(**defaults)


def make_analyzer() -> RiskAnalyzer:
    return RiskAnalyzer(RiskSubWeights())


def test_clean_token_is_low_or_moderate_risk():
    assessment = make_analyzer().assess(make_security(), pair=make_pair())
    assert assessment.category in (RiskCategory.LOW_RELATIVE, RiskCategory.MODERATE)
    assert assessment.components["security"] < 25


def test_destructive_security_pins_component_at_max():
    assessment = make_analyzer().assess(make_security(destructive=True), pair=make_pair())
    assert assessment.components["security"] == 100.0
    assert assessment.category in (RiskCategory.HIGH, RiskCategory.EXTREME)


def test_wild_volatility_and_thin_liquidity_raise_market_risk():
    calm = make_analyzer().assess(make_security(), pair=make_pair())
    wild = make_analyzer().assess(
        make_security(), pair=make_pair(price_change_24h=150.0, liquidity_usd=6000.0)
    )
    assert wild.components["market"] > calm.components["market"]


def test_bear_regime_adds_market_risk():
    neutral = make_analyzer().assess(make_security(), pair=make_pair(), regime=MarketRegime.NEUTRAL)
    bear = make_analyzer().assess(make_security(), pair=make_pair(), regime=MarketRegime.BEAR)
    assert bear.components["market"] > neutral.components["market"]


def test_unverifiable_profile_cannot_be_low_risk():
    """Unknown is not safe: sparse data floors the category at HIGH."""
    assessment = make_analyzer().assess(make_security(sparse=True))  # no pair/community/token
    assert assessment.coverage < 0.5
    assert assessment.category in (RiskCategory.HIGH, RiskCategory.EXTREME)
    assert any("unverifiable" in r for r in assessment.main_risks)


def test_emergency_flags_from_destructive_findings():
    critical, _high = emergency_flags(make_security(destructive=True))
    assert critical and "CRITICAL" in critical[0]
    critical_none, _ = emergency_flags(make_security())
    assert not critical_none


def test_summary_renders():
    text = make_analyzer().assess(make_security(), pair=make_pair()).summary()
    assert "Risk score" in text and "higher = riskier" in text


# ---- Portfolio-level checks (Part 9, Sections 2/5/8-9) ----

def manager() -> PortfolioRiskManager:
    return PortfolioRiskManager(RiskSettings())


def test_healthy_portfolio_has_no_warnings():
    positions = [
        Position("A", "solana", 5.0, narrative="dog"),
        Position("B", "ethereum", 4.0, narrative="cat"),
    ]
    assert manager().exposure_warnings(positions) == []


def test_single_position_limit():
    warnings = manager().exposure_warnings([Position("BIG", "solana", 15.0)])
    assert any("15.0% of portfolio" in w for w in warnings)


def test_chain_and_narrative_concentration():
    positions = [Position(f"T{i}", "solana", 9.0, narrative="dog") for i in range(7)]
    warnings = manager().exposure_warnings(positions)
    assert any("chain 'solana'" in w for w in warnings)
    assert any("narrative 'dog'" in w for w in warnings)


def test_total_exposure_and_position_count():
    positions = [Position(f"T{i}", f"chain{i}", 8.0) for i in range(11)]
    warnings = manager().exposure_warnings(positions)
    assert any("position limit" in w or "positions exceeds" in w for w in warnings)
    assert any("total exposure" in w for w in warnings)


def test_drawdown_posture_thresholds():
    m = manager()
    assert m.drawdown_posture(1.0, 2.0)[0] is RiskPosture.NORMAL
    assert m.drawdown_posture(6.0, 0.0)[0] is RiskPosture.REDUCED
    assert m.drawdown_posture(0.0, 12.0)[0] is RiskPosture.REDUCED
    assert m.drawdown_posture(11.0, 0.0)[0] is RiskPosture.DEFENSIVE
    assert m.drawdown_posture(0.0, 25.0)[0] is RiskPosture.DEFENSIVE
    _, guidance = m.drawdown_posture(11.0, 0.0)
    assert "revenge" in guidance
