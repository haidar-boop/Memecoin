"""Tests for configuration loading and validation (Spec Part 31 defaults, Rule 17)."""

import pytest

from meme_intelligence.config.settings import (
    ClassificationBands,
    LiquidityProbeSettings,
    ScoringWeights,
    SecuritySubWeights,
    SecurityThresholds,
    Settings,
)
from meme_intelligence.core.errors import ConfigurationError


def test_default_settings_are_valid():
    settings = Settings.from_env(env={})
    assert settings.weights.security == 0.15
    assert settings.weights.timing == 0.10
    assert settings.bands.elite == 90.0
    assert settings.alerts.overall == 85.0
    assert settings.intervals.ultra_fast == 7.0


def test_scoring_weights_match_consistency_lock():
    """Part 31 Section 4: 6 categories at 15% plus timing at 10%."""
    w = ScoringWeights()
    assert (w.foundation, w.security, w.community, w.blockchain, w.momentum, w.narrative) == (
        0.15, 0.15, 0.15, 0.15, 0.15, 0.15,
    )
    assert w.timing == 0.10


def test_security_sub_weights_sum_to_one():
    w = SecuritySubWeights()
    assert w.contract + w.liquidity + w.distribution + w.developer + w.manipulation == pytest.approx(1.0)


def test_security_sub_weights_match_part_33_section_11():
    """Lock the values to Part 33 Section 11's literal weighting (Rule 1) so
    the earlier undocumented drift (liquidity 0.25 / developer 0.15) can't
    silently recur."""
    w = SecuritySubWeights()
    assert (w.contract, w.liquidity, w.developer, w.distribution, w.manipulation) == (
        0.25, 0.20, 0.20, 0.20, 0.15)


def test_opportunity_weights_match_part_28_section_5():
    from meme_intelligence.config.settings import OpportunityWeights
    w = OpportunityWeights()
    assert (w.growth_potential, w.momentum, w.foundation, w.risk, w.timing) == (
        0.30, 0.25, 0.20, 0.15, 0.10)


def test_opportunity_weights_loaded_from_env():
    settings = Settings.from_env(env={"MEMEINTEL_OPPORTUNITY_WEIGHTS_GROWTH_POTENTIAL": "0.40",
                                      "MEMEINTEL_OPPORTUNITY_WEIGHTS_MOMENTUM": "0.15"})
    assert settings.opportunity_weights.growth_potential == 0.40
    assert settings.opportunity_weights.momentum == 0.15


def test_invalid_weight_sum_rejected():
    with pytest.raises(ConfigurationError, match="must sum to 1.0"):
        ScoringWeights(security=0.50)  # breaks the sum


def test_bands_must_descend():
    with pytest.raises(ConfigurationError, match="descending"):
        ClassificationBands(elite=70.0, strong_candidate=80.0)


def test_env_override_applies():
    env = {
        "MEMEINTEL_WEIGHTS_SECURITY": "0.20",
        "MEMEINTEL_WEIGHTS_TIMING": "0.05",
        "MEMEINTEL_INTERVALS_ULTRA_FAST": "5",
        "MEMEINTEL_LOG_LEVEL": "DEBUG",
    }
    settings = Settings.from_env(env=env)
    assert settings.weights.security == 0.20
    assert settings.weights.timing == 0.05
    assert settings.intervals.ultra_fast == 5.0
    assert settings.log_level == "DEBUG"


def test_bad_env_value_raises_clear_error():
    with pytest.raises(ConfigurationError, match="MEMEINTEL_HTTP_TIMEOUT_SECONDS"):
        Settings.from_env(env={"MEMEINTEL_HTTP_TIMEOUT_SECONDS": "not-a-number"})


def test_negative_interval_rejected():
    with pytest.raises(ConfigurationError, match="must be positive"):
        Settings.from_env(env={"MEMEINTEL_INTERVALS_FAST": "-1"})


def test_load_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "MEMEINTEL_TEST_DOTENV_A=hello\n"
        "MEMEINTEL_TEST_DOTENV_B='quoted'\n"
        "\n"
        "not-a-kv-line\n"
    )
    monkeypatch.delenv("MEMEINTEL_TEST_DOTENV_A", raising=False)
    monkeypatch.setenv("MEMEINTEL_TEST_DOTENV_B", "real-env-wins")

    from meme_intelligence.config.settings import load_dotenv
    loaded = load_dotenv(str(env_file))

    import os
    assert os.environ["MEMEINTEL_TEST_DOTENV_A"] == "hello"
    assert os.environ["MEMEINTEL_TEST_DOTENV_B"] == "real-env-wins"  # env beats file
    assert loaded == 1
    monkeypatch.delenv("MEMEINTEL_TEST_DOTENV_A")
    assert load_dotenv(str(tmp_path / "missing.env")) == 0


# ---- Live Jupiter round-trip sell test (Project 1) ----

def test_liquidity_probe_rejects_non_positive_probe_amount():
    with pytest.raises(ConfigurationError, match="probe_sol_amount"):
        LiquidityProbeSettings(probe_sol_amount=0.0)
    with pytest.raises(ConfigurationError, match="probe_sol_amount"):
        LiquidityProbeSettings(probe_sol_amount=-0.1)


def test_liquidity_probe_rejects_non_finite_probe_amount():
    """Bug-hunt 2026-07-12: NaN/inf slipped past `<= 0` (all comparisons with
    NaN are False), so a misconfigured env could size a probe trade with a
    non-finite SOL amount. Guard with math.isfinite."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ConfigurationError, match="probe_sol_amount"):
            LiquidityProbeSettings(probe_sol_amount=bad)


def test_liquidity_probe_rejects_out_of_range_slippage():
    with pytest.raises(ConfigurationError, match="slippage_bps"):
        LiquidityProbeSettings(slippage_bps=0)
    with pytest.raises(ConfigurationError, match="slippage_bps"):
        LiquidityProbeSettings(slippage_bps=10001)


def test_security_thresholds_reject_extreme_below_max_round_trip_loss():
    with pytest.raises(ConfigurationError, match="extreme_round_trip_loss_percent"):
        SecurityThresholds(max_round_trip_loss_percent=60.0, extreme_round_trip_loss_percent=50.0)


def test_env_picks_up_jupiter_api_key_and_probe_amount():
    env = {
        "MEMEINTEL_JUPITER_API_KEY": "test-jupiter-key",
        "MEMEINTEL_LIQUIDITY_PROBE_PROBE_SOL_AMOUNT": "0.5",
    }
    settings = Settings.from_env(env=env)
    assert settings.jupiter_api_key == "test-jupiter-key"
    assert settings.liquidity_probe.probe_sol_amount == pytest.approx(0.5)


# ---- Bug-hunt fixes: NaN-blind validation, bool parsing (Rule 6/8) ----

def test_nan_rejected_by_positivity_validation():
    """`nan <= 0` is always False, so a naive check lets NaN slip through."""
    with pytest.raises(ConfigurationError):
        from meme_intelligence.config.settings import ScanIntervals
        ScanIntervals(ultra_fast=float("nan"))


def test_nan_rejected_by_weight_sum_check():
    with pytest.raises(ConfigurationError, match="must sum to 1.0"):
        ScoringWeights(security=float("nan"))


def test_nan_rejected_by_http_settings():
    from meme_intelligence.config.settings import HttpSettings
    with pytest.raises(ConfigurationError):
        HttpSettings(timeout_seconds=float("nan"))


def test_http_settings_validates_previously_unchecked_fields():
    from meme_intelligence.config.settings import HttpSettings
    with pytest.raises(ConfigurationError):
        HttpSettings(cache_ttl_seconds=-1.0)
    with pytest.raises(ConfigurationError):
        HttpSettings(cache_max_entries=0)
    with pytest.raises(ConfigurationError):
        HttpSettings(retry_base_delay=10.0, retry_max_delay=1.0)


def test_wallet_dominance_usd_now_validated():
    from meme_intelligence.config.settings import WalletIntelSettings
    with pytest.raises(ConfigurationError):
        WalletIntelSettings(min_buy_volume_for_dominance_usd=-1.0)


def test_provider_cooldown_now_validated():
    from meme_intelligence.config.settings import ProviderSettings
    with pytest.raises(ConfigurationError):
        ProviderSettings(cooldown_seconds=0.0)


def test_bool_env_rejects_unrecognized_string_instead_of_silently_false():
    """A typo like 'treu' previously became False silently."""
    with pytest.raises(ConfigurationError):
        Settings.from_env(env={"MEMEINTEL_AI_ENABLE_IN_MONITOR": "treu"})


def test_bool_env_still_accepts_common_spellings():
    for value in ("1", "true", "TRUE", "yes", "on"):
        settings = Settings.from_env(env={"MEMEINTEL_AI_ENABLE_IN_MONITOR": value})
        assert settings.ai.enable_in_monitor is True
    for value in ("0", "false", "no", "off"):
        settings = Settings.from_env(env={"MEMEINTEL_AI_ENABLE_IN_MONITOR": value})
        assert settings.ai.enable_in_monitor is False


def test_copycat_veto_settings_validate_and_load():
    """The copycat screen is configurable (Rule 17): thresholds must be
    positive, and the enable switch parses as a real boolean."""
    from meme_intelligence.config.settings import Settings

    with pytest.raises(ConfigurationError, match="copycat_liquidity_ratio"):
        Settings.from_env(env={"MEMEINTEL_ALERTS_COPYCAT_LIQUIDITY_RATIO": "-1"})
    with pytest.raises(ConfigurationError, match="copycat_min_liquidity_usd"):
        Settings.from_env(env={"MEMEINTEL_ALERTS_COPYCAT_MIN_LIQUIDITY_USD": "0"})

    defaults = Settings.from_env(env={})
    assert defaults.alerts.copycat_veto_enabled is True
    off = Settings.from_env(env={"MEMEINTEL_ALERTS_COPYCAT_VETO_ENABLED": "false"})
    assert off.alerts.copycat_veto_enabled is False


# ---- Project 2: Telegram commands + execution scaffold groups ----

def test_telegram_command_settings_validation():
    from meme_intelligence.config.settings import TelegramCommandSettings

    assert TelegramCommandSettings().enabled is False   # opt-in (ROADMAP #2)
    with pytest.raises(ConfigurationError, match="poll_timeout_seconds"):
        TelegramCommandSettings(poll_timeout_seconds=0.0)
    with pytest.raises(ConfigurationError, match="poll_timeout_seconds"):
        TelegramCommandSettings(poll_timeout_seconds=51.0)
    with pytest.raises(ConfigurationError, match="idle_delay_seconds"):
        TelegramCommandSettings(idle_delay_seconds=0.0)


def test_execution_settings_validation_and_defaults():
    from meme_intelligence.config.settings import ExecutionSettings

    defaults = ExecutionSettings()
    assert defaults.buy_button_enabled is False   # buttons hidden by default
    assert defaults.live_enabled is False         # live trading off by default
    assert defaults.buy_preset_list() == (0.05, 0.1)
    with pytest.raises(ConfigurationError, match="max_buy_sol"):
        ExecutionSettings(max_buy_sol=0.0)
    with pytest.raises(ConfigurationError, match="slippage_bps"):
        ExecutionSettings(slippage_bps=0)
    # A preset above the per-trade cap is rejected (can't offer an illegal button).
    with pytest.raises(ConfigurationError, match="exceeds max_buy_sol"):
        ExecutionSettings(max_buy_sol=0.1, buy_presets_sol="0.05,0.5")


def test_execution_live_env_overrides():
    settings = Settings.from_env(env={
        "MEMEINTEL_EXECUTION_LIVE_ENABLED": "true",
        "MEMEINTEL_EXECUTION_PRIVATE_KEY": "somebase58key",
        "MEMEINTEL_EXECUTION_MAX_BUY_SOL": "0.2",
        "MEMEINTEL_EXECUTION_HELIUS_API_KEY": "dedicated-trading-key",
    })
    assert settings.execution.live_enabled is True
    assert settings.trading_private_key == "somebase58key"
    assert settings.execution.max_buy_sol == 0.2
    assert settings.trading_helius_api_key == "dedicated-trading-key"
    # Default stays empty -> trading shares the scanner's key (Rule 18).
    assert Settings.from_env(env={}).trading_helius_api_key == ""


def test_project2_env_overrides_load():
    settings = Settings.from_env(env={
        "MEMEINTEL_TELEGRAM_COMMANDS_ENABLED": "true",
        "MEMEINTEL_TELEGRAM_COMMANDS_POLL_TIMEOUT_SECONDS": "30",
        "MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED": "true",
        "MEMEINTEL_EXECUTION_MAX_BUY_SOL": "0.25",
    })
    assert settings.telegram_commands.enabled is True
    assert settings.telegram_commands.poll_timeout_seconds == 30.0
    assert settings.execution.buy_button_enabled is True
    assert settings.execution.max_buy_sol == 0.25


# ---- Project 3: mind-layer veto settings ----

def test_learning_veto_defaults_off_and_validates():
    from meme_intelligence.config.settings import LearningSettings

    defaults = LearningSettings()
    assert defaults.veto_enabled is False   # authority is earned, then opted into
    with pytest.raises(ConfigurationError, match="veto_min_p_rug"):
        LearningSettings(veto_min_p_rug=1.5)
    with pytest.raises(ConfigurationError, match="veto_min_samples"):
        LearningSettings(veto_min_samples=0)
    with pytest.raises(ConfigurationError, match="veto_metrics_ttl_seconds"):
        LearningSettings(veto_metrics_ttl_seconds=0.0)


def test_learning_veto_env_overrides():
    settings = Settings.from_env(env={
        "MEMEINTEL_LEARNING_VETO_ENABLED": "true",
        "MEMEINTEL_LEARNING_VETO_MIN_P_RUG": "0.9",
        "MEMEINTEL_LEARNING_VETO_MIN_ACCURACY": "0.8",
        "MEMEINTEL_LEARNING_VETO_MIN_SAMPLES": "25",
    })
    assert settings.learning.veto_enabled is True
    assert settings.learning.veto_min_p_rug == 0.9
    assert settings.learning.veto_min_accuracy == 0.8
    assert settings.learning.veto_min_samples == 25


def test_liquidity_probe_rejects_bad_sell_confirm_fraction():
    with pytest.raises(ConfigurationError, match="sell_confirm_fraction"):
        LiquidityProbeSettings(sell_confirm_fraction=0.0)
    with pytest.raises(ConfigurationError, match="sell_confirm_fraction"):
        LiquidityProbeSettings(sell_confirm_fraction=1.0)
    assert LiquidityProbeSettings(sell_confirm_fraction=0.1).sell_confirm_fraction == 0.1


def test_insufficient_data_retry_settings_load_and_validate():
    from meme_intelligence.config.settings import WorkflowSettings

    defaults = WorkflowSettings()
    assert defaults.insufficient_data_retry_enabled is True
    assert defaults.insufficient_data_min_coverage == 0.5
    assert defaults.insufficient_data_retry_minutes == 15.0
    assert defaults.insufficient_data_max_age_minutes == 120.0

    with pytest.raises(ConfigurationError, match="insufficient_data_min_coverage"):
        WorkflowSettings(insufficient_data_min_coverage=0.0)
    with pytest.raises(ConfigurationError, match="insufficient_data_min_coverage"):
        WorkflowSettings(insufficient_data_min_coverage=1.5)
    with pytest.raises(ConfigurationError, match="insufficient_data_max_age_minutes"):
        WorkflowSettings(insufficient_data_retry_minutes=30.0,
                         insufficient_data_max_age_minutes=10.0)

    settings = Settings.from_env(env={
        "MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_ENABLED": "false",
        "MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_MIN_COVERAGE": "0.4",
        "MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_RETRY_MINUTES": "10",
        "MEMEINTEL_WORKFLOW_INSUFFICIENT_DATA_MAX_AGE_MINUTES": "60",
    })
    assert settings.workflow.insufficient_data_retry_enabled is False
    assert settings.workflow.insufficient_data_min_coverage == 0.4
    assert settings.workflow.insufficient_data_retry_minutes == 10.0
    assert settings.workflow.insufficient_data_max_age_minutes == 60.0


def test_boost_watcher_defaults():
    bw = Settings.from_env(env={}).boost_watcher
    assert bw.enabled is False
    assert bw.threshold == 100.0
    assert bw.poll_interval_seconds == 30.0
    assert bw.chain_filter == "solana"
    assert bw.max_seen_keys == 5000


def test_boost_watcher_env_override():
    bw = Settings.from_env(env={
        "MEMEINTEL_BOOST_WATCHER_ENABLED": "true",
        "MEMEINTEL_BOOST_WATCHER_THRESHOLD": "250",
        "MEMEINTEL_BOOST_WATCHER_CHAIN_FILTER": "",
    }).boost_watcher
    assert bw.enabled is True
    assert bw.threshold == 250.0
    assert bw.chain_filter == ""     # empty = all chains


def test_boost_watcher_rejects_nonpositive_threshold():
    with pytest.raises(ConfigurationError, match="threshold"):
        Settings.from_env(env={"MEMEINTEL_BOOST_WATCHER_THRESHOLD": "0"})


def test_smart_wallet_defaults():
    sw = Settings.from_env(env={}).smart_wallet
    assert sw.enabled is False
    assert sw.max_holders_per_token == 10
    assert sw.max_seen_keys == 5000


def test_smart_wallet_env_override():
    sw = Settings.from_env(env={
        "MEMEINTEL_SMART_WALLET_ENABLED": "true",
        "MEMEINTEL_SMART_WALLET_MAX_HOLDERS_PER_TOKEN": "5",
    }).smart_wallet
    assert sw.enabled is True
    assert sw.max_holders_per_token == 5


def test_smart_wallet_rejects_nonpositive_holder_cap():
    with pytest.raises(ConfigurationError, match="max_holders_per_token"):
        Settings.from_env(env={"MEMEINTEL_SMART_WALLET_MAX_HOLDERS_PER_TOKEN": "0"})
