#!/usr/bin/env bash
# Turn X/Twitter community tracking (social intelligence via LunarCrush,
# Roadmap item 5) ON or OFF.
#
# X/Twitter community tracking reads aggregate social-conversation signal
# for each candidate coin from LunarCrush — sentiment (overall and
# X-specific), how much content people are posting about it on X, social
# dominance, "galaxy score", and rank — and merges it into the existing
# community score alongside CoinGecko's free data. It spends METERED
# LunarCrush API credits, so it should only be enabled on a PAID LunarCrush
# plan. The built-in credit gate keeps the spend small: lookups only run on
# coins that could still earn a buy-side alert, capped per day, with a
# per-coin cooldown — the same protection the wallet-tracking layer uses.
#
# Usage (from the repo root, e.g. ~/meme-intelligence):
#
#   bash deploy/enable-x-community-tracking.sh <PAID_LUNARCRUSH_API_KEY>   # turn ON
#   bash deploy/enable-x-community-tracking.sh off                        # turn OFF
#
# The script edits .env in place (replacing the line if the key already
# exists — appended duplicates would be ignored by the loader) and restarts
# the meme-intelligence service if it is installed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"

if [ $# -ne 1 ] || [ -z "$1" ]; then
    echo "Usage: bash deploy/enable-x-community-tracking.sh <PAID_LUNARCRUSH_API_KEY>"
    echo "       bash deploy/enable-x-community-tracking.sh off"
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
    set_kv MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR false
    echo "X/Twitter community tracking DISABLED (the LunarCrush key in .env is kept for manual use)."
else
    set_kv MEMEINTEL_LUNARCRUSH_API_KEY "$1"
    set_kv MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR true
    echo "X/Twitter community tracking ENABLED with the provided LunarCrush key."
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
