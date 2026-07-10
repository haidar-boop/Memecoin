"""Consistent SQLite backup for the intelligence database (Part 32.5 S10).

Uses sqlite3's online backup API, which is safe against a live writer
(the 24/7 monitor) — a plain file copy of a WAL database is not. Keeps
the last N daily copies and prunes older ones.

Run from the repo root (cron does):  .venv/bin/python deploy/backup_db.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

KEEP = 7  # rotated daily copies to retain


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo))
    from meme_intelligence.config.settings import get_settings

    db_path = repo / get_settings().database.path
    if not db_path.exists():
        print(f"nothing to back up: {db_path} does not exist")
        return 0

    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = backup_dir / f"{db_path.stem}.{stamp}.sqlite3"

    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)  # online backup: consistent even mid-write
    finally:
        dest.close()
        source.close()

    copies = sorted(backup_dir.glob(f"{db_path.stem}.*.sqlite3"))
    for stale in copies[:-KEEP]:
        stale.unlink()
    print(f"backed up {db_path} -> {target} ({len(copies[-KEEP:])} copies kept)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
