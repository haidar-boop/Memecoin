#!/usr/bin/env bash
# Install the scheduled jobs that complement the 24/7 monitor:
#
#   * daily routine  (Part 11) — market regime check, watchlist deep
#     review, daily report — once a day at 13:05 UTC
#   * backtest --refresh (Part 24) — measure prediction outcomes so the
#     self-improvement metrics accumulate — every hour (was every 6h;
#     2026-07-19 operator request: grade coins as soon as their windows
#     come due instead of waiting for the next 6-hour slot — same total
#     work, spread over smaller, quicker runs; flock still prevents
#     overlap if a run is slow)
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

DAILY="5 13 * * * cd $REPO_DIR && flock -n /tmp/memeintel-daily.lock $PYTHON -m meme_intelligence daily >> logs/cron-daily.log 2>&1"
BACKTEST="15 * * * * cd $REPO_DIR && flock -n /tmp/memeintel-backtest.lock $PYTHON -m meme_intelligence backtest --refresh >> logs/cron-backtest.log 2>&1"
BACKUP="45 13 * * * cd $REPO_DIR && flock -n /tmp/memeintel-backup.lock $PYTHON deploy/backup_db.py >> logs/cron-backup.log 2>&1"

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
