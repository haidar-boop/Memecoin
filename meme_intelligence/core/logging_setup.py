"""Logging configuration (Rule 13 — every important action should be logged).

Console output for interactive use plus a rotating file in ``log_dir`` so
long-running scanner sessions keep bounded, inspectable history (Rule 7).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# Levels ``logging.Logger.setLevel`` accepts by name. Anything else raises
# ValueError, which used to kill the process before any handler existed.
_VALID_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> logging.Logger:
    """Configure the root project logger. Safe to call more than once.

    An unrecognized ``level`` falls back to INFO with a warning instead of
    raising. ``MEMEINTEL_LOG_LEVEL`` is unvalidated env text that the operator
    edits by hand on the droplet, and this function is the SECOND statement of
    ``_run`` — before any handler, sink or scanner exists. A typo
    (``WARNNING``, ``verbose``) therefore raised ``ValueError: Unknown level``
    straight out of ``main()``, and with ``Restart=always`` /
    ``StartLimitIntervalSec=0`` in the systemd unit the service crash-looped
    every 10 seconds forever, Telegram silent, with nothing but a traceback in
    the journal that a non-technical operator has no reason to look for
    (bug-hunt finding, 2026-07-29). A misspelled log level must never be able
    to take the bot off the air (Rule 7); it is surfaced loudly instead
    (Rule 13).
    """
    requested = (level or "").strip().upper()
    unusable = requested not in _VALID_LEVELS
    root = logging.getLogger("meme_intelligence")
    root.setLevel("INFO" if unusable else requested)

    def _warn_if_unusable() -> None:
        if unusable:
            root.warning(
                "log level %r is not recognized (expected one of %s) — "
                "falling back to INFO; fix MEMEINTEL_LOG_LEVEL in .env",
                level, ", ".join(_VALID_LEVELS))

    if root.handlers:  # already configured (e.g. repeated calls in tests)
        _warn_if_unusable()
        return root

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "meme_intelligence.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Warn only now that the handlers exist, so the message actually reaches
    # the console and the log file rather than being swallowed.
    _warn_if_unusable()
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the project namespace."""
    return logging.getLogger(f"meme_intelligence.{name}")
