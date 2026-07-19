#!/usr/bin/env bash
# Turn wallet tracking (smart-money intelligence, Part 17) ON or OFF.
#
# Wallet tracking reads who holds and trades each candidate coin (whales,
# smart wallets, bot-painted volume) and feeds it into scoring and alerts.
# It spends METERED Helius API credits, so it should only be enabled on a
# PAID Helius plan — the free tier was exhausted in ~3 days the last time
# it ran (2026-07-11). The built-in credit gate keeps the spend small:
# lookups only run on coins that could still earn a buy-side alert, capped
# per day, with a per-coin cooldown.
#
# Usage (from the repo root, e.g. ~/meme-intelligence):
#
#   bash deploy/enable-wallet-tracking.sh <PAID_HELIUS_API_KEY>   # turn ON
#   bash deploy/enable-wallet-tracking.sh off                     # turn OFF
#
# The script edits .env in place (replacing the line if the key already
# exists — appended duplicates would be ignored by the loader) and restarts
# the meme-intelligence service if it is installed. It never touches
# MEMEINTEL_EXECUTION_HELIUS_API_KEY: trading keeps its own separate
# account so the money path never competes with data collection.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"

if [ $# -ne 1 ] || [ -z "$1" ]; then
    echo "Usage: bash deploy/enable-wallet-tracking.sh <PAID_HELIUS_API_KEY>"
    echo "       bash deploy/enable-wallet-tracking.sh off"
    exit 1
fi

touch "$ENV_FILE"

# Replace KEY=... if present (first occurrence wins in the loader), else append.
set_kv() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    fi
}

if [ "$1" = "off" ]; then
    set_kv MEMEINTEL_WALLET_ENABLE_IN_MONITOR false
    set_kv MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE 0
    echo "Wallet tracking DISABLED (the Helius key in .env is kept for manual commands)."
else
    set_kv MEMEINTEL_HELIUS_API_KEY "$1"
    set_kv MEMEINTEL_WALLET_ENABLE_IN_MONITOR true
    # Keep the momentum alert floor aligned with the credit gate's security
    # floor (both 50): a coin too weak for a wallet lookup should not reach
    # the phone as a buy signal either.
    set_kv MEMEINTEL_ALERTS_MOMENTUM_MIN_SECURITY_SCORE 50
    echo "Wallet tracking ENABLED with the provided Helius key."
    echo "Credit protection active: lookups only on alert-worthy coins,"
    echo "max 200/day, 60-min per-coin cooldown (all tunable in .env)."
fi

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files meme-intelligence.service >/dev/null 2>&1; then
    systemctl restart meme-intelligence 2>/dev/null \
        || sudo systemctl restart meme-intelligence \
        || echo "Could not restart automatically — run: sudo systemctl restart meme-intelligence"
    echo "Service restarted."
else
    echo "No meme-intelligence service found — restart the monitor yourself to apply."
fi
