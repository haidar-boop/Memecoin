"""Tests for the hard-signal rug-pull engine (Section 5a)."""

import pytest

from meme_intelligence.config.settings import RugSignalWeights, RugThresholds, Settings
from meme_intelligence.core.errors import ConfigurationError
from meme_intelligence.core.models import SecurityProfile, TokenIdentity
from meme_intelligence.learning.models import CoinSnapshot
from meme_intelligence.learning.rug_engine import RugEngine

TOKEN = TokenIdentity(chain="solana", address="Mint1")


def _engine() -> RugEngine:
    return RugEngine(RugSignalWeights(), RugThresholds())


def _sec(**kw) -> SecurityProfile:
    return SecurityProfile(token=TOKEN, source="test", **kw)


def test_clean_token_scores_zero():
    sec = _sec(is_honeypot=False, is_mintable=False, is_freezable=False,
               lp_locked_percent=100.0, top_holder_percent=5.0, top10_holder_percent=25.0,
               sell_tax_percent=0.0)
    result = _engine().assess(security=sec, deployer_rug_count=0)
    assert result.score == 0
    assert result.fired_names == ()


def test_unknown_data_does_not_fire():
    # Everything None: unknown must never be treated as guilt (Rule 8).
    result = _engine().assess(security=_sec(), snapshots=(), deployer_rug_count=0)
    assert result.score == 0
    assert result.signals == ()


def test_honeypot_fires_unsellable():
    result = _engine().assess(security=_sec(is_honeypot=True))
    assert "unsellable" in result.fired_names
    assert result.score == RugSignalWeights().unsellable


def test_unsellable_override_wins_over_clean_profile():
    result = _engine().assess(security=_sec(is_honeypot=False), unsellable_override=True)
    assert "unsellable" in result.fired_names


def test_mint_and_freeze_authorities():
    result = _engine().assess(security=_sec(is_mintable=True, is_freezable=True))
    assert {"mint_authority_active", "freeze_authority_active"} <= set(result.fired_names)


def test_concentration_top_holder():
    result = _engine().assess(security=_sec(top_holder_percent=45.0))
    assert "top_holder_concentration" in result.fired_names


def test_unlocked_liquidity_fires_but_locked_does_not():
    fired = _engine().assess(security=_sec(lp_locked_percent=10.0))
    assert "liquidity_unlocked" in fired.fired_names
    safe = _engine().assess(security=_sec(lp_locked_percent=90.0))
    assert "liquidity_unlocked" not in safe.fired_names


def test_high_sell_tax():
    result = _engine().assess(security=_sec(sell_tax_percent=35.0))
    assert "high_sell_tax" in result.fired_names


def test_liquidity_removal_from_drop():
    snaps = [
        CoinSnapshot(age_seconds=0, liquidity_usd=10000.0),
        CoinSnapshot(age_seconds=60, liquidity_usd=8000.0),
        CoinSnapshot(age_seconds=120, liquidity_usd=2000.0),  # -80% from peak
    ]
    result = _engine().assess(snapshots=snaps)
    assert "liquidity_removed" in result.fired_names


def test_liquidity_removal_from_event():
    snaps = [
        CoinSnapshot(age_seconds=0, liquidity_usd=10000.0, liquidity_event_usd=0.0),
        CoinSnapshot(age_seconds=60, liquidity_usd=9000.0, liquidity_event_usd=-5000.0),
    ]
    result = _engine().assess(snapshots=snaps)
    assert "liquidity_removed" in result.fired_names


def test_dev_dumping():
    snaps = [
        CoinSnapshot(age_seconds=0, dev_outflow_usd=0.0),
        CoinSnapshot(age_seconds=60, dev_outflow_usd=5000.0),
    ]
    result = _engine().assess(snapshots=snaps)
    assert "dev_wallet_dumping" in result.fired_names


def test_fake_volume_wash_trading():
    # High volume, very few holders -> suspicious volume-per-holder.
    snaps = [CoinSnapshot(age_seconds=0, volume_1h_usd=100000.0, holder_count=10)]
    result = _engine().assess(snapshots=snaps)
    assert "fake_volume" in result.fired_names


def test_fake_volume_not_fired_with_healthy_holders():
    snaps = [CoinSnapshot(age_seconds=0, volume_1h_usd=100000.0, holder_count=5000)]
    result = _engine().assess(snapshots=snaps)
    assert "fake_volume" not in result.fired_names


def test_deployer_blacklist_fires():
    result = _engine().assess(security=_sec(), deployer_rug_count=2)
    assert "deployer_blacklisted" in result.fired_names


def test_same_creator_honeypot_count_fires_deployer():
    result = _engine().assess(security=_sec(honeypot_same_creator_count=3), deployer_rug_count=0)
    assert "deployer_blacklisted" in result.fired_names


def test_score_clamped_at_100():
    # Pile on many signals; score must cap at 100.
    sec = _sec(is_honeypot=True, is_mintable=True, is_freezable=True,
               lp_locked_percent=0.0, top_holder_percent=90.0, sell_tax_percent=50.0,
               honeypot_same_creator_count=5)
    snaps = [
        CoinSnapshot(age_seconds=0, liquidity_usd=10000.0, dev_outflow_usd=0.0,
                     volume_1h_usd=100000.0, holder_count=3),
        CoinSnapshot(age_seconds=60, liquidity_usd=500.0, dev_outflow_usd=9000.0,
                     liquidity_event_usd=-9000.0, volume_1h_usd=100000.0, holder_count=3),
    ]
    result = _engine().assess(security=sec, snapshots=snaps, deployer_rug_count=4)
    assert result.score == 100
    # And the fired signals are still individually reported (explainable).
    assert len(result.signals) >= 6


def test_rug_thresholds_config_defaults_and_validation():
    s = Settings.from_env(env={})
    assert s.rug_thresholds.min_lp_locked_percent == 50.0
    assert s.rug_thresholds.sell_tax_max_percent == 20.0
    with pytest.raises(ConfigurationError):
        RugThresholds(min_lp_locked_percent=150.0)
    with pytest.raises(ConfigurationError):
        RugThresholds(dev_dump_usd=0.0)


def test_rug_thresholds_env_override():
    s = Settings.from_env(env={"MEMEINTEL_RUG_THRESHOLDS_SELL_TAX_MAX_PERCENT": "10"})
    assert s.rug_thresholds.sell_tax_max_percent == 10.0
    # A 15% tax now fires against the tightened threshold.
    engine = RugEngine(RugSignalWeights(), s.rug_thresholds)
    assert "high_sell_tax" in engine.assess(
        security=_sec(sell_tax_percent=15.0)).fired_names
