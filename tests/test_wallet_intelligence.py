"""Tests for the smart money & whale intelligence engine (Spec Part 17)."""

from datetime import datetime, timezone

import pytest

from meme_intelligence.analyzers.wallet_intelligence import (
    WalletIntelligenceAnalyzer,
    WalletTrackRecord,
    enrich_onchain_profile,
    sightings_from_assessment,
    wallet_reputation,
)
from meme_intelligence.config.settings import SmartMoneySubWeights, WalletIntelSettings
from meme_intelligence.core.enums import AccumulationVerdict, WhaleType
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.models import (
    DexPair,
    OnChainProfile,
    TokenIdentity,
    TokenTrade,
    TokenTransfer,
    WalletHolding,
    WalletIntelData,
)

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="MintAddr1", symbol="MEME")


def make_analyzer() -> WalletIntelligenceAnalyzer:
    return WalletIntelligenceAnalyzer(WalletIntelSettings(), SmartMoneySubWeights())


def make_pair(price=0.16) -> DexPair:
    return DexPair(chain="solana", pair_address="PoolAddr1", base_token=TOKEN,
                   price_usd=price, liquidity_usd=90_000.0)


def trade(owner, side, usd, price=0.16) -> TokenTrade:
    return TokenTrade(owner=owner, side=side, volume_usd=usd, timestamp=NOW, price_usd=price)


def healthy_data(**overrides) -> WalletIntelData:
    # Ten independent buyers of varied sizes, a couple of sellers, sane whales.
    trades = tuple(trade(f"Buyer{i}", "buy", 100.0 + 17 * i) for i in range(10))
    trades += (trade("SellerA", "sell", 150.0), trade("SellerB", "sell", 90.0))
    defaults = dict(
        token=TOKEN,
        sources=("helius", "birdeye"),
        top_holders=(
            WalletHolding("PoolAddr1", 12.0),      # the pool itself -> custodial
            WalletHolding("WhaleHold1", 3.0),
            WalletHolding("WhaleHold2", 2.0),
        ),
        recent_trades=trades,
        recent_transfers=(),
        holder_count=5000,
        unique_wallets_24h=400,
    )
    defaults.update(overrides)
    return WalletIntelData(**defaults)


def test_healthy_accumulation_scores_well():
    assessment = make_analyzer().assess(healthy_data(), make_pair())
    assert assessment.accumulation is AccumulationVerdict.HEALTHY
    assert assessment.accumulating_wallets == 10
    assert assessment.overall_score >= 60
    assert assessment.sub_scores["historical_success"] is None  # no track records yet
    assert assessment.coverage < 1.0


def test_pool_and_exchange_classified_custodial():
    data = healthy_data(top_holders=(
        WalletHolding("PoolAddr1", 12.0),
        WalletHolding("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", 4.0),  # binance
        WalletHolding("RealWhale", 2.0),
    ))
    assessment = make_analyzer().assess(data, make_pair())
    kinds = {w.owner: w.classification for w in assessment.whales}
    assert kinds["PoolAddr1"] is WhaleType.CUSTODIAL
    assert kinds["5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"] is WhaleType.CUSTODIAL
    assert kinds["RealWhale"] is WhaleType.LONG_TERM
    assert assessment.whales_selling == 0


def test_risk_whale_flagged():
    data = healthy_data(top_holders=(WalletHolding("MegaWhale", 8.0),))
    assessment = make_analyzer().assess(data, make_pair())
    whale = next(w for w in assessment.whales if w.owner == "MegaWhale")
    assert whale.classification is WhaleType.RISK
    assert any("move the market alone" in f.message for f in assessment.findings)


def test_selling_whale_counted():
    data = healthy_data(
        top_holders=(WalletHolding("WhaleSeller", 3.0),),
        recent_trades=healthy_data().recent_trades + (trade("WhaleSeller", "sell", 5000.0),),
    )
    assessment = make_analyzer().assess(data, make_pair())
    assert assessment.whales_selling == 1
    assert assessment.whale_net_flow_usd == pytest.approx(-5000.0)


def test_scripted_same_size_trades_are_artificial():
    trades = tuple(trade(f"Bot{i}", "buy", 250.0) for i in range(12))
    assessment = make_analyzer().assess(healthy_data(recent_trades=trades), make_pair())
    assert assessment.accumulation is AccumulationVerdict.ARTIFICIAL
    assert any("identical size" in f.message for f in assessment.findings)


def test_dominant_buyer_is_artificial_demand():
    trades = (trade("BigDog", "buy", 50_000.0),) + tuple(
        trade(f"Small{i}", "buy", 60.0 + i) for i in range(6))
    assessment = make_analyzer().assess(healthy_data(recent_trades=trades), make_pair())
    assert any("artificial demand" in f.message for f in assessment.findings)
    assert assessment.accumulation is AccumulationVerdict.ARTIFICIAL


def test_entry_timing_chasing_vs_accumulating():
    # Buys at the top of the observed range = chasing.
    chasing = tuple(trade(f"W{i}", "buy", 200.0, price=0.20) for i in range(6)) + tuple(
        trade(f"S{i}", "sell", 100.0, price=0.10 + 0.02 * i) for i in range(5))
    a_chasing = make_analyzer().assess(healthy_data(recent_trades=chasing), make_pair())

    accumulating = tuple(trade(f"W{i}", "buy", 200.0, price=0.11) for i in range(6)) + tuple(
        trade(f"S{i}", "sell", 100.0, price=0.10 + 0.02 * i) for i in range(5))
    a_accum = make_analyzer().assess(healthy_data(recent_trades=accumulating), make_pair())

    assert a_accum.sub_scores["entry_timing"] > a_chasing.sub_scores["entry_timing"]


def test_exchange_flow_lower_bounds():
    transfers = (
        TokenTransfer(from_owner="UserX",
                      to_owner="5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
                      ui_amount=1000.0, timestamp=NOW),
        TokenTransfer(from_owner="H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
                      to_owner="UserY", ui_amount=400.0, timestamp=NOW),
    )
    assessment = make_analyzer().assess(
        healthy_data(recent_transfers=transfers), make_pair(price=0.5))
    assert assessment.exchange_inflow_usd == pytest.approx(500.0)   # 1000 x 0.5
    assert assessment.exchange_outflow_usd == pytest.approx(200.0)  # 400 x 0.5


def test_no_trades_is_unknown_not_zero():
    data = healthy_data(recent_trades=(), holder_count=None)
    assessment = make_analyzer().assess(data, make_pair())
    assert assessment.accumulation is AccumulationVerdict.UNKNOWN
    assert assessment.accumulating_wallets is None
    assert assessment.sub_scores["quality_wallets"] is None


def test_no_data_at_all_raises():
    empty = WalletIntelData(token=TOKEN, sources=())
    with pytest.raises(InsufficientDataError):
        make_analyzer().assess(empty, make_pair())


def test_reputations_fill_historical_lens():
    assessment = make_analyzer().assess(
        healthy_data(), make_pair(),
        reputations={"Buyer0": 80.0, "Buyer1": 60.0},
    )
    assert assessment.sub_scores["historical_success"] == pytest.approx(70.0)
    assert assessment.coverage == pytest.approx(1.0)


def test_enrich_onchain_profile():
    assessment = make_analyzer().assess(healthy_data(), make_pair())
    profile = OnChainProfile(token=TOKEN, source="derived:market")
    enriched = enrich_onchain_profile(profile, assessment)
    assert enriched.smart_wallet_count == 10
    assert enriched.whale_net_flow_usd == assessment.whale_net_flow_usd


def test_sightings_flattened_for_storage():
    assessment = make_analyzer().assess(healthy_data(), make_pair())
    rows = sightings_from_assessment(assessment)
    sides = {side for _, side, _ in rows}
    assert "buy" in sides and "hold_whale" in sides
    assert all(usd is None or usd > 0 for _, _, usd in rows)


# ---- Wallet reputation formula (Part 17, Section 2) ----

def test_reputation_none_without_history():
    assert wallet_reputation(WalletTrackRecord(wallet="W")) is None


def test_reputation_full_record():
    record = WalletTrackRecord(
        wallet="W", tokens_traded=20, win_rate=0.7, early_entry_rate=0.6,
        rug_avoidance_rate=0.9, median_position_usd=1_000.0, active_span_days=120.0,
    )
    score, coverage = wallet_reputation(record)
    expected = (0.25 * 70 + 0.20 * 60 + 0.20 * 75 + 0.20 * 90 + 0.15 * 80)
    assert score == pytest.approx(expected)
    assert coverage == pytest.approx(1.0)


def test_reputation_partial_record_renormalizes():
    record = WalletTrackRecord(wallet="W", win_rate=0.8)
    score, coverage = wallet_reputation(record)
    assert score == pytest.approx(80.0)
    assert coverage == pytest.approx(0.25)


def test_summary_renders():
    text = make_analyzer().assess(healthy_data(), make_pair()).summary()
    assert "Smart money assessment" in text
    assert "accumulation=" in text
