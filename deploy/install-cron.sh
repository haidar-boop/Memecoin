#!/usr/bin/env bash
# Install the scheduled jobs that complement the 24/7 monitor:
#
#   * daily routine  (Part 11) — market regime check, watchlist deep
#     review, daily report — once a day at 05:05 UTC
#   * backtest --refresh (Part 24) — measure prediction outcomes so the
#     self-improvement metrics accumulate — every 6 hours
#
# Times are anchored to the operator's mornings in Lebanon (Asia/Beirut,
# UTC+3 in summer): the whole heavy cluster (backtest 04:15 -> daily 05:05
# -> backup 05:45 UTC) lands 07:15-08:45 Beirut, which is also the deadest
# US-market window — so the brief resource squeeze these jobs put on the
# 1 GB droplet's live monitor no longer causes a quiet-alert stretch in
# his afternoon (2026-07-24 operator move; previously 12:15-13:45 UTC).
# Cron stays in UTC deliberately (Rule 21 — no CRON_TZ cleverness); when
# Lebanon leaves DST in winter everything arrives an hour earlier
# locally, which is fine.
#
# Both share the monitor's SQLite database; storage runs in WAL mode
# with a busy timeout so concurrent access is safe. flock prevents a
# slow run from overlapping with the next one (Rule 11).
#
# Run from the repo root:  bash deploy/install-cron.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO_DIR/.venv/bin/python"
MARKER="# meme-intelligence scheduled jobs"

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON not found — run deploy/setup.sh first." >&2
  exit 1
fi

mkdir -p "$REPO_DIR/logs"

DAILY="5 5 * * * cd $REPO_DIR && flock -n /tmp/memeintel-daily.lock $PYTHON -m meme_intelligence daily >> logs/cron-daily.log 2>&1"
BACKTEST="15 4,10,16,22 * * * cd $REPO_DIR && flock -n /tmp/memeintel-backtest.lock $PYTHON -m meme_intelligence backtest --refresh >> logs/cron-backtest.log 2>&1"
BACKUP="45 5 * * * cd $REPO_DIR && flock -n /tmp/memeintel-backup.lock $PYTHON deploy/backup_db.py >> logs/cron-backup.log 2>&1"

# Replace any previous block we installed, keep everything else.
CURRENT="$(crontab -l 2>/dev/null | grep -v "$MARKER" | grep -vF "meme_intelligence daily" | grep -vF "meme_intelligence backtest" | grep -vF "deploy/backup_db.py" || true)"
{
  [ -n "$CURRENT" ] && echo "$CURRENT"
  echo "$MARKER"
  echo "$DAILY"
  echo "$BACKTEST"
  echo "$BACKUP"
} | crontab -

echo "==> Installed. Current crontab:"
crontab -l
echo ""
echo "Logs will appear in $REPO_DIR/logs/cron-daily.log and cron-backtest.log"
