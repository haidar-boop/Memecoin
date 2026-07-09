"""Tests for configuration loading and validation (Spec Part 31 defaults, Rule 17)."""

import pytest

from meme_intelligence.config.settings import (
    ClassificationBands,
    ScoringWeights,
    SecuritySubWeights,
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
