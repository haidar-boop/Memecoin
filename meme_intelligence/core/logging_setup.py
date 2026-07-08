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


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> logging.Logger:
    """Configure the root project logger. Safe to call more than once."""
    root = logging.getLogger("meme_intelligence")
    root.setLevel(level.upper())

    if root.handlers:  # already configured (e.g. repeated calls in tests)
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

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the project namespace."""
    return logging.getLogger(f"meme_intelligence.{name}")
