"""Tests for logging setup robustness (Rule 7 — a config typo must not
take the bot off the air)."""

import logging

import pytest

from meme_intelligence.core.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def _clean_root():
    root = logging.getLogger("meme_intelligence")
    saved = list(root.handlers)
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved)


def test_typo_in_log_level_falls_back_to_info_instead_of_crashing(tmp_path, caplog):
    """Bug hunt 2026-07-29: MEMEINTEL_LOG_LEVEL=WARNNING raised ValueError as
    the second statement of _run, before any handler or sink existed, and
    systemd (Restart=always, StartLimitIntervalSec=0) crash-looped forever
    with Telegram silent."""
    with caplog.at_level(logging.WARNING, logger="meme_intelligence"):
        root = setup_logging("WARNNING", log_dir=str(tmp_path))
        assert root.level == logging.INFO
        assert "not recognized" in caplog.text
        assert "WARNNING" in caplog.text


def test_empty_and_none_log_levels_are_survivable(tmp_path):
    assert setup_logging("", log_dir=str(tmp_path)).level == logging.INFO


def test_valid_levels_are_honored_and_case_insensitive(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="meme_intelligence"):
        root = setup_logging("debug", log_dir=str(tmp_path))
        assert root.level == logging.DEBUG
        assert "not recognized" not in caplog.text


def test_warning_still_emitted_when_handlers_already_exist(tmp_path, caplog):
    setup_logging("INFO", log_dir=str(tmp_path))       # first call adds handlers
    with caplog.at_level(logging.WARNING, logger="meme_intelligence"):
        root = setup_logging("verbose", log_dir=str(tmp_path))
        assert root.level == logging.INFO
        assert "not recognized" in caplog.text
